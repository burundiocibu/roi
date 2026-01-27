#!/usr/bin/env python3

import argparse
import datetime as dt
import pandas as pd
from pathlib import Path
import sqlite3
import sys
import types

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
    for account in accounts:
        all_positions[account["name"]] = compute_account_positions(cursor, account["id"])

    return all_positions


def compute_account_positions(cursor: sqlite3.Cursor, account_id: int) -> pd.DataFrame:
    """Compute positions for given account for day there are transactions."""
    global dtis, dtie, positions, latest_position_df, months, symbols

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
    positions.loc[dtie.iloc[-1]] = 0
    for i, r in latest_position_df.iterrows():
        positions.loc[dtie.iloc[-1], r["symbol"]] = r["quantity"]

    cash = positions.columns[positions.columns.get_loc(lmidb.cash)]

    debug = False
    if debug:
        print(f"{positions.iloc[-1].to_frame().T}")

    # start of month, end of month, and end of previous month
    months = pd.concat([dtis, dtie, dtie.shift(1)], axis=1)[::-1]
    for i, m in months.iterrows():
        # print(f"m0:{m[0]}, m1:{m[1]}, m2:{m[2]}")
        if m[2] == None:
            break
        positions.loc[m[2]] = positions.loc[m[1]]

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

            # The sign is reversed on transactions because we are going backwards in time
            # Have verified this algo on jon-ira back to Dec 2021
            # fmt: off
            match action:
                case "Journal":
                    print("Handle journal")
                    sys.exit(-1)
                case "Journaled Shares":
                    print("Handle journal")
                    sys.exit(-1)
                case "Reinvest Shares":
                    positions.loc[m[2], symbol] -= quantity
                    positions.loc[m[2], cash] += amount
                case "Reinvestment Adj":
                    positions.loc[m[2], symbol] += quantity
                    positions.loc[m[2], cash] += amount
                case "Reinvest Dividend" | "Long Term Cap Gain Reinvest" | "Short Term Cap Gain Reinvest" | "Div Adjustment" | "Dividend Adj" | "Short Term Cap Gain" | "Long Term Cap Gain":
                    positions.loc[m[2], cash] += amount
                case "Buy":
                    positions.loc[m[2], symbol] -= quantity
                    positions.loc[m[2], cash] -= amount
                case "Sell":
                    positions.loc[m[2], symbol] += quantity
                    positions.loc[m[2], cash] -= amount
                case "Full Redemption" | "Full Redemption Adj":
                    positions.loc[m[2], symbol] -= quantity
                    positions.loc[m[2], cash] -= amount
                case "Security Transfer":
                    if symbol != "":
                        print(f"New condition for {action} on {t['date']}")
                    positions.loc[m[2], cash] -= amount
                case "Bank Interest" | "Cash Dividend" | "Bond Interest" | "Advisor Fee" | "Advisor Fee Adj" | "Special Dividend" | "Funds Received":
                    positions.loc[m[2], cash] -= amount
                case "MoneyLink Transfer" | "Wire Sent":
                    positions.loc[m[2], cash] -= amount
                case "Stock Split":
                    positions.loc[m[2], symbol] -= quantity
                case _:
                    print(f"Unhandled action: i:{i}")
                    print(f"  {t['date']} {action} {symbol} {quantity} {amount} {fees} {price}")
                    print(f"  Description: {t['description']}")
            # fmt: off
            if debug:
                print(f"{t['date']} {action}, {symbol}, {quantity}, {amount}, {fees}, {price}")
                ph = positions.loc[m[2]].to_frame().T
                print(f"{ph}")
    return positions


def analyze(account: dict[str, pd.DataFrame], closings) -> None:
    if len(account) == 0:
        return
    print(f"Processing {account['name']}")
    global transactions, positions
    transactions = account["transactions"]
    final_positions = account["positions"]

    positions = compute_positions(transactions, final_positions)

    years = transactions["Date"].apply(lambda x: x.year).unique()[::-1]
    for year in years:
        global y_start, y_end, ymask, ty, sym, price_start, prices_end, start_value, end_value
        y_start = pd.Timestamp(f"{year-1}-12-31").date()
        y_end = min(pd.Timestamp(f"{year}-12-31").date(), positions.index[-1])
        pct_of_year = (y_end - y_start).days / (pd.Timestamp(f"{year}-12-31").date() - y_start).days
        ymask = (transactions["Date"] > y_start) & (transactions["Date"] <= y_end)
        ty = transactions[ymask]

        # Make sure we have starting prices
        if y_start not in positions.index:
            continue

        # Really should make this track realized gains
        dividends = ty["Amount"][ty["Action"] == "Cash Dividend"].sum()
        advisor_fees = -ty["Amount"][ty["Action"] == "Advisor Fee"].sum()
        advisor_fees += -ty["Amount"][ty["Action"] == "Advisor Fee Adj"].sum()
        net_contributions = ty["Amount"][ty["Action"] == "MoneyLink Transfer"].sum()
        net_contributions += ty["Amount"][ty["Action"] == "Wire Sent"].sum()

        pstart = positions.loc[y_start]
        pstart = pstart[pstart > 0]
        cstart = closings.iloc[closings.index.searchsorted(y_start)]
        start_value = (cstart.loc[pstart.index] * pstart).sum()

        end_value = 0.0
        pend = positions.loc[y_end]
        pend = pend[pend > 0]
        cend = closings.iloc[closings.index.searchsorted(y_end, side="right") - 1]
        end_value = (cend.loc[pend.index] * pend).sum()

        # ROI Calculation
        if start_value > 0.0:
            roi = (end_value - start_value - net_contributions) / start_value
            roi_pct = 100 * roi
        else:
            roi_pct = float("NaN")

        print(f"Year: {year} through {y_end}")
        print(f"  Start Value: {start_value:.2f} End Value: {end_value:.2f}")
        print(f"  Dividends: {dividends:.2f}")
        print(f"  Net Contributions: {net_contributions:.2f}")
        print(f"  Advisors Fees: {advisor_fees.sum():.2f}")
        print(f"  ROI: {roi_pct:.2f}%, {roi_pct/pct_of_year:.2f}% annual")
        # print(f"Transactions:\n{ty}")


def summary(cursor: sqlite3.Cursor, account_filter: str | None = None) -> None:
    all_positions = compute_all_account_positions(cursor, account_filter)
    for account in all_positions.keys():
        print(f"Account: {account}")
        p = all_positions[account].iloc[0]
        print(p.to_frame().T)


def roi(cursor: sqlite3.Cursor) -> None:
    print("tbd")


def main():
    parser = argparse.ArgumentParser(description="ROI calculator", epilog="Use schwab account credentials if prompted for a login.")
    parser.add_argument(
        "--database", default=Path("lmi.db"), type=Path, metavar="fn", help="Name of database file. (default: %(default)s)"
    )
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
    args = parser.parse_args()

    pd.options.display.float_format = "{:.2f}".format
    pd.options.display.max_rows = None
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 1000)

    global conn
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
            all_positions = compute_all_account_positions(cursor, args.account)
            for account in all_positions.keys():
                print(f"Account: {account}")
                print(f"{all_positions[account]}")
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
