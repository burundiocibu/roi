#!/usr/bin/env python3
import unittest
import sqlite3
import datetime as dt

from lmidb import create_tables, add_account, add_ticker, add_transaction


class TestLmiDB(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        create_tables(self.cursor)

    def tearDown(self):
        self.conn.close()

    def test_create_tables(self):
        # Try to select from tables to ensure they exist
        for table in ["transactions", "accounts", "tickers", "ochlv"]:
            self.cursor.execute(f"SELECT * FROM {table}")

    def test_add_account(self):
        add_account(self.cursor, "123", "Test Account", "Alice")
        # AUTOINCREMENT - id is automatic, so check by account
        self.cursor.execute("SELECT number, name, owner FROM accounts WHERE number='123'")
        row = self.cursor.fetchone()
        self.assertEqual(row, ("123", "Test Account", "Alice"))

    def test_add_ticker(self):
        add_ticker(self.cursor, "AAPL")
        self.cursor.execute("SELECT symbol FROM tickers WHERE symbol='AAPL'")
        row = self.cursor.fetchone()
        self.assertEqual(row, ("AAPL",))

    def test_add_transaction(self):
        # Precondition: At least one account and ticker
        add_account(self.cursor, "124", "Broker Acct", "Bob")
        add_ticker(self.cursor, "MSFT")
        date = dt.datetime(2024, 6, 27, 14, 0, 0)
        add_transaction(self.cursor, date, "124", "BUY", "MSFT", "Buy Microsoft shares", 5, 300.0, 1.5, 1500.0)
        self.cursor.execute(
            "SELECT date, account, action, symbol, description, quantity, price, fees, amount FROM transactions WHERE account='124'"
        )
        row = self.cursor.fetchone()
        self.assertEqual(row[0][:19], date.isoformat()[:19])  # SQLite may drop microseconds
        self.assertEqual(row[1:], ("124", "BUY", "MSFT", "Buy Microsoft shares", 5.0, 300.0, 1.5, 1500.0))


if __name__ == "__main__":
    unittest.main()
