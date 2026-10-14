"""Storage backends: local disk and S3.

The S3 half had never been executed before these tests — it was written against
the API and left until a bucket existed. That is exactly the code most worth
pinning: the first time it runs will be in a deployment, against real IAM, where
a wrong answer looks like missing data rather than a bug.

boto3 is not needed here. S3Storage takes an injected client, so a small fake
that mimics the three call shapes it uses covers the logic without the SDK,
without credentials, and without a bucket.
"""

from __future__ import annotations

import pytest

from app.engine import storage as storage_mod
from app.engine.storage import LocalStorage, S3Storage, StorageError


class FakeClientError(Exception):
    """Shaped like botocore's ClientError, which carries the code in a dict."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Just enough S3: a dict of key -> bytes, and the four calls we make."""

    def __init__(self, objects: dict[str, bytes] | None = None, error: str | None = None):
        self.objects = dict(objects or {})
        self.error = error          # raised by every call, to simulate IAM denial
        self.put_keys: list[str] = []

    def _check(self):
        if self.error:
            raise FakeClientError(self.error)

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3's own casing
        self._check()
        self.put_keys.append(Key)
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise FakeClientError("404")
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._check()
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        return _Paginator(self)


class _Body:
    def __init__(self, data): self.data = data
    def read(self): return self.data


class _Paginator:
    def __init__(self, client): self.client = client

    def paginate(self, Bucket, Prefix):  # noqa: N803
        self.client._check()
        hits = [{"Key": k} for k in sorted(self.client.objects) if k.startswith(Prefix)]
        # Two pages, to prove the caller actually walks them.
        yield {"Contents": hits[:1]}
        yield {"Contents": hits[1:]} if hits[1:] else {}


# ── Key layout ────────────────────────────────────────────────────────────


def test_the_prefix_is_added_on_write_and_stripped_on_list():
    """A prefix is how one bucket serves several deployments. It must be
    invisible above this layer: what list_prefix returns has to be a key that
    get() accepts, or every round trip breaks."""
    client = FakeS3()
    store = S3Storage("bucket", prefix="dataalphaone", client=client)

    store.put("reports/r1/Q3.xlsx", b"PK")

    assert client.put_keys == ["dataalphaone/reports/r1/Q3.xlsx"]
    listed = store.list_prefix("reports/r1")
    assert listed == ["reports/r1/Q3.xlsx"], "prefix leaked into the returned key"
    assert store.get(listed[0]) == b"PK", "a listed key did not round trip"


def test_no_prefix_leaves_keys_untouched():
    client = FakeS3()
    store = S3Storage("bucket", client=client)

    store.put("templates/t1.xlsx", b"x")

    assert client.put_keys == ["templates/t1.xlsx"]
    assert store.list_prefix("templates") == ["templates/t1.xlsx"]


def test_listing_walks_every_page():
    """Paginated by default at 1,000 keys. A report run daily for three years
    passes that, and a caller taking only the first page would then serve a
    stale 'latest'."""
    client = FakeS3({f"reports/r1/f{i:03d}.xlsx": b"x" for i in range(5)})
    store = S3Storage("bucket", client=client)

    assert len(store.list_prefix("reports/r1")) == 5


def test_a_bucket_is_required():
    with pytest.raises(StorageError):
        S3Storage("")


# ── Absence vs inability ──────────────────────────────────────────────────


def test_a_missing_object_is_absent():
    store = S3Storage("bucket", client=FakeS3())

    assert store.exists("templates/nope.xlsx") is False
    with pytest.raises(StorageError, match="no object at"):
        store.get("templates/nope.xlsx")


def test_access_denied_is_not_reported_as_absence():
    """The bug this pins: exists() swallowed every exception, so a 403 from a
    half-configured IAM policy read as 'file not there'. The app then told the
    user to re-upload a template that was sitting in the bucket the whole time.
    """
    store = S3Storage("bucket", client=FakeS3(error="AccessDenied"))

    with pytest.raises(StorageError, match="could not check"):
        store.exists("templates/t1.xlsx")
    with pytest.raises(StorageError, match="could not read"):
        store.get("templates/t1.xlsx")


# ── Local disk ────────────────────────────────────────────────────────────


def test_local_round_trip_and_listing(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("reports/r1/a.xlsx", b"one")
    store.put("reports/r1/b.xlsx", b"two")

    assert store.get("reports/r1/a.xlsx") == b"one"
    assert store.exists("reports/r1/b.xlsx")
    assert store.list_prefix("reports/r1") == ["reports/r1/a.xlsx", "reports/r1/b.xlsx"]

    store.delete("reports/r1/a.xlsx")
    assert not store.exists("reports/r1/a.xlsx")


def test_local_refuses_a_key_that_escapes_the_root(tmp_path):
    """Keys are built from ids we mint, but this is the one place a traversal
    would land, so it is checked rather than trusted."""
    store = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageError, match="escapes the storage root"):
        store.put("../../etc/passwd", b"x")


def test_listing_a_prefix_that_does_not_exist_is_empty(tmp_path):
    assert LocalStorage(tmp_path).list_prefix("reports/never") == []


# ── Key helpers ───────────────────────────────────────────────────────────


def test_the_three_prefixes_are_distinct_and_stable():
    """uploads / templates / reports carry different retention rules, so the
    lifecycle policy keys off these paths. Changing one silently would leave
    objects with the wrong expiry."""
    assert storage_mod.upload_key("s1") == "uploads/s1.xlsx"
    assert storage_mod.template_key("t1") == "templates/t1.xlsx"
    assert storage_mod.report_key("r1", "20260819T120000Z", "Q3 Report") == (
        "reports/r1/Q3_Report_20260819T120000Z.xlsx"
    )
    assert storage_mod.reports_prefix("r1") == "reports/r1"


def test_a_report_name_cannot_break_out_of_its_folder():
    """The name comes from a text box. Slashes and dots in it must not become
    path structure."""
    key = storage_mod.report_key("r1", "20260819T120000Z", "../../etc/passwd")

    assert key.startswith("reports/r1/")
    assert ".." not in key
