#!/usr/bin/env python3

import argparse
import datetime as dt
import pandas as pd
from pathlib import Path
import sqlite3

import lmidb


def compute_all_account_positions(cursor: sqlite3.Cursor, account_filter: str | None = None) -> dict[str, pd.DataFrame]:
    # Get all accounts from the database
    if account_filter:
        cursor.execute("SELECT id, name, number, owner FROM accounts WHERE name = ?", (account_filter,))
    else:
        cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts and account_filter:
        print(f"No account found with name: {account_filter}")
        return {}

    all_positions = {}
    global account, positions, short_term_gains, long_term_gains, dividends, mgmt_fees, distributions, month

    for account in accounts:
        positions, short_term_gains, long_term_gains, dividends, mgmt_fees, distributions = compute_account_positions(
            cursor, account["id"]
        )
        all_positions[account["name"]] = positions

        # Trim dividends to only include columns with non-zero positions in the last row
        non_zero_cols = positions.iloc[-1][positions.iloc[-1] != 0].index
        dividends = dividends[non_zero_cols]

        # Create quarterly versions of the dataframes
        # Convert index to DatetimeIndex for resampling
        positions_temp = positions.copy()
        positions_temp.index = pd.to_datetime(positions_temp.index)
        short_term_gains_temp = short_term_gains.copy()
        short_term_gains_temp.index = pd.to_datetime(short_term_gains_temp.index)
        long_term_gains_temp = long_term_gains.copy()
        long_term_gains_temp.index = pd.to_datetime(long_term_gains_temp.index)
        dividends_temp = dividends.copy()
        dividends_temp.index = pd.to_datetime(dividends_temp.index)
        mgmt_fees_temp = mgmt_fees.copy()
        mgmt_fees_temp.index = pd.to_datetime(mgmt_fees_temp.index)
        distributions_temp = distributions.copy()
        distributions_temp.index = pd.to_datetime(distributions_temp.index)

        # Resample to quarterly: positions use last, others use sum
        positions_quarterly = positions_temp.resample("QE").last()
        short_term_gains_quarterly = short_term_gains_temp.resample("QE").sum()
        long_term_gains_quarterly = long_term_gains_temp.resample("QE").sum()
        dividends_quarterly = dividends_temp.resample("QE").sum()
        mgmt_fees_quarterly = mgmt_fees_temp.resample("QE").sum()
        distributions_quarterly = distributions_temp.resample("QE").sum()

        print(f"{account["name"]} quarterly dividends")
        print(f"{dividends_quarterly}")

    return all_positions


def compute_account_positions(cursor: sqlite3.Cursor, account_id: int):
    """Compute monthly positions and activity for given account for day there are transactions."""

    cursor.execute(f"SELECT MIN(Date) as first_date, MAX(Date) as last_date FROM transactions_{account_id}")
    result = cursor.fetchone()
    first_transaction_date = dt.datetime.fromisoformat(result["first_date"])
    latest_transaction_date = dt.datetime.fromisoformat(result["last_date"])

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
    dtis = pd.Series(pd.date_range(start=start_date, end=latest_position_date, freq="MS").date)
    dtie = list(pd.date_range(start=start_date, end=latest_position_date, freq="ME").date)
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
                print(f"Transaction: {tdate}: A:{action}, S:{symbol}, Q:{quantity}, A:{amount}, F:{fees}, P:{price}", end="")
                if symbol != "":
                    print(f" --- {symbol}:{positions.loc[month, symbol]:.2f}, {cash}:{positions.loc[month, cash]:.2f} -> ", end="")
                else:
                    print(f" --- {cash}:{positions.loc[month, cash]:.2f} -> ", end="")

            # The sign is reversed on transactions because we are going backwards in time
            # fmt: off
            match action:
                case "Advisor Fee":
                    # these are negative to start with
                    positions.loc[month, cash] += amount
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
                case "Div Adjustment":
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

    return positions, short_term_gains, long_term_gains, income, mgmt_fees, distributions


def roi(cursor: sqlite3.Cursor, account_filter: str | None = None) -> None:
    # Get all accounts from the database
    if account_filter:
        cursor.execute("SELECT id, name, number, owner FROM accounts WHERE name = ?", (account_filter,))
    else:
        cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts and account_filter:
        print(f"No account found with name: {account_filter}")
        return {}

    all_positions = {}
    for account in accounts:
        all_positions[account["name"]], activity = compute_account_positions(cursor, account["id"])

    return all_positions


def foo():
    # Categorize the transaction
    # fmt: off
    match action:
        case "Reinvest Dividend" | "Long Term Cap Gain Reinvest" | "Short Term Cap Gain Reinvest" | "Reinvest Shares":
            transaction_summary.loc[month_end, "reinvested dividends"] += amount # type: ignore
        case "Cash Dividend" | "Div Adjustment" | "Dividend Adj" | "Short Term Cap Gain" | "Long Term Cap Gain" | "Special Dividend" | "Bond Interest" | "Bank Interest" | "Reinvestment Adj":
            transaction_summary.loc[month_end, "cash dividends"] += amount # type: ignore
        case "Advisor Fee" | "Advisor Fee Adj":
            transaction_summary.loc[month_end, "advisor fees"] += amount # type: ignore
        case "MoneyLink Transfer" | "Security Transfer" | "Funds Received":
            if amount > 0:
                transaction_summary.loc[month_end, "contributions"] += amount # type: ignore
            else:
                transaction_summary.loc[month_end, "distributions"] += amount # type: ignore
        case "Wire Sent":
            transaction_summary.loc[month_end, "distributions"] += amount # type: ignore
        case "Buy" | "Sell" | "Full Redemption" | "Full Redemption Adj" | "Stock Split":
            # These don't affect transaction summary - they're position changes only
            pass
        case "Journal" | "Journaled Shares":
            # These are not yet handled - log for now
            print(f"Warning: Unhandled action '{action}' on {date} with amount {amount}")
        case _:
            # Unhandled action type
            print(f"Warning: Unknown action '{action}' on {date} with amount {amount}")
    # fmt: on


def summary(cursor: sqlite3.Cursor, account_filter: str | None = None) -> None:
    all_positions = compute_all_account_positions(cursor, account_filter)
    for account in all_positions.keys():
        print(f"Account: {account}")
        p = all_positions[account].iloc[0]
        print(p.to_frame().T)


def main():
    parser = argparse.ArgumentParser(description="ROI calculator", epilog="Use schwab account credentials if prompted for a login.")
    parser.add_argument(
        "--database", default=Path("lmi.db"), type=Path, metavar="fn", help="Name of database file. (default: %(default)s)"
    )
    parser.add_argument("-d", "--debug", action="store_true", default=False, help="Enable debug output")
    parser.add_argument(
        "--schwab-data",
        default=Path("schwab-data"),
        type=Path,
        metavar="dir",
        help="Directory to get shwab transactions and positions from. (default: %(default)s)",
    )
    parser.add_argument("--add-old", action="store_true", default=False, help="Add old/historical data when updating.")
    parser.add_argument("--account", default=None, type=str, help="Only act on this account name. (default: act on all accounts)")
    parser.add_argument(
        "action",
        type=str,
        choices=[
            "update-db",
            "dump-db",
            "summary",
            "roi",
            "update-candles",
            "compute-positions",
            "print-accounts",
            "print-positions",
            "print-transactions",
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

    match args.action:
        case "update-db":
            lmidb.update_db(cursor, args.schwab_data, args.add_old, args.account)
        case "update-candles":
            lmidb.update_candles(cursor)
        case "summary":
            summary(cursor, args.account)
        case "roi":
            roi(cursor)
        case "dump-db":
            lmidb.dump_summary(cursor, args.account)
        case "compute-positions":
            compute_all_account_positions(cursor, args.account)
        case "print-accounts":
            lmidb.print_accounts(cursor)
        case "print-positions":
            lmidb.print_positions(cursor, args.account)
        case "print-transactions":
            lmidb.print_transactions(cursor, args.account)
        case _:
            print("inconcievable")


if __name__ == "__main__":
    main()
