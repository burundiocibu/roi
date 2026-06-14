import argparse
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from roi import ProfilingCursor, compute_all_history

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DB_PATH = Path(__file__).parent.parent / "lmi.db"


def pytest_addoption(parser):
    parser.addoption(
        "--update-fixtures",
        action="store_true",
        default=False,
        help="Regenerate fixture CSVs from current output.",
    )


def make_args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        ticker=None,
        debug=False,
        interval="ME",
        all=True,
        account=None,
        equity_sum=True,
        start_date=None,
        end_date=None,
        verbosity=0,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture(scope="session")
def db_cursor():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    cursor = ProfilingCursor(conn.cursor(), enable_profiling=False)
    yield cursor
    conn.close()
