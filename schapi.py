#!/usr/bin/env -S uv run
"""
A wrapper to the schwab_py to get historical data.
https://schwab-py.readthedocs.io/en/latest/ for the underlying api
"""

import datetime as dt
import httpx
import json
import pandas as pd
import schwab
from schwab.auth import easy_client
import shlex


def get_config(fn: str):
    tokens_dict = {}
    with open(fn) as f:
        for line in f:
            tokens = shlex.split(line, posix=True)
            for token in tokens:
                if "=" in token:
                    key, value = token.split("=", 1)
                    tokens_dict[key] = value
    return tokens_dict


def get_client():
    config = get_config(".secrets")
    client = easy_client(
        api_key=config["APP_KEY"],
        app_secret=config["SECRET"],
        callback_url=config["CALLBACK_URL"],
        token_path="/tmp/token.json",
    )
    print(f"Client token is {client.token_age()/86400:.2} days old. (7 is max)")
    return client


def get_daily(client, ticker, start_datetime=None, end_datetime=None):
    resp = client.get_price_history_every_day(ticker, start_datetime=start_datetime, end_datetime=end_datetime)
    assert resp.status_code == httpx.codes.OK
    return resp.json()


def get_instrument(client, symbol):
    """Fetch instrument metadata for a single symbol.
    Returns the first instrument dict from the response, or None if not found."""
    from schwab.client import Client

    resp = client.get_instruments([symbol], projection=Client.Instrument.Projection.SYMBOL_SEARCH)
    assert resp.status_code == httpx.codes.OK
    instruments = resp.json().get("instruments", [])
    return instruments[0] if instruments else None


def history_to_dataframe(history_json):
    """Convert Schwab historical price JSON to Pandas DataFrame"""
    print(f"{history_json}")
    candles = history_json.get("candles", [])
    if len(candles) == 0:
        return None
    df = pd.DataFrame(candles)
    if not df.empty and "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("datetime")
        df.index = df.index.date
    return df


if __name__ == "__main__":
    # if it asks for verification, use the account login, not the dev portal login
    global df, client, transactions
    client = get_client()
    df = history_to_dataframe(get_daily(client, "NET"))  # cloudflare
    print(df)

    # So. I could never get the transactions to be returned. Also, I could only "see" "my" accounts
    # when I enabled the accesss. Keeping this code in case I want to re-visit using the api to
    # get position and transaction data. For now just using the manual download of those data.
    # accounts = client.get_account_numbers()
    # for a in accounts.json():
    #    print(f"account: {a["accountNumber"]}, hash:{a["hashValue"]}")
    #    # acct = client.get_account(a["hashValue"], fields=schwab.client.Client.Account.Fields.POSITIONS)
    #    # print(f"positions:\n{json.dumps(acct.json(), indent=2)}")
    #    transactions = client.get_transactions(
    #        a["hashValue"],
    #        start_date=dt.datetime(2025, 6, 1),
    #        end_date=dt.datetime(2025, 8, 1),
    #        transaction_types=schwab.client.Client.Transactions.TransactionType.TRADE,
    #    )
    #    print(transactions)
    #    print(f"transactions:\n{json.dumps(transactions.json(), indent=2)}")
