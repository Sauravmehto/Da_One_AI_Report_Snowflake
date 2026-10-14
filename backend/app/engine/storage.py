"""Where template workbooks and generated reports live.

Local disk today, S3 when a bucket exists. The interface is deliberately tiny —
put / get / exists / list / delete over opaque keys — because that is the whole
surface both backends need, and anything richer would leak filesystem
assumptions into code that later has to run against object storage.

Keys are POSIX-style and relative: `templates/<id>.xlsx`,
`reports/<id>/<id>_<stamp>.xlsx`. No absolute paths cross this boundary, so
nothing downstream can accidentally depend on there being a filesystem.

Why the engine hands back BYTES rather than writing a file: openpyxl reads and
writes file-like objects perfectly well, and a `run()` that returns a workbook
can be stored anywhere by its caller. A `run()` that writes to a path can only
ever work on a disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class StorageError(Exception):
    """A stored object could not be read or written."""


@runtime_checkable
class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    """Files under a root directory. The deployment note that matters: this is
    container-local, so without a mounted volume every approved template and
    generated report is lost on redeploy."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # Keys come from our own code, but resolve and re-check anyway: joining
        # a caller-supplied key onto a root is exactly where traversal lands.
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise StorageError(f"key escapes the storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no object at {key!r}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except StorageError:
            return False

    def list_prefix(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        root = self.root.resolve()
        return sorted(
            p.relative_to(root).as_posix()
            for p in base.rglob("*")
            if p.is_file()
        )

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


def _is_not_found(exc: Exception) -> bool:
    """Whether a botocore error means "no such object" rather than "could not
    ask". Read off the error payload by duck-typing so this module keeps its
    lazy boto3 import — nothing here should require the AWS SDK to be installed
    just to run against local disk."""
    code = getattr(exc, "response", None)
    if not isinstance(code, dict):
        return False
    return str(code.get("Error", {}).get("Code", "")) in {
        "404", "NoSuchKey", "NotFound",
    }


class S3Storage:
    """Objects in a bucket, under an optional key prefix.

    Downloads still stream through the backend rather than via a presigned URL:
    presigning is cheaper on bandwidth but produces a link that bypasses the
    session check, and these are investor reports. Workbooks are small enough
    that proxying costs nothing.
    """

    def __init__(self, bucket: str, prefix: str = "", client=None):
        if not bucket:
            raise StorageError("S3 storage needs a bucket name")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3  # imported lazily so local runs need no AWS SDK
            except ImportError as exc:  # pragma: no cover
                raise StorageError(
                    "S3 storage requires boto3 (`pipenv install boto3`)."
                ) from exc
            self._client = boto3.client("s3")
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return key

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # noqa: BLE001 - botocore raises many shapes
            if _is_not_found(exc):
                raise StorageError(f"no object at {key!r}") from exc
            # Anything else — denied, throttled, wrong region — is NOT absence.
            raise StorageError(
                f"could not read {key!r} from s3://{self.bucket}: {exc}"
            ) from exc
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        """True/False for presence — but only when presence is what was learned.

        This used to answer False for every failure, which made an IAM
        misconfiguration indistinguishable from a missing file. On a first S3
        deployment that is the likeliest failure of all, and it surfaced to the
        user as "no stored workbook for this template — re-upload it": advice
        that cannot work and hides the real cause. A permissions or network
        error now raises instead.
        """
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:  # noqa: BLE001 - botocore raises many shapes
            if _is_not_found(exc):
                return False
            raise StorageError(
                f"could not check {key!r} in s3://{self.bucket}: {exc}"
            ) from exc

    def list_prefix(self, prefix: str) -> list[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        full = self._key(prefix)
        cut = len(self.prefix) + 1 if self.prefix else 0
        out: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for item in page.get("Contents", []):
                out.append(item["Key"][cut:])
        return sorted(out)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


_storage: Storage | None = None


def get_storage() -> Storage:
    """The configured backend, built once.

    `EXCEL_STORAGE_BACKEND=s3` plus `EXCEL_S3_BUCKET` switches to S3; anything
    else stays on local disk. Deliberately env-driven rather than automatic —
    a deployment that half-configures S3 should fail loudly at startup, not
    silently write reports to a container filesystem that is about to vanish.
    """
    global _storage
    if _storage is not None:
        return _storage

    backend = os.environ.get("EXCEL_STORAGE_BACKEND", "local").strip().lower()
    if backend == "s3":
        _storage = S3Storage(
            os.environ.get("EXCEL_S3_BUCKET", ""),
            os.environ.get("EXCEL_S3_PREFIX", ""),
        )
    else:
        from app.engine import excel_reports
        _storage = LocalStorage(excel_reports.EXCEL_STORAGE_DIR)
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Override the backend (tests, or a request-scoped bucket)."""
    global _storage
    _storage = storage


# --- key helpers, so the layout is stated once ------------------------------

def upload_key(scan_id: str) -> str:
    """A workbook that has been uploaded but not yet approved."""
    return f"uploads/{scan_id}.xlsx"


# --- small shared blobs -----------------------------------------------------
#
# Several flows are two-step: propose something, then save the exact thing that
# was proposed. Each held the proposal in a module-level dict, which is one
# worker's memory — so behind `--workers 4` the second step landed on a process
# that had never seen the first, and the user got "it may have expired" for
# something created seconds earlier. These put that state where every worker
# can read it.

def put_json(key: str, value: Any) -> None:
    """Best effort: the in-process cache still covers the single-worker case,
    so a storage failure degrades to the old behaviour rather than breaking the
    step that was working."""
    try:
        get_storage().put(key, json.dumps(value, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001 - shared state is the improvement, not the path
        pass


def get_json(key: str) -> Any | None:
    try:
        store = get_storage()
        if not store.exists(key):
            return None
        return json.loads(store.get(key).decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreadable shared state is a cache miss
        return None


def drop(key: str) -> None:
    try:
        get_storage().delete(key)
    except Exception:  # noqa: BLE001
        pass


def plan_key(plan_id: str) -> str:
    """A proposed dashboard, between plan and save."""
    return f"plans/{plan_id}.json"


def file_plan_key(plan_id: str) -> str:
    """A proposed file-source dashboard, between plan and save."""
    return f"file_plans/{plan_id}.json"


def file_dashboard_key(report_id: str) -> str:
    """A saved dashboard backed by an uploaded file or folder."""
    return f"file_dashboards/{report_id}.json"


def file_edit_key(report_id: str) -> str:
    """A previewed edit for a file-source dashboard, between preview and commit."""
    return f"file_edits/{report_id}.json"


def file_version_key(report_id: str, version: int) -> str:
    """An archived version snapshot for a file-source dashboard."""
    return f"file_dashboard_versions/{report_id}/v{version}.json"


def edit_key(report_id: str) -> str:
    """A previewed edit, between preview and commit. Keyed by report because a
    conversation edits one dashboard at a time."""
    return f"plans/edit_{report_id}.json"


def panel_description_key(fingerprint: str) -> str:
    """A plain-English explanation of one panel, keyed by what the panel IS.

    The fingerprint covers the title, chart type and columns — not the report
    id — so two dashboards showing the same chart share one description, and a
    panel that is edited gets a new key rather than a stale explanation.
    """
    return f"panel_descriptions/{fingerprint}.json"


def narrative_key(report_id: str, fingerprint: str) -> str:
    """The executive narrative for one dashboard RUN, not one dashboard.

    The fingerprint covers the numbers the narrative quotes, so a refreshed
    dashboard whose figures moved gets a new key rather than a stale sentence
    describing last week's position. A dashboard re-opened against unchanged
    data hits the same key on any worker, which is the common case — the UI
    asks for this on every load.
    """
    return f"narratives/{report_id}/{fingerprint}.json"


def template_key(template_id: str) -> str:
    return f"templates/{template_id}.xlsx"


def slugify(name: str, fallback: str = "report") -> str:
    """A filename-safe version of a report's name.

    The download used to be called `b46910b25b8a4cbfb7c6df4178b7fa7a_2026…xlsx`
    — the internal id, which means nothing to whoever opens it. The name the
    person typed is the only part they recognise, so it leads.
    """
    import re

    slug = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_")
    return (slug[:60] or fallback)


def report_key(owner: str, stamp: str, name: str | None = None) -> str:
    """`reports/<owner>/<Report_Name>_<stamp>.xlsx`.

    `owner` is a report_id for a saved report and a template_id for a one-off
    run from the builder, which has no report yet. Either way the id stays in the
    PATH so runs group together and cannot collide when two reports share a name;
    the filename is the human part, because that is what a download shows.
    """
    return f"reports/{owner}/{slugify(name or owner)}_{stamp}.xlsx"


def reports_prefix(owner: str) -> str:
    return f"reports/{owner}"
