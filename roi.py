#!/usr/bin/env python3

import argparse
import datetime as dt
import pandas as pd
from pathlib import Path
import sqlite3

import lmidb


def compute_all_history(cursor: sqlite3.Cursor, args: argparse.Namespace) -> dict:
    account_filters = args.account
    # Get all accounts from the database
    if account_filters:
        placeholders = ",".join("?" * len(account_filters))
        cursor.execute(
            f"SELECT id, name, number, owner FROM accounts WHERE name IN ({placeholders})",
            account_filters,
        )
    else:
        cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts and account_filters:
        print(f"No accounts found with names: {', '.join(account_filters)}")
        return {}

    all_history = {}

    for account in accounts:
        if args.debug:
            print(f"Computing history for {account["name"]}")
        all_history[account["name"]] = compute_history(cursor, account["id"])

    return all_history


def compute_history(cursor: sqlite3.Cursor, account_id: int) -> dict:
    """Compute monthly positions, short term gains, long term gains, income, management fees, and distributions
    for the indicated account."""

    cursor.execute(
        f"SELECT MIN(Date) as first_date, MAX(Date) as last_date FROM transactions_{account_id}"
    )
    result = cursor.fetchone()
    first_transaction_date = dt.datetime.fromisoformat(result["first_date"])

    cursor.execute(
        f"""
        SELECT * FROM positions_{account_id} 
        WHERE Date = (SELECT MAX(Date) FROM positions_{account_id})
    """
    )
    latest_position_df = pd.DataFrame([dict(row) for row in cursor])
    latest_position_date = dt.datetime.fromisoformat(latest_position_df["date"][0])

    # start dataframe with the oldest transaction
    start_date = dt.date(first_transaction_date.year, first_transaction_date.month, 1)
    # end with the most recent position
    dtis = pd.Series(
        pd.date_range(start=start_date, end=latest_position_date, freq="MS").date
    )
    dtie = list(
        pd.date_range(start=start_date, end=latest_position_date, freq="ME").date
    )
    if dtie[-1] < latest_position_date.date():
        dtie.append(latest_position_date.date())
    dtie = pd.Series(dtie)

    symbols = lmidb.get_securities_in_account(cursor, account_id)
    positions = pd.DataFrame(index=dtie, columns=symbols)

    # initialize the end of positions datafrom with the latest position data
    positions[:] = 0
    for i, r in latest_position_df.iterrows():
        positions.loc[dtie.iloc[-1], r["symbol"]] = r["quantity"]

    cash = positions.columns[positions.columns.get_loc(lmidb.cash)]

    short_term_gains = pd.DataFrame(index=dtie, columns=symbols)
    short_term_gains[:] = 0.0
    long_term_gains = pd.DataFrame(index=dtie, columns=symbols)
    long_term_gains[:] = 0.0
    income = pd.DataFrame(index=dtie, columns=symbols)
    income[:] = 0
    mgmt_fees = pd.Series(index=dtie)
    mgmt_fees[:] = 0
    distributions = pd.Series(index=dtie)
    distributions[:] = 0

    # start of month, end of month, and end of previous month
    months = pd.concat([dtis, dtie, dtie.shift(1)], axis=1)[::-1]
    for i, m in months.iterrows():
        if m[2] == None:
            break
        month = m[2]
        positions.loc[month] = positions.loc[m[1]]

        cursor.execute(
            f"SELECT * FROM transactions_{account_id} WHERE Date >= ? AND Date <= ? ORDER BY Date DESC",
            (m[0].isoformat(), m[1].isoformat()),
        )

        for t in cursor:
            action = t["Action"]
            symbol = t["Symbol"]
            quantity = float(t["Quantity"])
            amount = float(t["Amount"])
            fees = float(t["fees"])
            price = float(t["price"])
            tdate = t["date"][:10]
            if args.debug:
                print(
                    f"Transaction: {tdate}: A:{action}, S:{symbol}, Q:{quantity}, A:{amount}, F:{fees}, P:{price}",
                    end="",
                )
                if symbol != "":
                    print(
                        f" --- {symbol}:{positions.loc[month, symbol]:.2f}, {cash}:{positions.loc[month, cash]:.2f} -> ",
                        end="",
                    )
                else:
                    print(f" --- {cash}:{positions.loc[month, cash]:.2f} -> ", end="")

            # The sign is reversed on transactions because we are going backwards in time
            # fmt: off
            match action:
                case "Advisor Fee":
                    # these are negative to start with
                    positions.loc[month, cash] -= amount
                    mgmt_fees[month] -= amount
                case "Advisor Fee Adj":
                    positions.loc[month, cash] -= amount
                    mgmt_fees[month] -= amount
                case "Bank Interest":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, cash] += amount
                case "Bond Interest":
                    positions.loc[month, cash] += amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Buy":
                    positions.loc[month, symbol] -= quantity # type: ignore
                    positions.loc[month, cash] -= amount
                case "Cash Dividend":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Div Adj" | "Dividend Adj" | "Div Adjustment":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Full Redemption":
                    positions.loc[month, symbol] -= amount # type: ignore
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Full Redemption Adj":
                    positions.loc[month, cash] -= amount
                    long_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Funds Received":
                    positions.loc[month, cash] -= amount
                    distributions.loc[month] -= amount
                case "Long Term Cap Gain":
                    positions.loc[month, cash] -= amount
                    long_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Long Term Cap Gain Reinvest":
                    positions.loc[month, cash] -= amount
                    long_term_gains.loc[month, symbol] += amount # type: ignore
                case "MoneyLink Transfer":
                    positions.loc[month, cash] -= amount
                    distributions[month] -= amount
                case "Reinvest Dividend":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                case "Reinvest Shares":
                    positions.loc[month, cash] -= amount
                    positions.loc[month, symbol] -= quantity # type: ignore
                case "Reinvestment Adj":
                    positions.loc[month, symbol] -= quantity # type: ignore
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                case "Security Transfer":
                    positions.loc[month, cash] -= amount
                case "Sell":
                    positions.loc[month, symbol] += quantity # type: ignore
                    positions.loc[month, cash] -= amount
                    long_term_gains.loc[month, symbol] += amount # type: ignore
                case "Short Term Cap Gain":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Short Term Cap Gain Reinvest":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Special Dividend":
                    positions.loc[month, cash] -= amount
                    short_term_gains.loc[month, symbol] += amount # type: ignore
                    income.loc[month, symbol] += amount # type: ignore
                case "Stock Split":
                    positions.loc[month, symbol] -= quantity # type: ignore
                case "Wire Sent":
                    positions.loc[month, cash] -= amount
                    distributions[month] -= amount
                case _:
                    print(f"Unhandled action: i:{i}")
                    print(f"{tdate}: A:{action}, S:{symbol}, Q:{quantity}, A:{amount}, F:{fees}, P:{price}")
            # fmt: off
            if args.debug:
                if symbol != "":
                    print(f"{symbol}:{positions.loc[month, symbol]:.2f}, {cash}:{positions.loc[month, cash]:.2f}")
                else:
                    print(f"{cash}:{positions.loc[month, cash]:.2f}")

    # resample the history on the indicated interval
    if args.interval != "ME":
        # Convert index to DatetimeIndex for resampling
        positions_temp = positions.copy()
        positions_temp.index = pd.to_datetime(positions_temp.index)
        positions = positions_temp.resample(args.interval).last()

        short_term_gains_temp = short_term_gains.copy()
        short_term_gains_temp.index = pd.to_datetime(short_term_gains_temp.index)
        short_term_gains = short_term_gains_temp.resample(args.interval).sum()

        long_term_gains_temp = long_term_gains.copy()
        long_term_gains_temp.index = pd.to_datetime(long_term_gains_temp.index)
        long_term_gains = long_term_gains_temp.resample(args.interval).sum()

        income_temp = income.copy()
        income_temp.index = pd.to_datetime(income_temp.index)
        income = income_temp.resample(args.interval).sum()

        mgmt_fees_temp = mgmt_fees.copy()
        mgmt_fees_temp.index = pd.to_datetime(mgmt_fees_temp.index)
        mgmt_fees = mgmt_fees_temp.resample(args.interval).sum()

        distributions_temp = distributions.copy()
        distributions_temp.index = pd.to_datetime(distributions_temp.index)
        distributions = distributions_temp.resample(args.interval).sum()

    if False:
        # add totals to the income, short_term_gains, and long_term_gains
        income_total = income.sum()
        income.index = income.index.strftime("%Y-%m-%d")
        income.loc["Total"] = income_total
        short_term_gains_total = short_term_gains.sum()
        short_term_gains.index = short_term_gains.index.strftime("%Y-%m-%d")
        short_term_gains.loc["Total"] = short_term_gains_total
        long_term_gains_total = long_term_gains.sum()
        long_term_gains.index = long_term_gains.index.strftime("%Y-%m-%d")
        long_term_gains.loc["Total"] = long_term_gains_total

    if args.active:
        # Only include securities still in account
        lp = positions.iloc[-1]
        lpi = lp[lp != 0].index
        positions = positions[lpi]
        short_term_gains = short_term_gains[lpi]
        long_term_gains = long_term_gains[lpi]
        income = income[lpi]

    history = {
        "positions": positions,
        "stg": short_term_gains,
        "ltg": long_term_gains,
        "income": income,
        "fees": mgmt_fees,
        "distributions": distributions,
    }
    if args.active:
        # compute which securities are stil the account
        lp = positions.iloc[-1]
        history["active"] = lp[lp != 0].index
    return history


def roi(cursor: sqlite3.Cursor, all_history: dict) -> None:
    for account, history in all_history.items():
        print(f"Account: {account} positions:")
        print(history["positions"])
        print(f"short_term_gains\n{history["stg"]}")
        print(f"long_term_gains\n{history["ltg"]}")
        print(f"income\n{history["income"]}")
        print(f"fees\n{history["fees"]}")
        print(f"distributions\n{history["distributions"]}")


def positions(cursor: sqlite3.Cursor, all_history: dict) -> None:
    for account, history in all_history.items():
        print(f"Account: {account}")
        print(history["positions"])


def summary(cursor: sqlite3.Cursor, all_history: dict) -> None:
    for account, history in all_history.items():
        positions = history["positions"]
        print(f"Account: {account}")
        print(positions.iloc[0].to_frame().T)
        print(positions.iloc[-1].to_frame().T)


def income(cursor: sqlite3.Cursor, all_history: dict) -> None:
    for account, history in all_history.items():
        print(f"{account} income:")
        print(history["income"])


def main():
    parser = argparse.ArgumentParser(
        description="ROI calculator",
        epilog="Use schwab account credentials if prompted for a login.",
    )
    parser.add_argument(
        "--database",
        default=Path("lmi.db"),
        type=Path,
        metavar="fn",
        help="Name of database file. (default: %(default)s)",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true", default=False, help="Enable debug output"
    )
    parser.add_argument(
        "-i",
        "--interval",
        default="ME",
        choices=["ME","QE","YE","all"],
        help="Interval to report on.",
    )
    parser.add_argument(
        "-a",
        "--active",
        action="store_true",
        default=False,
        help="Only show information for securities in the account at the most recent position.",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        type=str,
        help="Only act on this account name. Can be specified multiple times. (default: act on all accounts)",
    )
    parser.add_argument(
        "action",
        type=str,
        choices=[
            "positions",
            "income",
            "summary",
            "roi",
        ],
        help="Action to take.",
    )
    global args
    args = parser.parse_args()

    pd.options.display.float_format = "{:.2f}".format
    pd.options.display.max_rows = None
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 1000)

    fn = args.database
    if fn == Path(""):
        conn = sqlite3.connect(":memory:")
    else:
        conn = sqlite3.connect(fn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    global cursor
    cursor = conn.cursor()

    all_history = compute_all_history(cursor, args)

    match args.action:
        case "summary":
            summary(cursor, all_history)
        case "positions":
            positions(cursor, all_history)
        case "roi":
            roi(cursor, all_history)
        case "income":
            income(cursor, all_history)
        case _:
            print("inconcievable")


if __name__ == "__main__":
    main()
