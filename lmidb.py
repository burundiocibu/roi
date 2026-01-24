#!/usr/bin/env python3
"""
lmidb: little-mann investment database
a sqlite db and functions to populate it from data from schwab
"""

import csv
import datetime as dt
import os
import pandas as pd
from pathlib import Path
import re
import sqlite3

import schapi

# this is a stupid identifier to deal with the fact that there is a stock ticker
# with the value of "cash"
cash = "__cash"


def create_initial_tables(cursor: sqlite3.Cursor) -> None:
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
    cursor.execute(f"INSERT OR IGNORE INTO tickers VALUES ('{symbol}')")
    cursor.connection.commit()


def get_tickers(cursor: sqlite3.Cursor) -> list[str]:
    cursor.execute("SELECT * FROM tickers")
    return [row[0] for row in cursor.fetchall()]


def get_account_id_by_number(cursor: sqlite3.Cursor, number: str) -> int | None:
    cursor.execute("SELECT id FROM accounts WHERE number = ?", (number,))
    result = cursor.fetchone()
    return result[0] if result else None


def add_account(cursor: sqlite3.Cursor, number: str, name: str, owner: str) -> None:
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
    iso_date = date.isoformat()
    cursor.execute(
        f"""INSERT INTO transactions_{account_id}
        (date, action, symbol, description, quantity, price, fees, amount)
        VALUES ('{iso_date}', '{action}', '{symbol}', 
                '{description}', {quantity}, {price}, {fees}, {amount})"""
    )


def fns_to_float(v: str) -> float:
    """
    Converts a string with possible commans and dollar signs to a float
    """
    vv = v.translate(str.maketrans({"$": "", ",": "", "=": "", '"': ""}))
    if len(vv) == 0:
        return 0
    else:
        return float(vv)


def update_schwab_transactions(fn: Path, account_id: int, cursor: sqlite3.Cursor):
    """
    Reads schwab exported transactions .csv files and adds them to the database, optimized for date-sorted input.
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
            if latest_db_date and date <= pd.to_datetime(latest_db_date):
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
                print("interesting, transaction already exists, skipping insert")
    cursor.connection.commit()
    if new_transactions > 0:
        print(f"Added {new_transactions} new transactions from {fn}")


def update_all_schwab_transactions_from_dir(directory: Path, cursor: sqlite3.Cursor):
    """
    Iterates through given directory, finds Schwab transactions CSVs,
    extracts owner and account info, ensures accounts are present, and imports transactions.
    """
    pattern = re.compile(r"([a-zA-Z]+)[\-_]([a-zA-Z0-9]+)_(\w+)_Transactions_\d{8}-\d{6}\.csv")
    directory = Path(directory)
    for file in directory.glob("*_Transactions_*.csv"):
        m = pattern.match(file.name)
        if not m:
            continue
        owner, type, account_number = m.groups()
        account_name = f"{owner}-{type}"

        # Ensure this account is present in accounts table
        account_id = get_account_id_by_number(cursor, account_number)
        if account_id is None:
            add_account(cursor, account_number, account_name, owner)
            account_id = get_account_id_by_number(cursor, account_number)

        update_schwab_transactions(file, account_id, cursor)  # type: ignore


def get_transactions(cursor: sqlite3.Cursor, account_id: int):
    cursor.execute(f"SELECT * FROM transactions_{account_id}")
    return [row[0] for row in cursor.fetchall()]


def update_schwab_positions(fn: Path, account_id: int, cursor: sqlite3.Cursor) -> None:
    """
    Reads a Schwab exported positions .csv file and adds the positions to the database using csv.DictReader.
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


def update_all_schwab_positions_from_dir(directory: Path, cursor: sqlite3.Cursor):
    """
    Iterates through given directory, finds Schwab positions CSVs
    with the pattern <account_name>-Positions-<YYYY-MM-DD-HHMMSS>.csv,
    ensures accounts are present, and updates positions in the DB.
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
        # Find the account id from the name
        cursor.execute("SELECT id FROM accounts WHERE name = ?", (account_name,))
        result = cursor.fetchone()
        if not result:
            print(f"Warning: Account with name {account_name} not found in accounts table, skipping {file}")
            continue
        account_id = result[0]
        update_schwab_positions(file, account_id, cursor)


def get_candles_dates(cursor: sqlite3.Cursor) -> dict:
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
    Get/Update candles (ochlv data) for all securities in the tickers db
    from the schwab api (schapi) into the db
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
    """
    table_name = f"transactions_{account_id}"
    try:
        cursor.execute(f"SELECT DISTINCT symbol FROM {table_name} WHERE symbol IS NOT NULL AND symbol != ''")
        return [row[0] for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        # The transactions table does not exist for this account
        return []


def update_db(cursor: sqlite3.Cursor, data_path: Path):
    """
    Update accounts, positions, and transactions from the indicated data_path.
    """

    create_initial_tables(cursor)
    update_all_schwab_transactions_from_dir(data_path, cursor)
    update_all_schwab_positions_from_dir(data_path, cursor)


def dump_summary(cursor: sqlite3.Cursor):
    """
    Prints latest positions for each account, showing all position rows for the most recent date.
    Prints the tickers with data and the range of dates for which there is data

    :param cursor: cursor for database connection
    :type cursor: sqlite3.Cursor
    """
    # Get all accounts
    cursor.execute("SELECT id, name, number, owner FROM accounts")
    accounts = cursor.fetchall()
    for acc in accounts:
        account_id = acc["id"]
        try:
            cursor.execute(f"SELECT COUNT(*) FROM transactions_{account_id}")
            (tx_count,) = cursor.fetchone()
        except sqlite3.OperationalError:
            tx_count = 0  # If the table doesn't exist
        # Count the number of unique position dates for this account
        table_name = f"positions_{account_id}"
        try:
            cursor.execute(f"SELECT COUNT(DISTINCT date) FROM {table_name}")
            unique_position_dates = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            unique_position_dates = 0
        account_info = (
            f"{acc['name']} ({acc['number']}, owner: {acc['owner']}), transactions: {tx_count}, position: {unique_position_dates}"
        )

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
