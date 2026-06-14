import datetime as dt

import pandas as pd
import pytest

from conftest import FIXTURES_DIR, make_args
from roi import compute_all_history


def _fixture_path(account, start, end, key):
    return FIXTURES_DIR / f"{account}_{start}_{end}_{key}.csv"


def _check(request, df, account, start, end, key):
    """Compare df against a stored CSV fixture, or write the fixture if --update-fixtures."""
    path = _fixture_path(account, start, end, key)
    df = df.copy()
    df.index = pd.to_datetime(df.index)

    if request.config.getoption("--update-fixtures"):
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path)
        return

    if not path.exists():
        pytest.fail(f"Fixture missing: {path.name} — run pytest --update-fixtures to create it.")

    expected = pd.read_csv(path, index_col=0, parse_dates=True)
    pd.testing.assert_frame_equal(df, expected, check_exact=False, atol=0.01, rtol=1e-4, check_dtype=False, check_index_type=False)


@pytest.fixture(scope="module")
def jon_ira_h2_2025(db_cursor):
    args = make_args(
        account=["jon-ira"],
        start_date=dt.date(2025, 7, 31),
        end_date=dt.date(2025, 12, 31),
    )
    return compute_all_history(db_cursor, args)["jon-ira"]


class TestJonIraH22025:
    ACCOUNT = "jon-ira"
    START = "2025-07-31"
    END = "2025-12-31"

    def test_positions(self, request, jon_ira_h2_2025):
        _check(request, jon_ira_h2_2025["positions"], self.ACCOUNT, self.START, self.END, "positions")

    def test_cost_basis(self, request, jon_ira_h2_2025):
        _check(request, jon_ira_h2_2025["cost_basis"], self.ACCOUNT, self.START, self.END, "cost_basis")

    def test_value(self, request, jon_ira_h2_2025):
        _check(request, jon_ira_h2_2025["value"], self.ACCOUNT, self.START, self.END, "value")

    def test_income(self, request, jon_ira_h2_2025):
        _check(request, jon_ira_h2_2025["income"], self.ACCOUNT, self.START, self.END, "income")

    def test_cumulative_roi(self, request, jon_ira_h2_2025):
        _check(request, jon_ira_h2_2025["cumulative_roi"], self.ACCOUNT, self.START, self.END, "cumulative_roi")
