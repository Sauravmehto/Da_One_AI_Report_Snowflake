"""HTTP surface for Excel template reports.

The engine is covered by tests/engine/. What is unproven until here is the
route layer: that a real workbook survives a multipart upload and reaches the
scanner intact, that engine failures surface as 4xx rather than a blank 500,
and that the download endpoint cannot be talked into serving a file outside
the storage directory.

Snowflake is stubbed. The point is the plumbing, not the warehouse — and a
route test that needs a live warehouse is one nobody runs.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_session
from app.main import app
from app.session.base import SessionData

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "test_fixtures"
    / "excel"
    / "CreditFund_RiskDashboard (1).xlsx"
)


@pytest.fixture
def session() -> SessionData:
    from datetime import UTC, datetime, timedelta

    return SessionData(
        session_id="excel-routes",
        snowflake_user="SVC_USER",
        role="R",
        access_token="token",
        refresh_token_encrypted="",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_mode="keypair",
    )


@pytest.fixture
def client(monkeypatch, session) -> TestClient:
    """A client whose engine calls run inline, with no Snowflake connection."""

    async def fake_run_engine(request, sess, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        "app.api.v1.routes.excel_reports._run_engine", fake_run_engine
    )
    app.dependency_overrides[get_current_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_current_session, None)


# ── Upload ────────────────────────────────────────────────────────────────


def test_a_real_workbook_survives_the_upload_and_is_classified(client, monkeypatch):
    """The whole reason upload moved off the chat attachment path: the bytes
    arrive intact over HTTP, with no base64 round trip through a model."""
    captured: dict[str, Any] = {}

    def fake_scan(file_path=None, file_content_base64=None, file_name=None):
        captured["bytes"] = base64.b64decode(file_content_base64)
        captured["name"] = file_name
        return {"scan_id": "s1", "template_name": file_name, "sheets": []}

    monkeypatch.setattr("app.engine.excel_reports.scan_template", fake_scan)

    original = FIXTURE.read_bytes()
    response = client.post(
        "/api/v1/excel/templates/scan",
        files={"file": (FIXTURE.name, original, "application/vnd.ms-excel")},
    )

    assert response.status_code == 200
    assert captured["bytes"] == original, "workbook was altered in transit"
    assert captured["name"] == FIXTURE.name


def test_the_real_scanner_classifies_the_fixture(client, monkeypatch):
    """End to end through the route into the actual scanner — no stub — so a
    regression in either the upload path or the classifier is caught here."""
    monkeypatch.setattr(
        "app.engine.excel_reports._find_template_by_fingerprint", lambda fp: None
    )

    response = client.post(
        "/api/v1/excel/templates/scan",
        files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "application/vnd.ms-excel")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("scan_id")
    sheets = body.get("sheets") or []
    assert sheets, "the scanner found no sheets in a real workbook"
    # A template is only useful if a data sheet was identified to fill.
    assert any(s.get("role") == "data" for s in sheets), [s.get("role") for s in sheets]


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("notes.txt", b"hello", 400),          # not a workbook
        ("empty.xlsx", b"", 400),              # nothing uploaded
    ],
)
def test_bad_uploads_are_rejected(client, filename, content, expected):
    response = client.post(
        "/api/v1/excel/templates/scan",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert response.status_code == expected


def test_engine_errors_surface_as_400_not_500(client, monkeypatch):
    """A template with no data sheet is a user problem, not a server fault."""

    def boom(*args, **kwargs):
        raise __import__("app.engine.excel_reports", fromlist=["x"]).ExcelReportError(
            "template has no data sheet"
        )

    monkeypatch.setattr("app.engine.excel_reports.save_template", boom)

    response = client.post("/api/v1/excel/templates/scan-id-1/approve")

    assert response.status_code == 400
    assert "data sheet" in response.json()["detail"]


# ── Download ──────────────────────────────────────────────────────────────


def test_download_before_a_run_is_a_clear_404(client, monkeypatch):
    monkeypatch.setattr(
        "app.engine.excel_reports.latest_report_output", lambda report_id: None
    )

    response = client.get("/api/v1/excel/reports/r1/download")

    assert response.status_code == 404
    assert "run the report first" in response.json()["detail"].lower()


def test_download_refuses_a_path_outside_storage(client, monkeypatch, tmp_path):
    """The path comes from a database record. If that record were ever wrong or
    tampered with, this endpoint must not become a file-read primitive."""
    outside = tmp_path / "secrets.xlsx"
    outside.write_bytes(b"not yours")

    monkeypatch.setattr(
        "app.engine.excel_reports.latest_report_output",
        lambda report_id: {"path": str(outside), "filename": "x.xlsx"},
    )

    response = client.get("/api/v1/excel/reports/r1/download")

    assert response.status_code == 404


def test_download_serves_a_generated_workbook(client, monkeypatch):
    storage = Path(
        __import__("app.engine.excel_reports", fromlist=["x"]).EXCEL_STORAGE_DIR
    )
    generated = storage / "reports" / "r1" / "r1_test.xlsx"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(FIXTURE.read_bytes())

    monkeypatch.setattr(
        "app.engine.excel_reports.latest_report_output",
        lambda report_id: {
            "path": str(generated),
            "filename": "Meridian_Loan_Extract.xlsx",
        },
    )

    response = client.get("/api/v1/excel/reports/r1/download")

    assert response.status_code == 200
    assert "Meridian_Loan_Extract.xlsx" in response.headers["content-disposition"]
    assert response.content[:2] == b"PK", "not a zip — the workbook is corrupt"


# ── Saving a multi-block report ───────────────────────────────────────────
#
# The gap these cover: the builder could generate and download a workbook while
# persisting nothing runnable, so the Reports page listed nothing and there was
# no way to produce next month's copy.


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """A throwaway storage root, so nothing here touches the real one."""
    from app.engine import excel_reports as engine
    from app.engine import storage as storage_mod

    store = storage_mod.LocalStorage(tmp_path / "store")
    monkeypatch.setattr(storage_mod, "_storage", store)
    # save_template ALSO writes the legacy local copy; keep that out of the repo.
    monkeypatch.setattr(engine, "EXCEL_STORAGE_DIR", tmp_path / "local")
    return store


def test_publishing_saves_a_template_and_a_runnable_report(client, monkeypatch, storage):
    """One call, both halves. Approving alone is what left the Reports page empty."""
    from app.engine import excel_reports as engine
    from app.engine import storage as storage_mod

    monkeypatch.setattr(engine, "_SCAN_CACHE", {})
    upload = tmp_source(monkeypatch, engine)
    inserted: dict[str, Any] = {}
    monkeypatch.setattr(engine, "_insert_template", lambda row: None)
    monkeypatch.setattr(engine, "_insert_report", lambda row: inserted.update(row))
    monkeypatch.setattr(engine.snowflake_client, "session_identity", lambda: ("U", "R"))

    response = client.post(
        f"/api/v1/excel/scans/{upload}/publish",
        json={
            "report_title": "US Real Estate Sales",
            "block_sql": {"Raw_Sales!1": "SELECT 1 AS PROPID"},
            "parameters": [{"name": "as_of_date", "type": "date",
                            "default": "2026-08-01"}],
            "role_overrides": {"Raw_Sales": "source"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"] and body["report_id"]

    # The report row carries the WHOLE binding, not just a template pointer —
    # without the overrides a re-run could re-detect a different set of areas.
    config = inserted["parameterized_query"]
    assert config["kind"] == "multiblock"
    assert config["block_sql"] == {"Raw_Sales!1": "SELECT 1 AS PROPID"}
    assert config["role_overrides"] == {"Raw_Sales": "source"}
    assert config["parameters"][0]["name"] == "as_of_date"

    # And the workbook is in configured storage, which is where the multi-block
    # runner looks — approving used to leave it only on the local filesystem.
    assert storage.exists(storage_mod.template_key(body["template_id"]))


def test_publishing_with_no_query_is_refused(client, monkeypatch, storage):
    """A report bound to nothing would produce the template's sample rows and
    they would be read as real figures."""
    from app.engine import excel_reports as engine

    monkeypatch.setattr(engine, "_SCAN_CACHE", {})
    upload = tmp_source(monkeypatch, engine)
    monkeypatch.setattr(engine, "_insert_template", lambda row: None)
    monkeypatch.setattr(engine.snowflake_client, "session_identity", lambda: ("U", "R"))

    response = client.post(
        f"/api/v1/excel/scans/{upload}/publish",
        json={"report_title": "Empty", "block_sql": {"Raw_Sales!1": "   "}},
    )

    assert response.status_code == 400
    assert "at least one area" in response.json()["detail"]


def test_a_saved_multiblock_report_runs_through_the_normal_run_route(
    client, monkeypatch, storage
):
    """The Reports page presses one button. It must reach the multi-block runner
    without knowing that a second runner exists."""
    from app.engine import excel_reports as engine
    from app.engine import excel_multiblock, storage as storage_mod

    storage.put(storage_mod.template_key("t1"), FIXTURE.read_bytes())
    monkeypatch.setattr(engine, "_read_report", lambda rid: {
        "REPORT_ID": rid,
        "TEMPLATE_ID": "t1",
        "REPORT_TITLE": "US Real Estate Sales",
        "PARAMETERIZED_QUERY": {
            "kind": "multiblock",
            "block_sql": {"LOAN_DATA!5": "SELECT {{as_of_date}} AS D"},
            "parameters": [{"name": "as_of_date", "type": "date"}],
        },
    })
    monkeypatch.setattr(engine, "_touch_report_run", lambda rid: (3, "2026-08-19"))
    seen: dict[str, Any] = {}

    def fake_run(source, block_sql, execute_sql, **kwargs):
        seen["parameter_values"] = kwargs.get("parameter_values")
        seen["source_bytes"] = len(source)
        return {"workbook": b"PK\x03\x04 fake", "row_counts": {"LOAN_DATA!5": 42},
                "blocks_filled": [], "blocks_regenerated": [], "cells_moved": 0,
                "column_warnings": [], "lost_content": []}

    monkeypatch.setattr(excel_multiblock, "run", fake_run)
    monkeypatch.setattr(excel_multiblock, "snowflake_executor", lambda: (lambda s: s))

    response = client.post("/api/v1/excel/reports/r7/run",
                           json={"as_of_date": "2026-09-30"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert seen["parameter_values"] == {"as_of_date": "2026-09-30"}
    assert seen["source_bytes"] == len(FIXTURE.read_bytes())
    assert body["row_count"] == 42
    # The download is named after the report, not after its id.
    assert body["output_filename"].startswith("US_Real_Estate_Sales_")

    # And the generated file is where the download route will look for it.
    download = client.get("/api/v1/excel/reports/r7/download")
    assert download.status_code == 200
    assert "US_Real_Estate_Sales_" in download.headers["content-disposition"]


def test_a_multiblock_binding_error_is_a_400(client, monkeypatch, storage):
    """MultiBlockError is not ExcelReportError. Left untranslated it would reach
    the user as a blank 500 instead of the sentence explaining what to fix."""
    from app.engine import excel_reports as engine
    from app.engine import excel_multiblock, storage as storage_mod

    storage.put(storage_mod.template_key("t1"), FIXTURE.read_bytes())
    monkeypatch.setattr(engine, "_read_report", lambda rid: {
        "REPORT_ID": rid, "TEMPLATE_ID": "t1", "REPORT_TITLE": "R",
        "PARAMETERIZED_QUERY": {"kind": "multiblock",
                                "block_sql": {"A!1": "SELECT 1"}},
    })

    def boom(*args, **kwargs):
        raise excel_multiblock.MultiBlockError("No such block: A!1")

    monkeypatch.setattr(excel_multiblock, "run", boom)
    monkeypatch.setattr(excel_multiblock, "snowflake_executor", lambda: (lambda s: s))

    response = client.post("/api/v1/excel/reports/r8/run", json={})

    assert response.status_code == 400
    assert "No such block" in response.json()["detail"]


def test_a_missing_template_workbook_is_explained_not_crashed(
    client, monkeypatch, storage
):
    from app.engine import excel_reports as engine

    monkeypatch.setattr(engine, "_read_report", lambda rid: {
        "REPORT_ID": rid, "TEMPLATE_ID": "gone", "REPORT_TITLE": "R",
        "PARAMETERIZED_QUERY": {"kind": "multiblock",
                                "block_sql": {"A!1": "SELECT 1"}},
    })

    response = client.post("/api/v1/excel/reports/r9/run", json={})

    assert response.status_code == 400
    assert "Re-upload the template" in response.json()["detail"]


def tmp_source(monkeypatch, engine) -> str:
    """A cached scan whose source workbook is the real fixture."""
    engine._SCAN_CACHE["scan-1"] = {
        "scan": {"template_name": "US_RealEstate", "fingerprint": "fp",
                 "tab_roles": {}, "formula_classification": {}},
        "source_path": str(FIXTURE),
    }
    return "scan-1"


# ── Run history ───────────────────────────────────────────────────────────
#
# Every run is kept, because re-running last quarter's report today gives
# different numbers for the same as_of_date — the stored file is the only
# record of what was actually sent. That only means anything if past runs can
# be reached, which they could not: both download routes served sorted()[-1].


def test_every_run_is_listed_newest_first(client, storage):
    from app.engine import excel_reports as engine

    for stamp in ("20260601T090000Z", "20260819T090000Z", "20260715T090000Z"):
        storage.put(f"reports/r5/Q3_Report_{stamp}.xlsx", b"PK\x03\x04")

    runs = engine.list_report_outputs("r5")

    assert [r["filename"] for r in runs] == [
        "Q3_Report_20260819T090000Z.xlsx",
        "Q3_Report_20260715T090000Z.xlsx",
        "Q3_Report_20260601T090000Z.xlsx",
    ]
    assert runs[0]["generated_at"].startswith("2026-08-19")
    assert engine.latest_report_output("r5")["filename"] == runs[0]["filename"]


def test_an_older_run_can_be_downloaded_by_name(client, storage):
    storage.put("reports/r6/Q3_20260601T090000Z.xlsx", b"PK\x03\x04older")
    storage.put("reports/r6/Q3_20260819T090000Z.xlsx", b"PK\x03\x04newer")

    newest = client.get("/api/v1/excel/reports/r6/download")
    older = client.get(
        "/api/v1/excel/reports/r6/download", params={"run": "Q3_20260601T090000Z.xlsx"}
    )

    assert newest.content == b"PK\x03\x04newer"
    assert older.content == b"PK\x03\x04older"
    assert "Q3_20260601T090000Z.xlsx" in older.headers["content-disposition"]


@pytest.mark.parametrize(
    "run",
    [
        "../../templates/secret.xlsx",   # climb out of the report's folder
        "..%2F..%2Ftemplates%2Fx.xlsx",  # the same, encoded
        "Q3_20260601T090000Z.xlsx ",     # near-miss on a real name
        "other_report.xlsx",             # a file belonging to someone else
    ],
)
def test_the_run_name_cannot_address_anything_else(client, storage, run):
    """`run` arrives from the client. It is matched against this report's own
    listing rather than joined onto a path, so nothing outside is reachable —
    including a real file that belongs to a different report."""
    # Marker bytes that cannot occur in a JSON error message, so the assertion
    # tests what was SERVED rather than what the message happens to quote back.
    storage.put("reports/r7/Q3_20260601T090000Z.xlsx", b"PK\x03\x04mine")
    storage.put("reports/other/other_report.xlsx", b"\x89PAYLOAD-OTHER")
    storage.put("templates/secret.xlsx", b"\x89PAYLOAD-TEMPLATE")

    response = client.get("/api/v1/excel/reports/r7/download", params={"run": run})

    assert response.status_code == 404
    assert b"\x89PAYLOAD" not in response.content, "served a file it should not reach"


def test_a_report_never_run_lists_nothing(client, storage):
    from app.engine import excel_reports as engine

    assert engine.list_report_outputs("never-run") == []
    assert engine.latest_report_output("never-run") is None


def test_the_card_lists_only_the_most_recent_runs(client, storage, monkeypatch):
    """A monthly report accumulates faster than anyone wants to scroll, so the
    card shows a handful. It must say how many it is hiding — a list of five
    silently standing for twenty is the same trap as a truncated query that
    reports no truncation."""
    from app.engine import excel_reports as engine

    for i in range(9):
        storage.put(f"reports/r8/Q3_202608{10 + i:02d}T090000Z.xlsx", b"PK\x03\x04")
    monkeypatch.setattr(engine, "_read_report", lambda rid: {
        "REPORT_ID": rid, "TEMPLATE_ID": "t", "REPORT_TITLE": "Q3",
        "PARAMETERIZED_QUERY": {"kind": "multiblock", "block_sql": {}},
    })
    monkeypatch.setattr(engine, "_template_name", lambda tid: "T")

    body = client.get("/api/v1/excel/reports/r8").json()

    assert len(body["runs"]) == engine.REPORT_RUNS_SHOWN
    assert body["runs_total"] == 9
    assert body["runs"][0]["filename"] == "Q3_20260818T090000Z.xlsx", "not newest first"


def test_a_run_older_than_the_shown_window_still_downloads(client, storage):
    """The cap is presentation. A link to an older run — bookmarked, or sent to
    somebody — must keep working after newer runs push it past the cut."""
    for i in range(9):
        storage.put(f"reports/r9/Q3_202608{10 + i:02d}T090000Z.xlsx",
                    f"PK\x03\x04run{i}".encode())

    response = client.get("/api/v1/excel/reports/r9/download",
                          params={"run": "Q3_20260810T090000Z.xlsx"})

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04run0", "the oldest run became unreachable"


# ── Surviving more than one worker ────────────────────────────────────────
#
# Production runs `uvicorn --workers 4`. The scan cache is one process's dict,
# so the upload and the save that follows it land on different workers at
# random and the builder worked about one time in four. These pin the fix:
# everything the save needs is readable by any worker.


def test_a_scan_can_be_saved_by_a_worker_that_never_saw_the_upload(
    client, storage, monkeypatch
):
    from app.engine import excel_reports as engine

    monkeypatch.setattr(engine, "_insert_template", lambda row: None)
    monkeypatch.setattr(engine.snowflake_client, "session_identity", lambda: ("U", "R"))
    monkeypatch.setattr(engine, "_find_template_by_fingerprint", lambda fp: None)

    scanned = client.post(
        "/api/v1/excel/templates/scan",
        files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "application/vnd.ms-excel")},
    ).json()

    # The worker that saves is not the one that scanned: its cache is empty.
    engine._SCAN_CACHE.clear()

    response = client.post(f"/api/v1/excel/templates/{scanned['scan_id']}/approve")

    assert response.status_code == 200, response.text
    assert response.json()["template_id"]


def test_a_scan_id_that_never_existed_is_still_refused(client, storage):
    response = client.post("/api/v1/excel/templates/nope/approve")

    assert response.status_code == 400
    assert "No scan found" in response.json()["detail"]


def test_the_stored_scan_carries_the_classification_not_just_the_bytes(
    client, storage, monkeypatch
):
    """The bytes alone are not enough — save_template also persists tab roles
    and the formula classification, which only exist in the scan result."""
    from app.engine import excel_reports as engine
    from app.engine import storage as storage_mod

    monkeypatch.setattr(engine, "_find_template_by_fingerprint", lambda fp: None)
    scanned = client.post(
        "/api/v1/excel/templates/scan",
        files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "application/vnd.ms-excel")},
    ).json()
    engine._SCAN_CACHE.clear()

    entry = engine._scan_entry(scanned["scan_id"])

    assert entry is not None
    assert entry["scan"]["tab_roles"], "roles lost between workers"
    assert entry["scan"]["formula_classification"] is not None
    assert entry["data"][:2] == b"PK", "the workbook did not survive"
    # Metadata sits beside the upload so the 7-day lifecycle rule sweeps both.
    assert storage.exists(storage_mod.upload_key(scanned["scan_id"]))
    assert storage.exists(f"uploads/{scanned['scan_id']}.json")
