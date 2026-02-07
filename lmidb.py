#!/usr/bin/env python3
"""
lmidb: little-mann investment database
a sqlite db and functions to populate it from data from schwab
"""

import argparse
import csv
import datetime as dt
import pandas as pd
from pathlib import Path
import re
import sqlite3

import schapi

# this is a stupid identifier to deal with the fact that there is a stock ticker
# with the value of "cash"
cash = "cash"


def create_initial_tables(cursor: sqlite3.Cursor) -> None:
    """
    Creates the initial database tables: accounts, tickers, and candles.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    """
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL,
            name TEXT NOT NULL,
            owner TEXT NOT NULL)
        """
    )

    cursor.execute(
        """CREATE TABLE IF NOT EXISTS tickers(
            symbol TEXT UNIQUE)
        """
    )
    cursor.executemany("INSERT OR IGNORE INTO tickers VALUES (?)", [[cash]])

    cursor.execute(
        """CREATE TABLE IF NOT EXISTS candles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATETIME NOT NULL,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            close REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            volume REAL NOT NULL,
            FOREIGN KEY (symbol) REFERENCES tickers(symbol))
        """
    )
    cursor.connection.commit()


def add_ticker(cursor: sqlite3.Cursor, symbol: str) -> None:
    """
    Adds a ticker symbol to the tickers table.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param symbol: ticker symbol to add
    :type symbol: str
    """
    cursor.execute(f"INSERT OR IGNORE INTO tickers VALUES ('{symbol}')")
    cursor.connection.commit()


def get_tickers(cursor: sqlite3.Cursor) -> list[str]:
    """
    Retrieves all ticker symbols from the tickers table.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :return: list of ticker symbols
    :rtype: list[str]
    """
    cursor.execute("SELECT * FROM tickers")
    return [row[0] for row in cursor.fetchall()]


def get_account_id_by_number(cursor: sqlite3.Cursor, number: str) -> int | None:
    """
    Retrieves the account ID for a given account number.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param number: account number to look up
    :type number: str
    :return: account ID if found, None otherwise
    :rtype: int | None
    """
    cursor.execute("SELECT id FROM accounts WHERE number = ?", (number,))
    result = cursor.fetchone()
    return result[0] if result else None


def add_account(cursor: sqlite3.Cursor, number: str, name: str, owner: str) -> None:
    """
    Adds an account to the accounts table and creates corresponding transactions and positions tables.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param number: account number
    :type number: str
    :param name: account name
    :type name: str
    :param owner: account owner name
    :type owner: str
    """
    cursor.executemany("INSERT OR IGNORE INTO accounts (number, name, owner) VALUES (?, ?, ?)", [[number, name, owner]])
    account_id = get_account_id_by_number(cursor, number)
    cursor.execute(
        f"""CREATE TABLE IF NOT EXISTS transactions_{account_id}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATETIME NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT NOT NULL,
            description TEXT,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            fees REAL,
            amount REAL NOT NULL,
            FOREIGN KEY (symbol) REFERENCES tickers(symbol))
        """
    )
    # Holds positions as reported by schwab on the given date.
    cursor.execute(
        f"""CREATE TABLE IF NOT EXISTS positions_{account_id}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATETIME NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            value REAL NOT NULL,
            FOREIGN KEY (symbol) REFERENCES tickers(symbol))
        """
    )
    cursor.connection.commit()


def add_transaction(
    cursor: sqlite3.Cursor,
    date: dt.datetime,
    account_id: int,
    action: str,
    symbol: str,
    description: str,
    quantity: float,
    price: float,
    fees: float,
    amount: float,
) -> None:
    """
    Adds a transaction to the specified account's transaction table.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param date: transaction date
    :type date: dt.datetime
    :param account_id: account ID
    :type account_id: int
    :param action: transaction action (Buy, Sell, Dividend, etc.)
    :type action: str
    :param symbol: ticker symbol
    :type symbol: str
    :param description: transaction description
    :type description: str
    :param quantity: number of shares
    :type quantity: float
    :param price: price per share
    :type price: float
    :param fees: fees and commissions
    :type fees: float
    :param amount: total transaction amount
    :type amount: float
    """
    iso_date = date.isoformat()
    cursor.execute(
        f"""INSERT INTO transactions_{account_id}
        (date, action, symbol, description, quantity, price, fees, amount)
        VALUES ('{iso_date}', '{action}', '{symbol}', 
                '{description}', {quantity}, {price}, {fees}, {amount})"""
    )


def fns_to_float(v: str) -> float:
    """
    Converts a string with possible commas and dollar signs to a float.

    :param v: string value to convert
    :type v: str
    :return: float value
    :rtype: float
    """
    vv = v.translate(str.maketrans({"$": "", ",": "", "=": "", '"': ""}))
    if len(vv) == 0:
        return 0
    else:
        return float(vv)


def update_schwab_transactions(fn: Path, account_id: int, cursor: sqlite3.Cursor, add_old=False) -> None:
    """
    Reads Schwab exported transactions .csv files and adds them to the database, optimized for date-sorted input.

    :param fn: path to the transactions CSV file
    :type fn: Path
    :param account_id: account ID
    :type account_id: int
    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param add_old: if True, add historical data; if False, only add new transactions
    :type add_old: bool
    """
    # Get the latest transaction date for this account
    table_name = f"transactions_{account_id}"
    cursor.execute(f"SELECT MAX(date) FROM {table_name}")
    result = cursor.fetchone()
    latest_db_date = result[0] if result and result[0] is not None else None

    with open(fn, newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        new_transactions = 0
        for row in reader:
            date = pd.to_datetime(row["Date"][0:10])
            if not add_old and latest_db_date and date <= pd.to_datetime(latest_db_date):
                continue  # Skip, already in DB
            action = row["Action"]
            symbol = row["Symbol"]
            description = row["Description"]
            quantity = fns_to_float(row["Quantity"])
            price = fns_to_float(row["Price"])
            fees = fns_to_float(row["Fees & Comm"])
            amount = fns_to_float(row["Amount"])

            # Optionally, still do a duplicate check by all fields, or just insert
            query = f"""
                SELECT 1 FROM {table_name} WHERE
                    date = '{date.isoformat()}' AND
                    action = '{action}' AND
                    symbol = '{symbol}' AND
                    description = '{description}' AND
                    quantity = {quantity} AND
                    price = {price} AND
                    fees = {fees} AND
                    amount = {amount}
                LIMIT 1
            """
            cursor.execute(query)
            if not cursor.fetchone():
                try:
                    add_transaction(
                        cursor,
                        date,
                        account_id,
                        action,
                        symbol,
                        description,
                        quantity,
                        price,
                        fees,
                        amount,
                    )
                    new_transactions += 1
                except sqlite3.IntegrityError:
                    add_ticker(cursor, symbol)
                    add_transaction(
                        cursor,
                        date,
                        account_id,
                        action,
                        symbol,
                        description,
                        quantity,
                        price,
                        fees,
                        amount,
                    )
            else:
                if not add_old:
                    print("interesting, transaction already exists, skipping insert")
    cursor.connection.commit()
    print(f"Added {new_transactions} new transactions from {fn}")


def update_all_schwab_transactions_from_dir(
    directory: Path,
    cursor: sqlite3.Cursor,
    add_old=False,
    account_filters: list[str] | None = None,
):
    """
    Iterates through given directory, finds Schwab transactions CSVs,
    extracts owner and account info, ensures accounts are present, and imports transactions.

    :param directory: directory containing transaction CSV files
    :type directory: Path
    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param add_old: if True, add historical data; if False, only add new transactions
    :type add_old: bool
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """
    pattern = re.compile(r"([a-zA-Z]+)[\-_]([a-zA-Z0-9]+)_(\w+)_Transactions_\d{8}-\d{6}\.csv")
    directory = Path(directory)
    for file in directory.glob("*_Transactions_*.csv"):
        m = pattern.match(file.name)
        if not m:
            continue
        owner, type, account_number = m.groups()
        account_name = f"{owner}-{type}"

        # Skip if account filters are set and this account doesn't match
        if account_filters and account_name not in account_filters:
            continue

        # Ensure this account is present in accounts table
        account_id = get_account_id_by_number(cursor, account_number)
        if account_id is None:
            add_account(cursor, account_number, account_name, owner)
            account_id = get_account_id_by_number(cursor, account_number)

        update_schwab_transactions(file, account_id, cursor, add_old)  # type: ignore


def update_schwab_positions(fn: Path, account_id: int, cursor: sqlite3.Cursor) -> None:
    """
    Reads a Schwab exported positions .csv file and adds the positions to the database using csv.DictReader.

    :param fn: path to the positions CSV file
    :type fn: Path
    :param account_id: account ID
    :type account_id: int
    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    """
    new_positions = 0
    table_name = f"positions_{account_id}"
    with open(fn, newline="") as csvfile:
        # The first line is intro line header with the date
        header = csvfile.readline()
        csvfile.readline()  # a blank line
        ymd = header.split(",")[1].strip('" \n')
        pos_date = dt.datetime.strptime(ymd, "%Y/%m/%d")
        reader = csv.DictReader(csvfile)
        for row in reader:
            symbol = row["Symbol"]
            if not symbol:
                print("Empty symbol")
            elif symbol == "Cash & Cash Investments":
                symbol = cash
                value = fns_to_float(row["Mkt Val (Market Value)"])
                quantity = value
                price = 1
            elif symbol == "Account Total":
                continue
            else:
                quantity = fns_to_float(row["Qty (Quantity)"])
                price = fns_to_float(row["Price"])
                value = fns_to_float(row["Mkt Val (Market Value)"])
            cursor.execute("INSERT OR IGNORE INTO tickers (symbol) VALUES (?)", (symbol,))
            select_query = f"""
                SELECT 1 FROM {table_name}
                WHERE date = ? AND symbol = ? AND quantity = ? AND price = ? AND value = ?
                LIMIT 1
            """

            cursor.execute(select_query, (pos_date.isoformat(), symbol, quantity, price, value))  # type: ignore
            if cursor.fetchone():
                continue  # Already exists, skip insert
            cursor.execute(
                f"INSERT INTO {table_name} (date, symbol, quantity, price, value) VALUES (?, ?, ?, ?, ?)",
                (pos_date.isoformat(), symbol, quantity, price, value),  # type: ignore
            )
            new_positions += 1
    cursor.connection.commit()
    print(f"Added {new_positions} positions from {fn}")


def update_all_schwab_positions_from_dir(directory: Path, cursor: sqlite3.Cursor, account_filters: list[str] | None = None):
    """
    Iterates through given directory, finds Schwab positions CSVs
    with the pattern <account_name>-Positions-<YYYY-MM-DD-HHMMSS>.csv,
    ensures accounts are present, and updates positions in the DB.

    :param directory: directory containing position CSV files
    :type directory: Path
    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """
    # Regex: account name can have dashes and letters/digits; then -Positions-, then date/time
    pattern = re.compile(r"([a-zA-Z]+)[\-_]([a-zA-Z0-9]+)-Positions-\d{4}-\d{2}-\d{2}-\d{6}\.csv")
    directory = Path(directory)
    for file in directory.glob("*-Positions-*.csv"):
        m = pattern.match(file.name)
        if not m:
            continue
        owner, type = m.groups()
        account_name = f"{owner}-{type}"

        # Skip if account filters are set and this account doesn't match
        if account_filters and account_name not in account_filters:
            continue

        # Find the account id from the name
        cursor.execute("SELECT id FROM accounts WHERE name = ?", (account_name,))
        result = cursor.fetchone()
        if not result:
            print(f"Warning: Account with name {account_name} not found in accounts table, skipping {file}")
            continue
        account_id = result[0]
        update_schwab_positions(file, account_id, cursor)


def get_candles_dates(cursor: sqlite3.Cursor) -> dict:
    """
    Retrieves the date ranges for candles data for each ticker symbol.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :return: dictionary mapping symbol to tuple of (first_date, last_date)
    :rtype: dict
    """
    cursor.execute(
        """
        SELECT symbol, MIN(date) as first_date, MAX(date) as last_date
        FROM candles
        GROUP BY symbol
    """
    )
    ranges = cursor.fetchall()
    cd = {}
    for symbol, first_date, last_date in ranges:
        cd[symbol] = (dt.datetime.fromisoformat(first_date), dt.datetime.fromisoformat(last_date))
    return cd


def update_candles(cursor: sqlite3.Cursor) -> None:
    """
    Get/Update candles (OHLCV data) for all securities in the tickers db
    from the Schwab API (schapi) into the database.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    """

    tickers = get_tickers(cursor)
    client = schapi.get_client()

    cd = get_candles_dates(cursor)

    for t in tickers:
        print(f"{t} ", end="", flush=True)
        if t == cash or t == "":
            continue
        if t in cd:
            # it appears that having a start day on the weekend throws an error
            d0 = cd[t][1]
            # d0 += dt.timedelta(days=1)
            # print(d0)
            candles_data = schapi.get_daily(client, [t], d0).get("candles", [])
        else:
            candles_data = schapi.get_daily(client, [t]).get("candles", [])

        if not candles_data:
            print(f"(No data found), ", end="", flush=True)
            continue

        print(f"({len(candles_data)} new)", end="", flush=True)
        # try this monday
        # if len(candles_data) == 1:
        #    continue
        for candle in candles_data:
            date = pd.to_datetime(candle["datetime"], unit="ms", utc=True).isoformat()
            open_ = candle["open"]
            close = candle["close"]
            high = candle["high"]
            low = candle["low"]
            volume = candle["volume"]
            # Insert, ignore if already there for this date and symbol
            cursor.execute(
                """
                INSERT OR IGNORE INTO candles (date, symbol, open, close, high, low, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date, t, open_, close, high, low, volume),
            )
        print(", ", end="", flush=True)
    cursor.connection.commit()
    print("\nCandle updates complete.")


def get_closing_values(cursor: sqlite3.Cursor, tickers: list[str], date: dt.date | dt.datetime) -> pd.Series:
    """
    Returns the closing values for a list of tickers for a single date as a pandas Series.
    If no data exists for a ticker on that date, NaN is returned for that ticker.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param tickers: list of ticker symbols
    :type tickers: list[str]
    :param date: date to get closing values for
    :type date: dt.date | dt.datetime
    :return: pandas Series with ticker symbols as index and closing values as values
    :rtype: pd.Series
    """
    # Convert date to just the date part if it's a datetime
    if isinstance(date, dt.datetime):
        date = date.date()

    result = {}

    for ticker in tickers:
        # Handle special case for cash
        if ticker == cash:
            result[ticker] = 1.0
            continue

        # Query for the closing value on the specified date
        # We need to match on the date part of the datetime string
        cursor.execute(
            """
            SELECT close FROM candles
            WHERE symbol = ? AND date(date) = date(?)
            ORDER BY date DESC
            LIMIT 1
            """,
            (ticker, date.isoformat()),
        )
        row = cursor.fetchone()

        if row:
            result[ticker] = float(row[0])
        else:
            result[ticker] = None

    return pd.Series(result)


def get_securities_in_account(cursor: sqlite3.Cursor, account_id: int) -> list[str]:
    """
    Returns a list of all unique security symbols that have appeared in any transaction
    in the specified account. If the transaction table for the account does not exist, returns an empty list.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_id: account ID
    :type account_id: int
    :return: list of ticker symbols including cash
    :rtype: list[str]
    """
    table_name = f"transactions_{account_id}"
    try:
        cursor.execute(f"SELECT DISTINCT symbol FROM {table_name} WHERE symbol IS NOT NULL AND symbol != ''")
        securities = [row[0] for row in cursor.fetchall()]
        securities.append(cash)
        return securities
    except sqlite3.OperationalError:
        # The transactions table does not exist for this account
        return []


def update_transactions(
    cursor: sqlite3.Cursor,
    data_path: Path,
    add_old=False,
    account_filters: list[str] | None = None,
) -> None:
    """
    Update accounts, positions, and transactions from the indicated data_path.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param data_path: directory path containing Schwab data files
    :type data_path: Path
    :param add_old: if True, add historical data; if False, only add new data
    :type add_old: bool
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """

    create_initial_tables(cursor)
    update_all_schwab_transactions_from_dir(data_path, cursor, add_old, account_filters)


def update_positions(
    cursor: sqlite3.Cursor,
    data_path: Path,
    add_old=False,
    account_filters: list[str] | None = None,
) -> None:
    """
    Update accounts, positions, and transactions from the indicated data_path.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param data_path: directory path containing Schwab data files
    :type data_path: Path
    :param add_old: if True, add historical data; if False, only add new data
    :type add_old: bool
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """

    create_initial_tables(cursor)
    update_all_schwab_positions_from_dir(data_path, cursor, account_filters)


def print_transactions(cursor: sqlite3.Cursor, account_filters: list[str] | None = None) -> None:
    """
    Prints all transactions for each account (or specific accounts if filtered).
    Creates a DataFrame with transaction details.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """
    # Get all accounts or filter by name
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
        return

    for acc in accounts:
        account_id = acc["id"]
        account_name = acc["name"]
        account_number = acc["number"]
        account_owner = acc["owner"]
        table_name = f"transactions_{account_id}"

        # Try to get all transactions for this account
        try:
            cursor.execute(
                f"SELECT date, action, symbol, description, quantity, price, fees, amount FROM {table_name} ORDER BY date DESC"
            )
            rows = cursor.fetchall()

            if not rows:
                print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
                print("  No transactions found.")
                continue

            # Convert to pandas DataFrame
            data = {
                "date": [],
                "action": [],
                "symbol": [],
                "description": [],
                "quantity": [],
                "price": [],
                "fees": [],
                "amount": [],
            }
            for row in rows:
                # Extract just the date part (YYYY-MM-DD) from the datetime
                date_only = row["date"].split("T")[0] if "T" in row["date"] else row["date"].split()[0]
                data["date"].append(date_only)
                data["action"].append(row["action"])
                data["symbol"].append(row["symbol"])
                data["description"].append(row["description"])
                data["quantity"].append(row["quantity"])
                data["price"].append(row["price"])
                data["fees"].append(row["fees"])
                data["amount"].append(row["amount"])

            df = pd.DataFrame(data)

            print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
            print(f"All Transactions ({len(df)} total):")
            print(df.to_string(index=False))

        except sqlite3.OperationalError:
            print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
            print("  No transactions table found.")


def print_positions(cursor: sqlite3.Cursor, account_filters: list[str] | None = None) -> None:
    """
    Prints all positions for each account (or specific accounts if filtered).
    Creates a DataFrame with dates as rows and tickers as columns, showing quantities.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """
    # Get all accounts or filter by name
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
        return

    for acc in accounts:
        account_id = acc["id"]
        account_name = acc["name"]
        account_number = acc["number"]
        account_owner = acc["owner"]
        table_name = f"positions_{account_id}"

        # Try to get all positions for this account
        try:
            cursor.execute(f"SELECT date, symbol, quantity FROM {table_name} ORDER BY date, symbol")
            rows = cursor.fetchall()

            if not rows:
                print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
                print("  No positions found.")
                continue

            # Convert to pandas DataFrame for easier pivoting
            data = {"date": [], "symbol": [], "quantity": []}
            for row in rows:
                # Extract just the date part (YYYY-MM-DD) from the datetime
                date_only = row["date"].split("T")[0] if "T" in row["date"] else row["date"].split()[0]
                data["date"].append(date_only)
                data["symbol"].append(row["symbol"])
                data["quantity"].append(row["quantity"])

            df = pd.DataFrame(data)

            # Pivot so dates are rows and symbols are columns
            pivot_df = df.pivot(index="date", columns="symbol", values="quantity")
            pivot_df = pivot_df.fillna(0)

            print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
            print(f"All Positions (Quantities):")
            print(pivot_df)

        except sqlite3.OperationalError:
            print(f"\nAccount: {account_name} ({account_number}, owner: {account_owner})")
            print("  No positions table found.")


def print_accounts(cursor: sqlite3.Cursor) -> None:
    """
    Prints a list of all accounts in the database.
    """
    cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts:
        print("No accounts found in database.")
        return

    print("\nAccounts:")
    print(f"{'ID':<5} {'Name':<20} {'Number':<15} {'Owner':<15}")
    print("-" * 60)
    for acc in accounts:
        print(f"{acc['id']:<5} {acc['name']:<20} {acc['number']:<15} {acc['owner']:<15}")
    print()


def print_candles(cursor: sqlite3.Cursor, ticker: str | None = None) -> None:
    """
    Prints candle (OHLCV) data for a specific ticker or all tickers.
    Creates a DataFrame with candle data.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param ticker: ticker symbol to display (if None, displays all tickers)
    :type ticker: str | None
    """
    if ticker:
        # Query for specific ticker
        cursor.execute(
            """
            SELECT date, symbol, open, close, high, low, volume
            FROM candles
            WHERE symbol = ?
            ORDER BY date DESC
            """,
            (ticker,),
        )
        rows = cursor.fetchall()

        if not rows:
            print(f"No candle data found for ticker: {ticker}")
            # Check if ticker exists in tickers table
            cursor.execute("SELECT symbol FROM tickers WHERE symbol = ?", (ticker,))
            if cursor.fetchone():
                print(f"  (Ticker {ticker} exists in database but has no candle data)")
            else:
                print(f"  (Ticker {ticker} not found in database)")
            return

        # Convert to pandas DataFrame
        data = {
            "date": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        }
        for row in rows:
            # Extract just the date part (YYYY-MM-DD) from the datetime
            date_only = row["date"].split("T")[0] if "T" in row["date"] else row["date"].split()[0]
            data["date"].append(date_only)
            data["open"].append(row["open"])
            data["high"].append(row["high"])
            data["low"].append(row["low"])
            data["close"].append(row["close"])
            data["volume"].append(row["volume"])

        df = pd.DataFrame(data)

        print(f"\nCandle data for {ticker} ({len(df)} records):")
        print(df.to_string(index=False))

    else:
        # Query for all tickers
        cursor.execute(
            """
            SELECT symbol, COUNT(*) as count, MIN(date) as first_date, MAX(date) as last_date
            FROM candles
            GROUP BY symbol
            ORDER BY symbol
            """
        )
        rows = cursor.fetchall()

        if not rows:
            print("No candle data found in database.")
            return

        print("\nCandle data summary:")
        print(f"{'Symbol':<10} {'Count':<10} {'First Date':<12} {'Last Date':<12}")
        print("-" * 50)
        for row in rows:
            symbol = row["symbol"]
            count = row["count"]
            first_date = row["first_date"].split("T")[0] if "T" in row["first_date"] else row["first_date"].split()[0]
            last_date = row["last_date"].split("T")[0] if "T" in row["last_date"] else row["last_date"].split()[0]
            print(f"{symbol:<10} {count:<10} {first_date:<12} {last_date:<12}")
        print()


def print_summary(cursor: sqlite3.Cursor, account_filters: list[str] | None = None):
    """
    Prints latest positions for each account, showing all position rows for the most recent date.
    Prints the tickers with data and the range of dates for which there is data.
    If account_filters is provided, only dump data for those account names.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filters: optional list of account names to filter by
    :type account_filters: list[str] | None
    """
    # Get all accounts or filter by name
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
        return
    for acc in accounts:
        account_id = acc["id"]
        try:
            cursor.execute(f"SELECT COUNT(*), MIN(date), MAX(date) FROM transactions_{account_id}")
            result = cursor.fetchone()
            tx_count = result[0]
            tx_min_date = result[1]
            tx_max_date = result[2]
        except sqlite3.OperationalError:
            tx_count = 0  # If the table doesn't exist
            tx_min_date = None
            tx_max_date = None
        # Count the number of unique position dates for this account
        table_name = f"positions_{account_id}"
        try:
            cursor.execute(f"SELECT COUNT(DISTINCT date) FROM {table_name}")
            unique_position_dates = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            unique_position_dates = 0

        tx_date_range = ""
        if tx_min_date and tx_max_date:
            tx_date_range = f" ({tx_min_date[:10]} to {tx_max_date[:10]})"

        account_info = f"{acc['name']} ({acc['number']}, owner: {acc['owner']}), transactions: {tx_count}{tx_date_range}, position: {unique_position_dates}"

        table_name = f"positions_{account_id}"
        # Try to get latest date for positions in this account
        try:
            cursor.execute(f"SELECT MAX(date) FROM {table_name}")
            max_date = cursor.fetchone()[0]
            if not max_date:
                print(f"{account_info}: No positions found.")
                continue
            print(f"\nAccount: {account_info}\nLatest Positions Date: {max_date}")
            cursor.execute(f"SELECT symbol, quantity, price, value FROM {table_name} WHERE date = ?", (max_date,))
            positions = cursor.fetchall()
            for pos in positions:
                symbol, quantity, price, value = pos
                print(f"  {symbol:10}  Qty: {quantity:10.4f}  Price: {price:10.2f}  Value: {value:10.2f}")
        except sqlite3.OperationalError:
            print(f"{account_info}: No positions table found.")

    print("\nTickers tracked:")
    for k, v in get_candles_dates(cursor).items():
        print(f"{k:8}: {v[0].date()} to {v[1].date()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="little-mann investment database.", epilog="Use schwab account credentials if prompted for a login."
    )
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
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        type=str,
        help="Only act on this account name. Can be specified multiple times. (default: act on all accounts)",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        type=str,
        help="Ticker symbol for candles action. (default: show all tickers)",
    )
    parser.add_argument(
        "action",
        type=str,
        choices=[
            "update",
            "update-positions",
            "update-transactions",
            "update-candles",
            "summary",
            "positions",
            "transactions",
            "accounts",
            "candles",
        ],
        help="Action to take.",
    )
    parser.add_argument("--add-old", action="store_true", default=False, help="Add old/historical data when updating.")

    args = parser.parse_args()

    fn = args.database
    if fn == Path(""):
        conn = sqlite3.connect(":memory:")
    else:
        conn = sqlite3.connect(fn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    match args.action:
        case "update":
            update_transactions(cursor, args.schwab_data, args.add_old, args.account)
            update_positions(cursor, args.schwab_data, args.add_old, args.account)
            update_candles(cursor)
        case "update-positions":
            update_positions(cursor, args.schwab_data, args.add_old, args.account)
        case "update-transactions":
            update_transactions(cursor, args.schwab_data, args.add_old, args.account)
        case "update-candles":
            update_candles(cursor)
        case "summary":
            print_summary(cursor, args.account)
        case "accounts":
            print_accounts(cursor)
        case "positions":
            print_positions(cursor, args.account)
        case "transactions":
            print_transactions(cursor, args.account)
        case "candles":
            print_candles(cursor, args.ticker)
        case _:
            print("inconcievable")
