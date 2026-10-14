"""Who owns a dashboard, and which group it belongs to.

Editing a dashboard inserts a NEW version row under the editor's name. The list
reads the current row, so "owner" quietly became "whoever touched it last" —
including for a change as small as dragging a panel. The owner is the author of
version 1, which was never lost, only never read.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.api.v1.routes.dashboards import (
    FILE_DATA_GROUP,
    UNGROUPED,
    _data_group,
)
from app.engine import dashboards
from app.engine.connectors.snowflake import client as snowflake_client


# --- the query asks for the right thing -------------------------------------

def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_the_owner_comes_from_the_lowest_version_not_the_highest():
    """The direction of that ORDER BY is the whole fix. Reversed, OWNER is the
    newest row again and the bug is back with the tests still green."""
    sql = _flat(snowflake_client._LIST_WITH_DATABASE)
    assert (
        "FIRST_VALUE(CREATED_BY) OVER ( PARTITION BY REPORT_ID ORDER BY VERSION) AS OWNER"
        in sql
    ), sql
    assert "ORDER BY VERSION DESC) AS OWNER" not in sql


def test_the_creation_time_also_comes_from_the_first_version():
    sql = _flat(snowflake_client._LIST_WITH_DATABASE)
    assert (
        "FIRST_VALUE(CREATED_AT) OVER ( PARTITION BY REPORT_ID ORDER BY VERSION) AS CREATED_AT"
        in sql
    ), sql
    assert "ORDER BY VERSION DESC) AS CREATED_AT" not in sql


def test_the_last_editor_is_reported_separately_from_the_owner():
    sql = snowflake_client._LIST_WITH_DATABASE
    assert "CREATED_BY AS UPDATED_BY" in sql
    assert "CREATED_AT AS UPDATED_AT" in sql


def test_only_the_current_version_is_listed():
    assert "QUALIFY IS_CURRENT = TRUE" in snowflake_client._LIST_WITH_DATABASE


def test_the_fallback_query_differs_only_by_the_new_columns():
    """A deployment that has not run the migration still lists its dashboards;
    it just cannot group them."""
    with_db = snowflake_client._LIST_WITH_DATABASE
    without = snowflake_client._LIST_WITHOUT_DATABASE
    for column in ("DATABASE_NAME", "DATA_GROUP"):
        assert column in with_db
        assert column not in without
    assert without == with_db.replace("DATABASE_NAME, DATA_GROUP, ", "")
    # Everything else — owner, dates, run counts — must survive the fallback.
    for kept in ("AS OWNER", "AS UPDATED_BY", "RUN_COUNT", "QUALIFY IS_CURRENT"):
        assert kept in without


# --- the rows are mapped to the right names ---------------------------------

def test_owner_and_editor_are_reported_separately(monkeypatch):
    monkeypatch.setattr(
        snowflake_client, "list_saved_dashboards",
        lambda limit: [{
            "REPORT_ID": "r1", "REPORT_TITLE": "T", "VERSION": 4,
            "OWNER": "PRASHANT", "CREATED_AT": "2026-01-01T00:00:00",
            "UPDATED_BY": "SAURABH", "UPDATED_AT": "2026-09-01T00:00:00",
            "DATABASE_NAME": "LOGISTICS_DB",
            "LAST_RUN_AT": None, "RUN_COUNT": 3,
        }],
    )
    item = dashboards.list_dashboards()[0]
    assert item["created_by"] == "PRASHANT"        # who owns it
    assert item["updated_by"] == "SAURABH"         # who last changed it
    assert item["created_at"] == "2026-01-01T00:00:00"
    assert item["updated_at"] == "2026-09-01T00:00:00"
    assert item["database"] == "LOGISTICS_DB"


# --- every write records the database ---------------------------------------

def test_the_insert_binds_a_database_for_every_version():
    stmt = snowflake_client._DASHBOARD_INSERT
    assert "DATABASE_NAME" in stmt
    assert stmt.count("%s") == len(
        snowflake_client._dashboard_insert_values({
            "report_id": "r", "version_id": "r_v1", "report_title": "T",
            "dashboard_plan": {}, "created_by": "X", "database_name": "D",
        })
    ), "the column list and the bound values must stay the same length"


def test_a_saved_dashboard_records_its_database(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(dashboards, "get_cached_plan",
                        lambda pid: {"plan": {"title": "T", "panels": []},
                                     "database": "fintech_db"})
    monkeypatch.setattr(snowflake_client, "session_identity", lambda: ("NYSA", "R"))
    monkeypatch.setattr(snowflake_client, "write_saved_dashboard", captured.update)
    dashboards.save_plan("pid", "My dashboard")
    assert captured["database_name"] == "FINTECH_DB"
    assert captured["created_by"] == "NYSA"


def test_every_version_insert_carries_the_database():
    """A layout change is an insert too. Miss one and the dashboard leaves its
    group the first time somebody drags a panel."""
    source = Path(dashboards.__file__).read_text(encoding="utf-8")
    inserts = source.count("snowflake_client.insert_dashboard_version({")
    carried = source.count('"database_name": _infer_database(')
    assert inserts == carried, (
        f"{inserts} version inserts but only {carried} record the database"
    )


# --- grouping ---------------------------------------------------------------

@pytest.mark.parametrize(
    "item, expected",
    [
        ({"source_type": "snowflake", "database": "fintech_db"}, "FINTECH_DB"),
        ({"source_type": "snowflake", "database": "  logistics_db "}, "LOGISTICS_DB"),
        ({"source_type": "snowflake", "database": None}, UNGROUPED),
        ({"source_type": "snowflake", "database": ""}, UNGROUPED),
        ({"source_type": "file", "source_key": "uploads/rent"}, FILE_DATA_GROUP),
    ],
)
def test_group_assignment(item, expected):
    assert _data_group(item) == expected


def test_uploads_share_one_group_rather_than_one_each():
    a = _data_group({"source_type": "file", "source_key": "uploads/a"})
    b = _data_group({"source_type": "file", "source_key": "uploads/b"})
    assert a == b == FILE_DATA_GROUP


# --- the title as a fallback source of the database -------------------------
#
# Rows saved before DATABASE_NAME existed have nothing to group by. The planner
# has been putting the database in quotes at the end of the title all along, so
# that is read when the column is empty. These are real titles, warts included:
# an unclosed quote, a stray bracket, a hyphen inside the name.

@pytest.mark.parametrize(
    "title, expected",
    [
        ('Sales & Funnel Performance "ECOMMERCE"', "ECOMMERCE_DB"),
        ('Freight & Logistics Lane Performance - "LOGISTICS"', "LOGISTICS_DB"),
        ('Store Performance & Conversion "RETAIL"', "RETAIL_DB"),
        ('Merchant & GTM "PAYMENTS"', "PAYMENTS_DB"),
        # The closing quote was never typed.
        ('GTM, Sales & Distribution "HOSPITALITY', "HOSPITALITY_DB"),
        # A bracket after the closing quote, and mixed case.
        ('GCC Digital Lending & Neobank - Executive Overview "Fintech")', "FINTECH_DB"),
        # Punctuation inside the name.
        ('Marketing & Acquisition : "E-Commerce"', "ECOMMERCE_DB"),
        ('Sales & Customer Performance "Logistics"', "LOGISTICS_DB"),
    ],
)
def test_the_database_can_be_read_from_the_title(title, expected):
    assert dashboards.infer_database_from_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Retail Performance Diagnostics - GCC Stores",   # no quotes at all
        "US Real Estate Market Overview",
        'The "big" report for Q3',                       # quoted, but mid-title
        'Something "NOT_A_DATABASE"',
        "",
        None,
    ],
)
def test_a_title_that_names_no_database_groups_nowhere(title):
    """Silence beats a wrong folder: a dashboard filed under the wrong database
    is harder to find than one sitting in Ungrouped."""
    assert dashboards.infer_database_from_title(title) is None


def test_the_stored_column_beats_the_title():
    """The title is a claim; the column is what the dashboard actually queried."""
    item = {
        "source_type": "snowflake",
        "database": "FINTECH_DB",
        "report_title": 'Misleading title "RETAIL"',
    }
    assert _data_group(item) == "FINTECH_DB"


def test_the_title_is_used_when_the_column_is_empty():
    item = {
        "source_type": "snowflake",
        "database": None,
        "report_title": 'Store Performance & Conversion "RETAIL"',
    }
    assert _data_group(item) == "RETAIL_DB"


def test_an_upload_ignores_a_database_name_in_its_title():
    item = {
        "source_type": "file",
        "report_title": 'Rent roll "LOGISTICS"',
    }
    assert _data_group(item) == FILE_DATA_GROUP


def test_an_ambiguous_title_token_is_refused(monkeypatch):
    """Two databases could answer to one token — file under neither.

    No current title is ambiguous, so without forcing the case this rule would
    never be exercised and could be removed without a test noticing.
    """
    monkeypatch.setattr(
        dashboards.metadata_store, "documented_databases",
        lambda: ["SALES", "SALES_DB"],
    )
    assert dashboards.infer_database_from_title('Q3 review "SALES"') is None


def test_a_single_match_still_resolves_under_the_same_rule(monkeypatch):
    monkeypatch.setattr(
        dashboards.metadata_store, "documented_databases",
        lambda: ["SALES_DB", "LOGISTICS_DB"],
    )
    assert dashboards.infer_database_from_title('Q3 review "SALES"') == "SALES_DB"


# --- moving a dashboard between folders -------------------------------------

def test_a_manual_folder_outranks_the_database():
    """The point of the override: a dashboard filed by hand stays where it was
    put, whatever its SQL queries."""
    item = {
        "source_type": "snowflake",
        "database": "FINTECH_DB",
        "data_group_override": "RealEstate",
        "report_title": 'Something "RETAIL"',
    }
    assert _data_group(item) == "RealEstate"


def test_clearing_the_override_returns_it_to_its_database():
    item = {
        "source_type": "snowflake",
        "database": "FINTECH_DB",
        "data_group_override": None,
        "report_title": "x",
    }
    assert _data_group(item) == "FINTECH_DB"


def test_an_override_of_only_whitespace_is_not_a_folder():
    item = {
        "source_type": "snowflake",
        "database": "FINTECH_DB",
        "data_group_override": "   ",
        "report_title": "x",
    }
    assert _data_group(item) == "FINTECH_DB"


def test_moving_writes_no_new_version(monkeypatch):
    """Filing is not editing. A new version row would put "moved folder" in the
    dashboard's history and bump the version number for everyone."""
    calls: list = []
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"DASHBOARD_PLAN": {"panels": []}})
    monkeypatch.setattr(snowflake_client, "set_dashboard_group",
                        lambda rid, group: calls.append((rid, group)))
    monkeypatch.setattr(
        snowflake_client, "insert_dashboard_version",
        lambda row: pytest.fail("moving a dashboard must not create a version"),
    )
    dashboards.set_group("r1", "RealEstate")
    assert calls == [("r1", "RealEstate")]


def test_moving_an_unknown_dashboard_is_refused(monkeypatch):
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard", lambda rid: None)
    with pytest.raises(dashboards.DashboardError):
        dashboards.set_group("nope", "RealEstate")


def test_the_update_touches_every_version_row():
    """Reverting to an older version must not undo a move, so the column is set
    per REPORT_ID rather than on the current row alone."""
    import inspect

    body = inspect.getsource(snowflake_client.set_dashboard_group)
    assert "WHERE REPORT_ID = %s" in body
    assert "IS_CURRENT" not in body


# --- saving must work on a table that has not been migrated -----------------
#
# The grouping columns are optional to READ — the list falls back — but the
# INSERT named DATABASE_NAME unconditionally, so on an unmigrated table every
# save and every edit failed on an invalid identifier. An optional feature has
# to be optional on both sides.

class _FakeCursor:
    def __init__(self, has_columns: bool, log: list):
        self.has_columns = has_columns
        self.log = log
        self.description = []

    def execute(self, statement, params=None, **kwargs):
        self.log.append((statement, params))
        names = statement.upper()
        if not self.has_columns and ("DATABASE_NAME" in names or "DATA_GROUP" in names):
            raise RuntimeError("002000 (42S22): SQL compilation error: "
                               "invalid identifier 'DATABASE_NAME'")

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, has_columns: bool, log: list):
        self.has_columns = has_columns
        self.log = log

    def cursor(self):
        return _FakeCursor(self.has_columns, self.log)


@pytest.fixture
def unmigrated(monkeypatch):
    log: list = []
    monkeypatch.setattr(snowflake_client, "_group_columns", None, raising=False)
    monkeypatch.setattr(snowflake_client, "get_connection",
                        lambda: _FakeConn(False, log))
    return log


@pytest.fixture
def migrated(monkeypatch):
    log: list = []
    monkeypatch.setattr(snowflake_client, "_group_columns", None, raising=False)
    monkeypatch.setattr(snowflake_client, "get_connection",
                        lambda: _FakeConn(True, log))
    return log


ROW = {
    "report_id": "r1", "version_id": "r1_v1", "version": 1,
    "report_title": "T", "description": None, "tags": [],
    "dashboard_plan": {"panels": []}, "created_by": "SAURABH",
    "edit_note": None, "database_name": "FINTECH_DB",
}


def test_a_first_save_works_without_the_column(unmigrated):
    snowflake_client.write_saved_dashboard(dict(ROW))
    inserts = [sql for sql, _ in unmigrated if sql.upper().startswith("INSERT")]
    assert inserts, "nothing was inserted"
    assert "DATABASE_NAME" not in inserts[-1]


def test_an_edit_works_without_the_column(unmigrated):
    """This is the one that broke: every Save and refresh after an edit."""
    snowflake_client.insert_dashboard_version(dict(ROW))
    inserts = [sql for sql, _ in unmigrated if sql.upper().startswith("INSERT")]
    assert inserts and "DATABASE_NAME" not in inserts[-1]


def test_the_bound_values_match_the_column_list(unmigrated):
    snowflake_client.write_saved_dashboard(dict(ROW))
    sql, params = [entry for entry in unmigrated
                   if entry[0].upper().startswith("INSERT")][-1]
    assert sql.count("%s") == len(params)


def test_a_migrated_table_still_records_the_database(migrated):
    snowflake_client.write_saved_dashboard(dict(ROW))
    sql, params = [entry for entry in migrated
                   if entry[0].upper().startswith("INSERT")][-1]
    assert "DATABASE_NAME" in sql
    assert params[-1] == "FINTECH_DB"
    assert sql.count("%s") == len(params)


def test_the_probe_is_asked_once(unmigrated):
    snowflake_client.write_saved_dashboard(dict(ROW))
    snowflake_client.write_saved_dashboard(dict(ROW))
    probes = [sql for sql, _ in unmigrated if "WHERE FALSE" in sql.upper()]
    assert len(probes) == 1, "the capability should be remembered, not re-asked"


def test_moving_says_what_to_do_when_it_cannot(unmigrated):
    """Better than a raw driver error nobody can act on."""
    with pytest.raises(RuntimeError, match="dashboard_database_name.sql"):
        snowflake_client.set_dashboard_group("r1", "RealEstate")
