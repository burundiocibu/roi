#!/usr/bin/env -S uv run

import argparse
import cProfile
import datetime as dt
import functools
import inspect
import io
import pandas as pd
from pathlib import Path
import pstats
import sqlite3
import time

import lmidb


class ProfilingCursor:
    """Wrapper around sqlite3.Cursor that tracks query execution times."""

    def __init__(self, cursor: sqlite3.Cursor, enable_profiling: bool = False):
        """Wrap a sqlite3 cursor to optionally record per-query timing statistics.

        Args:
            cursor: The underlying sqlite3 cursor to delegate to.
            enable_profiling: When True, records timing for every execute() call.
        """
        self._cursor = cursor
        self._enable_profiling = enable_profiling
        self.query_stats = {}  # {query_pattern: {"count": int, "total_time": float, "max_time": float}}

    def __getattr__(self, name):
        """Delegate all other attributes to the underlying cursor."""
        return getattr(self._cursor, name)

    def __iter__(self):
        """Delegate iteration to the underlying cursor."""
        return iter(self._cursor)

    def execute(self, sql, parameters=()):
        """Execute a SQL statement, recording elapsed time when profiling is enabled.

        Args:
            sql: SQL statement to execute.
            parameters: Bind parameters for the statement (default: empty tuple).
        Returns:
            The result of the underlying cursor.execute() call.
        """
        if self._enable_profiling:
            start_time = time.perf_counter()
            result = self._cursor.execute(sql, parameters)
            elapsed = time.perf_counter() - start_time

            # Create a pattern key (normalize the SQL for grouping)
            # Strip whitespace and limit length for readability
            query_pattern = " ".join(sql.split())[:100]

            if query_pattern not in self.query_stats:
                self.query_stats[query_pattern] = {"count": 0, "total_time": 0.0, "max_time": 0.0}

            self.query_stats[query_pattern]["count"] += 1
            self.query_stats[query_pattern]["total_time"] += elapsed
            self.query_stats[query_pattern]["max_time"] = max(self.query_stats[query_pattern]["max_time"], elapsed)

            # Log slow queries immediately (> 0.1 seconds)
            if elapsed > 0.1:
                print(f"[SLOW QUERY] {elapsed:.4f}s: {query_pattern}")

            return result
        else:
            return self._cursor.execute(sql, parameters)

    def print_stats(self):
        """Print query statistics sorted by total time."""
        if not self.query_stats:
            return

        print("\n" + "=" * 100)
        print("DATABASE QUERY PROFILING RESULTS")
        print("=" * 100)

        # Sort by total time descending
        sorted_queries = sorted(self.query_stats.items(), key=lambda x: x[1]["total_time"], reverse=True)

        total_time = sum(stats["total_time"] for _, stats in sorted_queries)
        total_count = sum(stats["count"] for _, stats in sorted_queries)

        print(f"\nTotal Queries: {total_count}")
        print(f"Total Query Time: {total_time:.4f} seconds\n")
        print(f"{'Count':<8} {'Total(s)':<10} {'Avg(s)':<10} {'Max(s)':<10} {'%':<6} Query")
        print("-" * 100)

        for query_pattern, stats in sorted_queries[:30]:  # Top 30 queries
            count = stats["count"]
            total = stats["total_time"]
            avg = total / count
            max_time = stats["max_time"]
            pct = (total / total_time * 100) if total_time > 0 else 0

            print(f"{count:<8} {total:<10.4f} {avg:<10.6f} {max_time:<10.6f} {pct:<6.1f} {query_pattern}")


def timeit(func):
    """Decorator to measure function execution time."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        import os

        enable_timing = os.environ.get("PROFILE", "0") == "1"

        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        elapsed = end_time - start_time

        if enable_timing:
            print(f"[TIMING] {func.__name__}: {elapsed:.4f} seconds")

        return result

    return wrapper


@timeit
def compute_all_history(cursor: sqlite3.Cursor, args: argparse.Namespace) -> dict:
    """Load history for every account matching the account filter, returning a combined dict.

    Args:
        cursor: Database cursor used for all queries.
        args: Parsed CLI arguments (uses args.account, args.debug, and others passed through).
    Returns:
        Dict keyed by account name, each value being the history dict from compute_history().
    """
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

    cursor.execute("""
        SELECT t.symbol FROM tickers t
        JOIN asset_types at ON t.asset_type_id = at.id
        WHERE at.asset_type = 'EQUITY'
        """)
    equity_tickers = {row[0] for row in cursor.fetchall()}

    all_history = {}

    for account in accounts:
        if args.debug:
            print(f"Computing history for {account["name"]}")
        all_history[account["name"]] = compute_history(cursor, account["id"], equity_tickers, args)

    return all_history


@timeit
def compute_history(cursor: sqlite3.Cursor, account_id: int, equity_tickers: set[str], args: argparse.Namespace) -> dict:
    """Compute full financial history for one account by walking transactions backward to derive positions, then forward for cost basis.

    Args:
        cursor: Database cursor used for all queries.
        account_id: Primary key of the account in the accounts table.
        equity_tickers: Set of ticker symbols classified as EQUITY (used for Equities aggregation).
        args: Parsed CLI arguments.
    Returns:
        Dict with keys: positions, stg, ltg, income, fees, distributions, cost_basis, value, cumulative_roi —
        each a DataFrame indexed by period-end date.
    """
    account_name = lmidb.get_account_name(cursor, account_id)

    cursor.execute(f"SELECT MIN(Date) as first_date, MAX(Date) as last_date FROM transactions_{account_id}")
    result = cursor.fetchone()
    first_transaction_date = dt.datetime.fromisoformat(result["first_date"])

    # Get latest positions, optionally filtered by ticker
    if args.ticker:
        cursor.execute(
            f"""
            SELECT * FROM positions_{account_id} 
            WHERE Date = (SELECT MAX(Date) FROM positions_{account_id})
            AND Symbol = ?
        """,
            (args.ticker,),
        )
    else:
        cursor.execute(f"""
            SELECT * FROM positions_{account_id} 
            WHERE Date = (SELECT MAX(Date) FROM positions_{account_id})
        """)
    latest_position_df = pd.DataFrame([dict(row) for row in cursor])

    if len(latest_position_df) > 0:
        latest_position_date = dt.datetime.fromisoformat(latest_position_df["date"][0])
    else:
        # No position data for this ticker, use transaction date range
        cursor.execute(f"SELECT MAX(Date) as last_date FROM transactions_{account_id}")
        result = cursor.fetchone()
        latest_position_date = dt.datetime.fromisoformat(result["last_date"])

    # start dataframe with last day of the month previous to the last transaction
    # end with the most recent position
    start_date = dt.date(first_transaction_date.year, first_transaction_date.month, 1) - dt.timedelta(days=1)
    period_dates = list(pd.date_range(start=start_date, end=latest_position_date, freq="ME").date)
    if period_dates[-1] < latest_position_date.date():
        period_dates.append(latest_position_date.date())
    period_dates = pd.Series(period_dates)

    if args.ticker:
        symbols = [args.ticker, lmidb.cash]  # Always include cash
    else:
        symbols = lmidb.get_securities_in_account(cursor, account_id)
        # Also include any symbols present in the latest positions snapshot
        # that don't yet have transactions (e.g. newly transferred-in holdings)
        position_symbols = set(latest_position_df["symbol"]) if len(latest_position_df) > 0 else set()
        new_symbols = sorted(position_symbols - set(symbols))
        if new_symbols:
            cash_idx = symbols.index(lmidb.cash)
            symbols = symbols[:cash_idx] + new_symbols + symbols[cash_idx:]
    positions = pd.DataFrame(index=period_dates, columns=symbols)

    # initialize the end of positions dataframe with the latest position data
    positions[:] = 0.0
    for i, r in latest_position_df.iterrows():
        positions.loc[period_dates.iloc[-1], r["symbol"]] = r["quantity"]

    cash = positions.columns[positions.columns.get_loc(lmidb.cash)]

    short_term_gains = pd.DataFrame(index=period_dates, columns=symbols)
    short_term_gains[:] = 0.0
    long_term_gains = pd.DataFrame(index=period_dates, columns=symbols)
    long_term_gains[:] = 0.0
    income = pd.DataFrame(index=period_dates, columns=symbols)
    income[:] = 0
    mgmt_fees = pd.Series(index=period_dates)
    mgmt_fees[:] = 0
    distributions = pd.Series(index=period_dates)
    distributions[:] = 0
    for m in period_dates[:0:-1]:
        som = dt.date(m.year, m.month, 1)
        month = som - dt.timedelta(days=1)
        positions.loc[month] = positions.loc[m]  # type: ignore

        # Get transactions for the month after month, optionally filtered by ticker
        if args.ticker:
            cursor.execute(
                f"SELECT * FROM transactions_{account_id} WHERE Date >= ? AND Date <= ? AND (Symbol = ? OR Symbol = '') ORDER BY Date DESC",
                (som.isoformat(), m.isoformat(), args.ticker),
            )
        else:
            cursor.execute(
                f"SELECT * FROM transactions_{account_id} WHERE Date >= ? AND Date <= ? ORDER BY Date DESC",
                (som.isoformat(), m.isoformat()),
            )

        for t in cursor:
            action = t["Action"]
            symbol = t["Symbol"]
            quantity = float(t["Quantity"])
            amount = float(t["Amount"])
            fees = float(t["fees"])
            price = float(t["price"])
            # If price is zero, assume it has a value of 1
            tdate = t["date"][:10]

            # Skip non-ticker transactions when focusing on a single ticker
            if args.ticker and symbol != args.ticker:
                continue
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
            match action:
                case "Advisor Fee" | "ADR Mgmt Fee" | "ADR Mgmt Fee Adj":
                    # these are negative to start with
                    positions.loc[month, cash] -= amount  # type: ignore
                    mgmt_fees[month] -= amount  # type: ignore
                case "Advisor Fee Adj":
                    positions.loc[month, cash] -= amount  # type: ignore
                    mgmt_fees[month] -= amount  # type: ignore
                case "Bank Interest" | "Cash In Lieu":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, cash] += amount  # type: ignore
                case "Bond Interest":
                    positions.loc[month, cash] += amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Buy":
                    positions.loc[month, symbol] -= quantity  # type: ignore
                    positions.loc[month, cash] -= amount  # type: ignore
                case "Cash Dividend" | "Non-Qualified Div":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Div Adj" | "Dividend Adj" | "Div Adjustment":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Foreign Tax Paid" | "Foreign Tax Reclaim Adj":
                    positions.loc[month, cash] -= amount  # type: ignore
                case "Full Redemption":
                    positions.loc[month, symbol] -= amount  # type: ignore
                case "Full Redemption Adj" | "CXL Redemption Adj" | "Final Cash Liquid":
                    positions.loc[month, cash] -= amount  # type: ignore
                case "Redemption Adj" | "Final Cash Liquid Adj":
                    positions.loc[month, symbol] -= quantity  # type: ignore
                case "Funds Received":
                    positions.loc[month, cash] -= amount  # type: ignore
                    distributions.loc[month] -= amount  # type: ignore
                case "Journal" | "Journaled Shares":
                    positions.loc[month, cash] -= amount  # type: ignore
                    if symbol != "":
                        positions.loc[month, symbol] -= quantity  # type: ignore
                case "Long Term Cap Gain":
                    positions.loc[month, cash] -= amount  # type: ignore
                    long_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Long Term Cap Gain Reinvest":
                    positions.loc[month, cash] -= amount  # type: ignore
                    long_term_gains.loc[month, symbol] += amount  # type: ignore
                case "MoneyLink Transfer":
                    positions.loc[month, cash] -= amount  # type: ignore
                    distributions[month] -= amount  # type: ignore
                case "Pr Yr Cash Div":
                    positions.loc[month, cash] -= amount  # type: ignore
                case "Qualified Dividend" | "Special Qual Div":
                    positions.loc[month, cash] -= amount  # type: ignore
                    long_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Reinvest Dividend":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                case "Reinvest Shares":
                    positions.loc[month, cash] -= amount  # type: ignore
                    positions.loc[month, symbol] -= quantity  # type: ignore
                case "Reinvestment Adj":
                    positions.loc[month, symbol] -= quantity  # type: ignore
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                case "Security Transfer":
                    positions.loc[month, cash] -= amount  # type: ignore
                    if symbol != "":
                        positions.loc[month, symbol] -= quantity  # type: ignore
                case "Sell":
                    positions.loc[month, symbol] += quantity  # type: ignore
                    positions.loc[month, cash] -= amount  # type: ignore
                    long_term_gains.loc[month, symbol] += amount  # type: ignore
                case "Short Term Cap Gain":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Short Term Cap Gain Reinvest":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                case "Special Dividend":
                    positions.loc[month, cash] -= amount  # type: ignore
                    short_term_gains.loc[month, symbol] += amount  # type: ignore
                    income.loc[month, symbol] += amount  # type: ignore
                case "Stock Split" | "Stock Merger":
                    positions.loc[month, symbol] -= quantity  # type: ignore
                    # For stock splits, cost basis total remains same
                    # but we need to track the quantity change
                case "Wire Sent":
                    positions.loc[month, cash] -= amount  # type: ignore
                    distributions[month] -= amount  # type: ignore
                case _:
                    print(
                        f"Unhandled action in {account_name}, {tdate}: A:{action}, S:{symbol}, Q:{quantity}, A:{amount}, F:{fees}, P:{price}"
                    )
            if args.debug:
                if symbol != "":
                    print(f"{symbol}:{positions.loc[month, symbol]:.2f}, {cash}:{positions.loc[month, cash]:.2f}")
                else:
                    print(f"{cash}:{positions.loc[month, cash]:.2f}")
    # Snap floating-point noise to zero (backward computation can leave tiny residuals)
    positions = positions.map(lambda x: 0.0 if abs(x) < 1e-9 else x)

    # Compute market value for each position first
    value = compute_value(cursor, symbols, positions)

    # Compute cost basis forward in time (needs positions for quantity tracking)
    # Pass value DataFrame to avoid redundant database lookups
    cost_basis = compute_cost_basis(cursor, account_id, symbols, positions, value, args)

    # resample the history on the indicated interval
    if args.interval != "ME":
        interval = args.interval

        def _rs(df, agg):
            d = df.copy()
            d.index = pd.to_datetime(d.index)
            return getattr(d.resample(interval), agg)()

        positions = _rs(positions, "last")
        short_term_gains = _rs(short_term_gains, "sum")
        long_term_gains = _rs(long_term_gains, "sum")
        income = _rs(income, "sum")
        mgmt_fees = _rs(mgmt_fees, "sum")
        distributions = _rs(distributions, "sum")
        cost_basis = _rs(cost_basis, "last")
        value = _rs(value, "last")

    if not args.all:
        # Only include securities still in account
        last_row = positions.iloc[-1]
        lpi = last_row[last_row != 0].index
        positions = positions[lpi]
        short_term_gains = short_term_gains[lpi]
        long_term_gains = long_term_gains[lpi]
        income = income[lpi]
        cost_basis = cost_basis[lpi]
        value = value[lpi]

    # Collapse individual EQUITY-type tickers into a single "Equities" aggregate column
    if args.equity_sum and not args.ticker:

        def _collapse_equities(df):
            eq_cols = [c for c in df.columns if c in equity_tickers]
            if not eq_cols:
                return df
            equities = df[eq_cols].sum(axis=1).rename("Equities")
            return pd.concat([df.drop(columns=eq_cols), equities], axis=1)

        value = _collapse_equities(value)
        cost_basis = _collapse_equities(cost_basis)
        income = _collapse_equities(income)
        short_term_gains = _collapse_equities(short_term_gains)
        long_term_gains = _collapse_equities(long_term_gains)
        positions = _collapse_equities(positions)

    # Add Total column to value after filtering
    value["Total"] = value.sum(axis=1)

    # Add Total column to cost_basis after filtering
    cost_basis["Total"] = cost_basis.sum(axis=1)

    # Add Total column to income after filtering
    income["Total"] = income.sum(axis=1)

    # Compute cumulative ROI directly from pre-computed value and cost_basis
    cumulative_roi = compute_cumulative_roi(value, cost_basis, income, args)

    # Apply date range filter after cumulative_roi so cumsum(income) uses the full history
    def _clip(df):
        idx = pd.DatetimeIndex(df.index)
        mask = pd.Series(True, index=df.index)
        if args.start_date:
            mask &= idx >= pd.Timestamp(args.start_date)
        if args.end_date:
            mask &= idx <= pd.Timestamp(args.end_date)
        return df[mask.values]

    history = {
        "positions": _clip(positions),
        "stg": _clip(short_term_gains),
        "ltg": _clip(long_term_gains),
        "income": _clip(income),
        "fees": _clip(mgmt_fees),
        "distributions": _clip(distributions),
        "cost_basis": _clip(cost_basis),
        "value": _clip(value),
        "cumulative_roi": _clip(cumulative_roi),
    }

    return history


@timeit
def compute_cost_basis(
    cursor: sqlite3.Cursor,
    account_id: int,
    symbols: list[str],
    positions: pd.DataFrame,
    value: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    """Compute cost basis forward in time using average-cost accounting; sells reduce basis proportionally.

    Args:
        cursor: Database cursor used for transaction and candle queries.
        account_id: Primary key of the account in the accounts table.
        symbols: Ordered list of ticker symbols (including cash) to track.
        positions: DataFrame of share quantities indexed by period-end date (from compute_history).
        value: Pre-computed market value DataFrame used to seed cost basis for pre-existing holdings.
        args: Parsed CLI arguments.
    Returns:
        DataFrame of cost basis values with the same index and columns as positions.
    """
    if args.debug:
        print(f"Computing cost basis for account {account_id}")
    period_dates = positions.index
    cost_basis = pd.DataFrame(index=period_dates, columns=symbols)
    cost_basis[:] = 0.0

    # Get all transactions in chronological order (forward in time)
    # Filter to only the specified ticker if provided
    if args.ticker:
        cursor.execute(f"SELECT * FROM transactions_{account_id} WHERE Symbol = ? OR Symbol = '' ORDER BY Date ASC", (args.ticker,))
    else:
        cursor.execute(f"SELECT * FROM transactions_{account_id} ORDER BY Date ASC")
    transactions = cursor.fetchall()

    # Track running cost basis for each security
    # Quantities come from the positions DataFrame
    running_cost_basis = {symbol: 0.0 for symbol in symbols}

    # Initialize cost basis for securities held at the start of the period
    # For these, we use the market value at the first month-end as initial cost basis
    first_month_end = period_dates[0]
    for symbol in symbols:
        qty_at_start = positions.loc[first_month_end, symbol]
        if qty_at_start > 0:  # type: ignore
            # This security was held before transaction data starts
            # Use market value at start as initial cost basis (from pre-computed value)
            initial_value = value.loc[first_month_end, symbol]
            running_cost_basis[symbol] = initial_value  # type: ignore
            if args.ticker and symbol == args.ticker and args.debug:
                price = initial_value / qty_at_start if qty_at_start > 0 else 0  # type: ignore
                print(f"Initializing cost basis for {symbol}: {qty_at_start:.2f} shares @ ${price:.2f} = ${initial_value:.2f}")

    transaction_idx = 0

    for month_end in period_dates:
        # Process all transactions up to and including this month end
        while transaction_idx < len(transactions):
            t = transactions[transaction_idx]
            tdate = dt.datetime.fromisoformat(t["date"][:10]).date()

            if tdate > month_end:
                break

            action = t["Action"]
            symbol = t["Symbol"]
            quantity = float(t["Quantity"])
            price = float(t["price"])

            # 9-char symbols are CUSIPs (fixed-income securities) with no candle data; use price=1
            if len(symbol) == 9:
                price = 1

            if symbol in symbols:
                old_cb = running_cost_basis[symbol]

                # Get quantity from positions DataFrame (computed backwards in compute_history)
                # Look up the position at the current month_end
                qty_at_month_end = positions.loc[month_end, symbol]

                # fmt: off
                match action:
                    case "Buy":
                        running_cost_basis[symbol] += quantity * price
                    case "Journaled Shares":
                        if price == 0:
                            candle_price = lmidb.get_closing_values(cursor, [symbol], tdate)[symbol]
                            running_cost_basis[symbol] += quantity * candle_price
                        else:
                            running_cost_basis[symbol] += quantity * price
                    case "Reinvest Shares":
                        running_cost_basis[symbol] += quantity * price
                    case "Reinvestment Adj":
                        running_cost_basis[symbol] += quantity * price
                    case "Security Transfer":
                        if price == 0:
                            candle_price = lmidb.get_closing_values(cursor, [symbol], tdate)[symbol]
                            running_cost_basis[symbol] += quantity * candle_price
                        else:
                            running_cost_basis[symbol] += quantity * price
                    case "Sell":
                        # For sells, we need the quantity BEFORE the sell
                        # Since positions are at month-end, we need qty + sell_quantity
                        qty_before_sell = qty_at_month_end + quantity
                        if qty_before_sell > 0 and running_cost_basis[symbol] > 0:
                            avg_cost_per_share = running_cost_basis[symbol] / qty_before_sell
                            running_cost_basis[symbol] -= quantity * avg_cost_per_share
                        elif args.debug:
                            print(f"Warning: Sell with no shares held for {symbol} on {tdate}")
                    case "Full Redemption":
                        running_cost_basis[symbol] = 0.0
                    case "Redemption Adj" | "Final Cash Liquid Adj":
                        redeemed_qty = abs(quantity)
                        qty_before = qty_at_month_end + redeemed_qty
                        if qty_before > 0 and running_cost_basis[symbol] > 0:
                            running_cost_basis[symbol] -= (redeemed_qty / qty_before) * running_cost_basis[symbol]
                    case "Stock Split" | "Non-Qualified Div":
                        # These don't change cost basis
                        pass
                # fmt: on

                # Debug output for ticker if specified
                if args.ticker and symbol == args.ticker and running_cost_basis[symbol] != old_cb:
                    if args.debug:
                        avg_cost = running_cost_basis[symbol] / qty_at_month_end if qty_at_month_end > 0 else 0
                        print(
                            f"{tdate}: {action:25} Q:{quantity:8.2f} P:{price:8.2f} Qty@MonthEnd: {qty_at_month_end:7.2f} CB: {old_cb:10.2f} -> {running_cost_basis[symbol]:10.2f} (Avg: ${avg_cost:.2f})"
                        )

            transaction_idx += 1

        # Record the cost basis at this month end
        for symbol in symbols:
            cost_basis.loc[month_end, symbol] = running_cost_basis[symbol]

    return cost_basis


@timeit
def compute_value(cursor: sqlite3.Cursor, symbols: list[str], positions: pd.DataFrame) -> pd.DataFrame:
    """Compute market value (quantity × closing price) for each security at each period-end date.

    Args:
        cursor: Database cursor used for candle price lookups.
        symbols: Ordered list of ticker symbols to value.
        positions: DataFrame of share quantities indexed by period-end date.
    Returns:
        DataFrame with the same index and columns as positions, containing dollar market values.
    """
    period_dates = positions.index
    value = pd.DataFrame(index=period_dates, columns=symbols)
    value[:] = 0.0

    # Calculate market value for each date
    for date_idx in period_dates:
        # Get closing prices for this date
        closings = lmidb.get_closing_values(cursor, list(symbols), date_idx)

        for symbol in symbols:
            quantity = positions.loc[date_idx, symbol]
            # Market value = quantity * closing price
            value.loc[date_idx, symbol] = quantity * closings[symbol]

    return value


@timeit
def compute_cumulative_roi(
    value_df: pd.DataFrame,
    cost_basis_df: pd.DataFrame,
    income_df: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    """Compute cumulative ROI (%) as (value + cumulative_income - cost_basis) / cost_basis × 100.

    Args:
        value_df: Market value DataFrame indexed by period-end date (excludes cash and Total columns).
        cost_basis_df: Cost basis DataFrame with matching index and columns.
        income_df: Per-period income DataFrame (dividends, interest, etc.) with matching index and columns.
        args: Parsed CLI arguments.
    Returns:
        DataFrame of ROI percentages with the same index; zero where cost basis is zero or None.
    """
    if args.debug:
        print(f"compute_cumulative_roi")
    symbols = [c for c in value_df.columns if c not in (lmidb.cash, "Total")]
    roi_df = pd.DataFrame(index=value_df.index, columns=symbols, dtype=float)
    roi_df[:] = 0.0
    for symbol in symbols:
        cb = cost_basis_df[symbol]
        val = value_df[symbol]
        cum_income = pd.to_numeric(income_df[symbol], errors="coerce").fillna(0.0).cumsum()
        cb_num = pd.to_numeric(cb, errors="coerce").replace(0, float("nan"))
        roi_df[symbol] = ((pd.to_numeric(val, errors="coerce") + cum_income - cb_num) / cb_num * 100).fillna(0.0)
    return roi_df


def cost_basis(all_history: dict) -> None:
    """Print the cost basis DataFrame for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        print(f"Cost basis for {account}:\n{history['cost_basis']}")


def show_positions(all_history: dict) -> None:
    """Print the share quantity positions DataFrame for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        print(f"Positions for {account}:\n{history['positions']}")


def show_value(all_history: dict) -> None:
    """Print the market value DataFrame for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        print(f"Market value for {account}:\n{history['value']}")


def income(all_history: dict) -> None:
    """Print the income (dividends, interest, capital gains distributions) DataFrame for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        print(f"Income for {account}:\n{history['income']}")


def summary(all_history: dict) -> None:
    """Print first and last period values, total cost basis, gain/loss, and overall ROI for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        positions = history["positions"]
        value = history["value"]
        cost_basis_data = history["cost_basis"]

        print(f"\nSummary for {account}:")
        print("\nFirst Period Value:")
        print(value.iloc[0].to_frame().T)
        print(f"Total Value: ${value.iloc[0]['Total']:.2f}")

        print("\nMost Recent Period Value:")
        print(value.iloc[-1].to_frame().T)
        print(f"Total Value: ${value.iloc[-1]['Total']:.2f}")
        print(f"Total Cost Basis: ${cost_basis_data.iloc[-1]['Total']:.2f}")

        total_gain = value.iloc[-1]["Total"] - cost_basis_data.iloc[-1]["Total"]
        total_roi = (total_gain / cost_basis_data.iloc[-1]["Total"] * 100) if cost_basis_data.iloc[-1]["Total"] != 0 else 0.0
        print(f"Total Gain/Loss: ${total_gain:.2f}")
        print(f"Total ROI: {total_roi:.2f}%")


def cumulative_roi(all_history: dict, args: argparse.Namespace) -> None:
    """Print cumulative ROI (%) for each security; with --ticker shows a per-period detail table including price and cost basis.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
        args: Parsed CLI arguments.
    """
    for account, history in all_history.items():
        positions = history["positions"]
        cost_basis_data = history["cost_basis"]
        value_data = history["value"]
        roi_df = history["cumulative_roi"]

        if args.ticker:
            ticker = args.ticker

            if ticker not in positions.columns:
                continue

            market_value = value_data[ticker] if ticker in value_data.columns else pd.Series(0.0, index=positions.index)
            closing_prices = (market_value / positions[ticker]).replace([float("inf"), -float("inf")], 0.0).fillna(0.0)

            detail_df = pd.DataFrame(
                {
                    "Quantity": positions[ticker],
                    "Price": closing_prices,
                    "Cost Basis": cost_basis_data[ticker],
                    "Market Value": market_value,
                    "ROI %": roi_df[ticker],
                }
            )
            detail_df = detail_df[detail_df["Quantity"] != 0]
            print(f"Account: {account} - Ticker: {ticker} cumulative ROI (%)")
            print(detail_df)
        else:
            print(f"Account: {account} cumulative ROI (%)")
            if args.verbosity > 0:
                print(roi_df)
            else:
                print(roi_df.iloc[[-1]])
        print("")


def annual_roi(all_history: dict, args: argparse.Namespace) -> None:
    """Print annualized (CAGR) ROI (%) for each security, calculated from first date held to each period-end. most-recent row only unless -v.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
        args: Parsed CLI arguments.
    """
    for account, history in all_history.items():
        positions = history["positions"]
        cost_basis_data = history["cost_basis"]
        cumulative_roi = history["cumulative_roi"]

        result = pd.DataFrame(index=positions.index, columns=cumulative_roi.columns, dtype=float)
        result[:] = 0.0

        value_df = history["value"]

        for symbol in cumulative_roi.columns:
            if symbol == "Total":
                continue
            if symbol not in cost_basis_data.columns:
                continue
            cb_series = cost_basis_data[symbol]
            held = cb_series[cb_series > 0]
            if len(held) == 0:
                continue
            first_date = held.index[0]

            for date_idx in positions.index:
                days_held = (date_idx - first_date).days
                if days_held <= 0:
                    continue
                roi_pct = cumulative_roi.loc[date_idx, symbol]
                roi_factor = 1 + roi_pct / 100
                if roi_factor > 0:
                    result.loc[date_idx, symbol] = (roi_factor ** (365.25 / days_held) - 1) * 100

        print(f"Account: {account} annualized ROI (%)")
        if args.verbosity > 0:
            print(result)
        else:
            print(result.iloc[[-1]])
        print("")


def fees(all_history: dict) -> None:
    """Print advisor/management fees Series for each account.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
    """
    for account, history in all_history.items():
        print(f"Fees for: {account}")
        print(f"{history["fees"]}\n")


def full_report(all_history: dict, args: argparse.Namespace) -> None:
    """Print positions, cost basis, market value, income, and ROI for each account; most-recent row only unless -v.

    Args:
        all_history: Dict keyed by account name containing history dicts from compute_history().
        args: Parsed CLI arguments.
    """
    for account, history in all_history.items():
        print(f"Full Report for: {account}")

        positions = history["positions"]
        value = history["value"]
        cost_basis_data = history["cost_basis"]
        roi_df = history["cumulative_roi"]
        income_data = history["income"]

        # If showing a single ticker, combine all metrics into one dataframe
        if args.ticker:
            ticker = args.ticker
            if ticker not in positions.columns:
                continue
            # Create combined dataframe with all metrics for this ticker
            combined = pd.DataFrame(index=positions.index)
            combined["Quantity"] = positions[ticker]
            combined["Cost Basis"] = cost_basis_data[ticker]
            combined["Market Value"] = value[ticker] if ticker in value.columns else value.get("Total", 0)
            combined["Income"] = income_data[ticker]
            combined["ROI %"] = roi_df[ticker]

            if "Total" in value.columns and ticker in value.columns:
                combined["Total Value"] = value["Total"]
                combined["Total Cost Basis"] = cost_basis_data["Total"]

            combined = combined[combined["Quantity"] != 0]
            print(f"\nCombined History for {ticker}:")
            print(combined)
        else:
            # Show separate tables for all securities
            if args.verbosity > 0:
                print(f"\nPositions\n{positions}")
                print(f"\nCost Basis\n{cost_basis_data}")
                print(f"\nMarket Value\n{value}")
                print(f"\nIncome\n{income_data}")
                print(f"\nROI (%)\n{roi_df}\n")
            else:
                print(f"\nPositions\n{positions.iloc[-1].to_frame().T}")
                print(f"\nCost Basis\n{cost_basis_data.iloc[-1].to_frame().T}")
                print(f"\nMarket Value\n{value.iloc[-1].to_frame().T}")
                print(f"\nIncome\n{income_data.iloc[-1].to_frame().T}")
                print(f"\nROI (%)\n{roi_df.iloc[-1].to_frame().T}\n")


def main(enable_profiling=False):
    """Parse CLI arguments, open the database, compute history for all matching accounts, and dispatch to the requested report.

    Args:
        enable_profiling: When True, wraps execution in cProfile and prints timing stats on exit.
    """
    if enable_profiling:
        profiler = cProfile.Profile()
        profiler.enable()

    start_time = time.perf_counter()
    action_funcs = {
        "annual-roi": annual_roi,
        "cost-basis": cost_basis,
        "fees": fees,
        "full": full_report,
        "income": income,
        "positions": show_positions,
        "roi": cumulative_roi,
        "summary": summary,
        "value": show_value,
    }
    action_help = "\n".join(f"  {name:14} {inspect.getdoc(fn).splitlines()[0]}" for name, fn in action_funcs.items())
    parser = argparse.ArgumentParser(
        description="Investment Performance Calculator",
        epilog=f"actions:\n{action_help}\n\nUse Schwab investment account credentials if prompted for a login.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        default=Path("lmi.db"),
        type=Path,
        metavar="fn",
        help="Name of database file. (default: %(default)s)",
    )
    parser.add_argument("-d", "--debug", action="store_true", default=False, help="Enable debug output")
    parser.add_argument(
        "-i",
        "--interval",
        default="ME",
        choices=["ME", "QE", "YE", "all"],
        help="Interval to report on.",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        default=False,
        help="Process all securities ever held.",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        type=str,
        help="Only act on this account name. Can be specified multiple times. (default: act on all accounts)",
    )
    parser.add_argument(
        "-t",
        "--ticker",
        default=None,
        type=str,
        help="Focus on a specific ticker symbol. (default: show all securities)",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        type=dt.date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Only show periods on or after this date.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        type=dt.date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Only show periods on or before this date.",
    )
    parser.add_argument(
        "action",
        type=str,
        choices=list(action_funcs.keys()),
        help="Action to take (see below for descriptions).",
    )
    parser.add_argument("-v", "--verbosity", action="count", default=0, help="Increase output verbosity.")
    parser.add_argument(
        "--no-equity-sum",
        action="store_false",
        dest="equity_sum",
        default=True,
        help="Disable the Equities sum column in output tables.",
    )
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
    raw_cursor = conn.cursor()
    cursor = ProfilingCursor(raw_cursor, enable_profiling=enable_profiling or args.debug)

    all_history = compute_all_history(cursor, args)  # type: ignore

    match args.action:
        case "annual-roi":
            args.all = True
            annual_roi(all_history, args)
        case "cost-basis":
            cost_basis(all_history)
        case "fees":
            fees(all_history)
        case "full":
            full_report(all_history, args)
        case "income":
            income(all_history)
        case "positions":
            show_positions(all_history)
        case "roi":
            args.all = True
            cumulative_roi(all_history, args)
        case "summary":
            summary(all_history)
        case "value":
            show_value(all_history)
    end_time = time.perf_counter()
    total_time = end_time - start_time

    # Print database query statistics
    if isinstance(cursor, ProfilingCursor):
        cursor.print_stats()

    if enable_profiling:
        print(f"\n[TOTAL EXECUTION TIME] {total_time:.4f} seconds")

    if enable_profiling:
        profiler.disable()
        s = io.StringIO()
        stats = pstats.Stats(profiler, stream=s)
        stats.sort_stats(pstats.SortKey.CUMULATIVE)
        stats.print_stats(30)  # Print top 30 functions
        print("\n" + "=" * 80)
        print("PROFILING RESULTS (Top 30 by cumulative time)")
        print("=" * 80)
        print(s.getvalue())


if __name__ == "__main__":
    # Enable profiling by setting environment variable PROFILE=1
    # or by uncommenting the line below
    import os

    enable_profiling = os.environ.get("PROFILE", "0") == "1"
    main(enable_profiling=enable_profiling)
