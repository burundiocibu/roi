#!/usr/bin/env -S uv run
"""Price-based performance report for tickers currently held across accounts."""

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

import lmidb


def get_held_tickers(cursor: sqlite3.Cursor, account_filters: list[str] | None) -> tuple[set[str], set[str], list[int], dict[str, dt.datetime]]:
    """Return (equity_tickers, other_tickers, account_ids, first_held_dates)."""
    if account_filters:
        placeholders = ",".join("?" * len(account_filters))
        cursor.execute(
            f"SELECT id FROM accounts WHERE name IN ({placeholders})",
            account_filters,
        )
    else:
        cursor.execute("SELECT id FROM accounts")
    account_ids = [row[0] for row in cursor.fetchall()]

    symbols: set[str] = set()
    for acct_id in account_ids:
        cursor.execute(
            f"""
            SELECT DISTINCT symbol FROM positions_{acct_id}
            WHERE date = (SELECT MAX(date) FROM positions_{acct_id})
            AND quantity != 0
            """
        )
        for row in cursor.fetchall():
            sym = row[0]
            if sym and sym != lmidb.cash:
                symbols.add(sym)

    if not symbols:
        return set(), set(), account_ids, {}

    placeholders = ",".join("?" * len(symbols))
    cursor.execute(
        f"""
        SELECT t.symbol, at.asset_type
        FROM tickers t
        LEFT JOIN asset_types at ON t.asset_type_id = at.id
        WHERE t.symbol IN ({placeholders})
        """,
        list(symbols),
    )
    equity_tickers: set[str] = set()
    other_tickers: set[str] = set()
    for row in cursor.fetchall():
        sym, asset_type = row[0], row[1]
        if asset_type == "EQUITY":
            equity_tickers.add(sym)
        else:
            other_tickers.add(sym)

    # Find the first date each ticker appeared in any account's positions
    first_held: dict[str, dt.datetime] = {}
    for sym in symbols:
        earliest = None
        for acct_id in account_ids:
            cursor.execute(
                f"SELECT MIN(date) FROM positions_{acct_id} WHERE symbol = ? AND quantity != 0",
                (sym,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                d = dt.datetime.fromisoformat(row[0])
                if d.tzinfo is not None:
                    d = d.replace(tzinfo=None)
                if earliest is None or d < earliest:
                    earliest = d
        if earliest:
            first_held[sym] = earliest

    return equity_tickers, other_tickers, account_ids, first_held


def build_period_index(cursor: sqlite3.Cursor, interval: str) -> pd.DatetimeIndex:
    """Return period-end dates from earliest candle to today."""
    cursor.execute("SELECT MIN(date) FROM candles")
    row = cursor.fetchone()
    if not row or not row[0]:
        return pd.DatetimeIndex([])
    first_date = dt.datetime.fromisoformat(row[0])
    if first_date.tzinfo is not None:
        first_date = first_date.replace(tzinfo=None)
    today = dt.datetime.today()
    if interval == "all":
        # Fall back to monthly for 'all'
        return pd.date_range(start=first_date, end=today, freq="ME")
    return pd.date_range(start=first_date, end=today, freq=interval)


def compute_returns(cursor: sqlite3.Cursor, all_symbols: list[str], index: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (returns, prices) DataFrames (index=dates, columns=symbols). Returns are period price return %."""
    prices = pd.DataFrame(index=index, columns=all_symbols, dtype=float)

    lmidb.load_candles_cache(cursor)
    for date in index:
        closing = lmidb.get_closing_values(cursor, all_symbols, date)
        for sym in all_symbols:
            prices.loc[date, sym] = closing.get(sym, float("nan"))

    returns = pd.DataFrame(index=index, columns=all_symbols, dtype=float)
    returns.iloc[0] = 0.0
    for i in range(1, len(index)):
        prev_prices = prices.iloc[i - 1]
        curr_prices = prices.iloc[i]
        for sym in all_symbols:
            p0, p1 = prev_prices[sym], curr_prices[sym]
            if pd.notna(p0) and pd.notna(p1) and p0 != 0:
                returns.loc[index[i], sym] = (p1 - p0) / p0 * 100
            else:
                returns.loc[index[i], sym] = float("nan")

    return returns, prices


def main():
    parser = argparse.ArgumentParser(description="Price-based performance report for held tickers.")
    parser.add_argument("--database", default=Path("lmi.db"), type=Path, metavar="fn")
    parser.add_argument(
        "-i", "--interval",
        default="ME",
        choices=["ME", "QE", "YE", "all"],
        help="Reporting interval (default: ME)",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        type=str,
        help="Filter to account name(s). Can be repeated.",
    )
    parser.add_argument(
        "-l", "--limit",
        type=int,
        default=None,
        help="Limit the report to N intervals back",
    )
    parser.add_argument("-v", "--verbosity", action="count", default=0)
    args = parser.parse_args()

    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    equity_tickers, other_tickers, _account_ids, first_held = get_held_tickers(cursor, args.account)
    all_symbols = sorted(equity_tickers) + sorted(other_tickers)

    if not all_symbols:
        print("No tickers found.")
        return

    index = build_period_index(cursor, args.interval)
    if len(index) == 0:
        print("No candle data found.")
        return

    returns, _prices = compute_returns(cursor, all_symbols, index)

    # Collapse equity tickers to equal-weighted "Equities" row
    result = pd.DataFrame(index=index, dtype=float)
    eq_cols = [s for s in all_symbols if s in equity_tickers]
    if eq_cols:
        result["Equities"] = returns[eq_cols].median(axis=1)
    for sym in sorted(other_tickers):
        result[sym] = returns[sym]

    # Transpose: rows = asset types, columns = dates
    display = result.T
    display.columns = [d.date() for d in display.columns]

    # Slice to displayed window first, then compute Total from those columns only
    limit = args.limit
    if limit is None and args.verbosity == 0:
        limit = 12

    if limit is not None:
        display = display.iloc[:, -limit:]

    display["Total"] = display.apply(
        lambda row: ((1 + row.fillna(0) / 100).prod() - 1) * 100, axis=1
    )
    display = display.sort_values("Total", ascending=False)

    pd.options.display.float_format = "{:+.2f}".format
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 1000)

    print(display)


if __name__ == "__main__":
    main()
