#!/usr/bin/env python3
"""
lmidb: little-mann investment database
a sqlite db and functions to populate it from data from schwab
"""

import csv
import datetime as dt
import pandas as pd
from pathlib import Path
import re
import sqlite3

import schapi

# this is a stupid identifier to deal with the fact that there is a stock ticker
# with the value of "cash"
cash = "__cash"


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
    directory: Path, cursor: sqlite3.Cursor, add_old=False, account_filter: str | None = None
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
    :param account_filter: optional account name to filter by
    :type account_filter: str | None
    """
    pattern = re.compile(r"([a-zA-Z]+)[\-_]([a-zA-Z0-9]+)_(\w+)_Transactions_\d{8}-\d{6}\.csv")
    directory = Path(directory)
    for file in directory.glob("*_Transactions_*.csv"):
        m = pattern.match(file.name)
        if not m:
            continue
        owner, type, account_number = m.groups()
        account_name = f"{owner}-{type}"

        # Skip if account filter is set and this account doesn't match
        if account_filter and account_name != account_filter:
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
    if new_positions > 0:
        print(f"Added {new_positions} positions from {fn}")


def update_all_schwab_positions_from_dir(directory: Path, cursor: sqlite3.Cursor, account_filter: str | None = None):
    """
    Iterates through given directory, finds Schwab positions CSVs
    with the pattern <account_name>-Positions-<YYYY-MM-DD-HHMMSS>.csv,
    ensures accounts are present, and updates positions in the DB.

    :param directory: directory containing position CSV files
    :type directory: Path
    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filter: optional account name to filter by
    :type account_filter: str | None
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

        # Skip if account filter is set and this account doesn't match
        if account_filter and account_name != account_filter:
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


def update_db(cursor: sqlite3.Cursor, data_path: Path, add_old=False, account_filter: str | None = None) -> None:
    """
    Update accounts, positions, and transactions from the indicated data_path.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param data_path: directory path containing Schwab data files
    :type data_path: Path
    :param add_old: if True, add historical data; if False, only add new data
    :type add_old: bool
    :param account_filter: optional account name to filter by
    :type account_filter: str | None
    """

    create_initial_tables(cursor)
    update_all_schwab_transactions_from_dir(data_path, cursor, add_old, account_filter)
    update_all_schwab_positions_from_dir(data_path, cursor, account_filter)


def print_positions(cursor: sqlite3.Cursor, account_filter: str | None = None) -> None:
    """
    Prints all positions for each account (or a specific account if filtered).
    Creates a DataFrame with dates as rows and tickers as columns, showing quantities.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filter: optional account name to filter by
    :type account_filter: str | None
    """
    # Get all accounts or filter by name
    if account_filter:
        cursor.execute("SELECT id, name, number, owner FROM accounts WHERE name = ?", (account_filter,))
    else:
        cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts and account_filter:
        print(f"No account found with name: {account_filter}")
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


def dump_summary(cursor: sqlite3.Cursor, account_filter: str | None = None):
    """
    Prints latest positions for each account, showing all position rows for the most recent date.
    Prints the tickers with data and the range of dates for which there is data.
    If account_filter is provided, only dump data for that account name.

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    :param account_filter: optional account name to filter by
    :type account_filter: str | None
    """
    # Get all accounts or filter by name
    if account_filter:
        cursor.execute("SELECT id, name, number, owner FROM accounts WHERE name = ?", (account_filter,))
    else:
        cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()

    if not accounts and account_filter:
        print(f"No account found with name: {account_filter}")
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
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # if it asks for verification, use the account login, not the dev portal login
    update_db(conn.cursor(), Path("schwab-data"))
    dump_summary(conn.cursor())
    conn.close()
