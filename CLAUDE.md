# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Investment ROI tracking and analysis system for Schwab brokerage accounts. Imports transaction and position data from Schwab CSV exports, fetches historical price data via Schwab API, and computes cost basis, market values, and returns over various time intervals.

## Core Architecture

### Three Main Components

1. **lmidb.py** - Database management layer
   - SQLite database with per-account transaction and position tables
   - Manages tickers, candles (OHLCV price data), accounts, transactions, and positions
   - Implements a global candles cache using NumPy arrays for fast binary search lookups (10-100x speedup)
   - Cache must be cleared with `clear_candles_cache()` after database updates

2. **roi.py** - Report generation and analysis
   - Computes positions backward in time from latest Schwab-reported positions
   - Computes cost basis forward in time using transaction history
   - Supports multiple reporting intervals: monthly (ME), quarterly (QE), yearly (YE)
   - Calculates cumulative ROI and interval ROI for each security

3. **schapi.py** - Schwab API wrapper
   - Uses schwab-py library for authentication and data fetching
   - Credentials stored in `.secrets` file (not in version control)
   - Token cached in `/tmp/token.json` (7 day expiration)
   - Fetches daily OHLCV candle data for securities

### Database Schema

- **accounts**: id, number, name, owner
- **tickers**: symbol (unique)
- **candles**: id, date, symbol, open, close, high, low, volume
- **transactions_{account_id}**: id, date, action, symbol, description, quantity, price, fees, amount
- **positions_{account_id}**: id, date, symbol, quantity, price, value

### Key Algorithms

**Positions Computation** (backward in time):
- Starts from latest Schwab-reported positions
- Walks backward through transactions, inverting operations
- Example: "Buy 10 shares" becomes "subtract 10 shares" when going backward

**Cost Basis Computation** (forward in time):
- Initializes with market value at first month-end for pre-existing holdings
- Adds cost for buys, proportionally reduces cost for sells
- Handles special cases: journaled shares, security transfers, stock splits

**ROI Calculations**:
- Cumulative ROI: Total return from initial investment to current period
- Interval ROI: Return for each discrete time period (accounts for cash flows)

## Common Development Commands

### Database Management (lmidb.py)

Update all data (transactions, positions, candles):
```bash
./lmidb.py update
```

Update specific data types:
```bash
./lmidb.py update-transactions      # Import new transactions from schwab-data/
./lmidb.py update-positions          # Import new positions from schwab-data/
./lmidb.py update-candles            # Fetch latest price data via Schwab API
```

View database contents:
```bash
./lmidb.py summary                   # Latest positions and candle date ranges
./lmidb.py accounts                  # List all accounts
./lmidb.py transactions --account deb-inv  # View transactions for specific account
./lmidb.py positions --account jon-ira     # View positions for specific account
./lmidb.py candles --ticker AAPL     # View candle data for ticker
```

Options:
- `--database lmi.db` - Database file (default: lmi.db)
- `--schwab-data schwab-data/` - Directory with CSV exports (default: schwab-data/)
- `--account <name>` - Filter to specific account(s), can be repeated
- `--add-old` - Import historical data (default: only new transactions)

### Report Generation (roi.py)

Generate reports:
```bash
./roi.py summary                     # First and last period values with total ROI
./roi.py full                        # Comprehensive report: positions, cost basis, value, income, ROI
./roi.py roi                         # Cumulative ROI for each security
./roi.py interval-roi                # Period-over-period ROI
./roi.py annual-roi                  # Annualized (CAGR) ROI for each security
./roi.py value                       # Market values over time
./roi.py cost-basis                  # Cost basis over time
./roi.py positions                   # Quantity positions over time
./roi.py income                      # Income (dividends, interest, gains) over time
```

Options:
- `--database lmi.db` - Database file (default: lmi.db)
- `--interval {ME,QE,YE,all}` - Reporting interval (default: ME=monthly)
- `--account <name>` - Filter to specific account(s), can be repeated
- `--ticker <symbol>` - Focus on specific ticker (shows detailed transaction log)
- `--all` - Include all securities ever held (default: only currently held)
- `-d, --debug` - Enable debug output and query profiling
- `-v, --verbosity` - Increase output verbosity (e.g. `roi` shows only latest row by default, full history with `-v`)

### Testing

Run tests:
```bash
python -m pytest tests/
python -m pytest tests/test_lmidb.py -v
```

### Profiling

Enable profiling to measure performance:
```bash
PROFILE=1 ./roi.py full
```

This shows:
- Function execution times (via @timeit decorators)
- Database query statistics
- Python profiler output for top 30 functions

## Data Flow

1. Export CSV files from Schwab website:
   - Transaction CSVs: `{owner}_{type}_XXX{number}_Transactions_{timestamp}.csv`
   - Position CSVs: `{owner}-{type}-Positions-{timestamp}.csv`
   - Place in `schwab-data/` directory

2. Run `./lmidb.py update` to:
   - Create/update accounts from CSV filenames
   - Import new transactions (date-sorted, skips duplicates)
   - Import new positions (stores snapshots)
   - Fetch candle data for all tickers via Schwab API

3. Run `./roi.py <action>` to generate reports from database

## Important Implementation Notes

- **Cash handling**: Cash is tracked as a special ticker with symbol `lmidb.cash` (string "cash")
- **Transaction actions**: 40+ action types (Buy, Sell, Dividend, etc.) - see roi.py lines 442-533 for complete handling
- **Candles cache**: Global `_candles_cache` dict with NumPy arrays for fast lookups - critical for performance
- **Cost basis edge cases**:
  - Journaled shares and security transfers with price=0 use the candle closing price at the transaction date
  - CUSIP-only securities (9-digit symbols) use price=1 when no candle data exists
  - Stock splits don't change total cost basis
- **Date handling**: Month-end dates (ME) are primary index, uses pandas date_range
- **ProfilingCursor**: Wrapper class for tracking query performance when debugging

## Code Style

Follow Black formatter standards:
- 4 spaces for indentation
- 88 character line limit
- Double quotes for strings
- Trailing commas in multi-line structures

## Dependencies

See requirements.txt:
- ipython - Interactive development
- pandas - Data processing
- numpy - Numerical operations and fast array lookups
- httpx - HTTP client
- schwab - Unofficial Schwab API library (schwab-py)
