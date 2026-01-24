#!/usr/bin/env python3

import argparse
import datetime as dt
import pandas as pd
from pathlib import Path
import sqlite3
import sys
import types

import lmidb


def compute_account_positions(cursor: sqlite3.Cursor, account_id: int) -> pd.DataFrame:
    """Compute positions for given account for each month covered by the transactions."""

    # first get all tickers that have been in this account
    # next get the final positions for all tickers that have been in this account
    # propigate backwards the positions

    symbols = [s for s in transactions["Symbol"].unique() if s != ""]
    symbols.append(lmidb.cash)  # dummy symbol for cash

    fpd = final_positions.iloc[0]["Date"]
    ftd = transactions.iloc[0]["Date"]
    fd = max(fpd, ftd)
    sd = transactions.iloc[-1]["Date"]
    sd = dt.date(sd.year, sd.month, 1)
    dtis = pd.Series(pd.date_range(start=sd, end=fd, freq="MS").date)
    dtie = list(pd.date_range(start=sd, end=fd, freq="ME").date)
    if dtie[-1].month < fd.month:
        dtie.append(fd)
    dtie = pd.Series(dtie)
    positions = pd.DataFrame(index=dtie, columns=symbols)

    positions.loc[fd] = 0
    for i, r in final_positions.iterrows():
        symbol = r["Symbol"]
        if symbol == "Account Total":
            continue
        if symbol == "Cash & Cash Investments":
            symbol = lmidb.cash
            qty = fns_to_float(r["Mkt Val (Market Value)"])
        else:
            qty = fns_to_float(r["Qty (Quantity)"])
        positions.loc[fd, symbol] = qty

    # start of month, end of month, and end of previous month
    months = pd.concat([dtis, dtie, dtie.shift(1)], axis=1)[::-1]
    for i, m in months.iterrows():
        # print(f"m0:{m[0]}, m1:{m[1]}, m2:{m[2]}")
        if m[2] == None:
            break
        positions.loc[m[2]] = positions.loc[m[1]]
        t_month = transactions[(transactions["Date"] >= m[0]) & (transactions["Date"] <= m[1])]
        # print(f"transactions:\n{t_month}")

        for j, t in t_month.iterrows():
            action = t["Action"]
            symbol = t["Symbol"]
            quantity = float(t["Quantity"])
            amount = float(t["Amount"])
            p = positions.loc[m[2]]

            # The sign is reversed on transactions because we are going backwards in time
            # Have verified this algo on jon-ira back to Dec 2021
            if action == "Journal":
                print("Handle journal")
                sys.exit(-1)
            elif action == "Journaled Shares":
                print("Handle journal")
                sys.exit(-1)
            elif action == "Reinvest Shares":
                p[symbol] -= quantity
                p[cash] += amount
            elif action == "Reinvestment Adj":
                p[symbol] += quantity
                p[cash] += amount
            elif action in [
                "Reinvest Dividend",
                "Long Term Cap Gain Reinvest",
                "Short Term Cap Gain Reinvest",
                "Div Adjustment",
                "Dividend Adj",
                "Short Term Cap Gain",
                "Long Term Cap Gain",
            ]:
                p[cash] += amount
            elif action == "Buy":
                p[symbol] -= quantity
                p[cash] -= amount
            elif action == "Sell":
                p[symbol] += quantity
                p[cash] -= amount
            elif action in [
                "Bank Interest",
                "Cash Dividend",
                "Bond Interest",
                "Advisor Fee",
                "Advisor Fee Adj",
                "Special Dividend",
            ]:
                p[cash] -= amount
            elif action in ["MoneyLink Transfer", "Wire Sent"]:
                p[cash] += amount
            else:
                print(f"Unhandled action: i:{i}, action:{action}")

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


def statement(cursor: sqlite3.Cursor) -> None:
    print("tbd")


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
    parser.add_argument(
        "action",
        type=str,
        choices=["update-db", "dump-db", "statement", "roi", "update-candles"],
        help="Action to take.",
    )
    args = parser.parse_args()

    pd.options.display.float_format = "{:.2f}".format
    pd.options.display.width = None  # type: ignore
    pd.options.display.max_rows = None

    global conn
    fn = args.database
    if fn == Path(""):
        conn = sqlite3.connect(":memory:")
    else:
        conn = sqlite3.connect(fn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    match args.action:
        case "dump-db":
            lmidb.dump_summary(cursor)
        case "update-db":
            lmidb.update_db(cursor, args.schwab_data)
        case "update-candles":
            lmidb.update_candles(cursor)
        case "statement":
            statement(cursor)
        case "roi":
            roi(cursor)
        case _:
            print("inconcievable")


if __name__ == "__main__":
    main()
