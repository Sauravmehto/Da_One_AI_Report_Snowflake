"""Semantic layer editing: read, draft, validate, publish.

The state model is deliberately small:

    published  what the model sees. Changes only on publish.
    draft      work in progress. Invisible to the model. Optional.

Editing a database that has no draft starts one from the published document,
so an admin never edits the live layer by accident. Publishing validates,
writes, discards the draft, and invalidates both loader caches.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass

from app.engine.metadata_loader import metadata_store
from app.metadata.loader import metadata_service
from app.semantic.document import SemanticDocument
from app.semantic.repository import MetadataRepository
from app.semantic.validation import Issue, LiveSchema, has_errors, validate_document

log = logging.getLogger(__name__)


class DocumentNotFoundError(Exception):
    """No published document and no draft for this database."""


class ValidationFailedError(Exception):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("Semantic layer document failed validation.")
        self.issues = issues


@dataclass(frozen=True)
class LayerState:
    """A database's editing state, as the admin UI needs to show it."""

    database: str
    published: SemanticDocument | None
    draft: SemanticDocument | None

    @property
    def has_draft(self) -> bool:
        return self.draft is not None

    @property
    def is_published(self) -> bool:
        return self.published is not None

    @property
    def editable(self) -> SemanticDocument | None:
        """What the editor should open: the draft if there is one."""
        return self.draft or self.published


class SemanticLayerService:
    def __init__(self, repository: MetadataRepository) -> None:
        self._repo = repository

    # ── Reading ──────────────────────────────────────────────────────────

    def _parse(self, data: bytes | None) -> SemanticDocument | None:
        if data is None:
            return None
        try:
            return SemanticDocument.from_toml_dict(tomllib.loads(data.decode("utf-8")))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            # A file edited by hand outside the UI can be malformed. Surface it
            # as "no document" rather than a 500 — the editor can then offer to
            # start a fresh one.
            log.error("Unparseable semantic document: %s", exc)
            return None

    def get_state(self, database: str) -> LayerState:
        return LayerState(
            database=database.upper(),
            published=self._parse(self._repo.read_published(database)),
            draft=self._parse(self._repo.read_draft(database)),
        )

    def published_databases(self) -> list[str]:
        return self._repo.list_published()

    # ── Editing ──────────────────────────────────────────────────────────

    def start_draft(self, database: str) -> SemanticDocument:
        """Open a draft, seeded from the published document if there is one."""
        state = self.get_state(database)
        if state.draft is not None:
            return state.draft

        draft = state.published or SemanticDocument(
            database=database.upper(),
            display_name=database.title(),
        )
        self.save_draft(database, draft)
        return draft

    def _carry_unmodelled(
        self, database: str, doc: SemanticDocument
    ) -> SemanticDocument:
        """Restore sections the editor does not model, from what is stored.

        `extra` is excluded from serialisation, so a document that has been to
        the browser and back always arrives with it empty — the client is never
        given data it cannot interpret, and is therefore never trusted to return
        it intact. Without this, one save through the UI silently deletes
        `[briefing]` and anything else we have not built a form for.

        Preserving here rather than in the route covers every caller, including
        future ones. The consequence to know: an unmodelled section cannot be
        DELETED through the UI, only through the file. That is the right default
        — the editor has no way to express the intent to remove one.
        """
        if doc.extra:
            return doc

        state = self.get_state(database)
        carried = (state.published.extra if state.published else {}) or (
            state.draft.extra if state.draft else {}
        )
        if not carried:
            return doc

        log.debug("Carrying unmodelled sections %s for %s", list(carried), database)
        return doc.model_copy(update={"extra": dict(carried)})

    def save_draft(self, database: str, doc: SemanticDocument) -> list[Issue]:
        """Persist a draft. Structural issues are returned, not raised.

        Autosave must not fight the person typing: a half-written document is
        the normal state of a draft, so only publishing enforces correctness.
        """
        doc = self._carry_unmodelled(database, doc)
        issues = validate_document(doc)
        self._repo.write_draft(database, doc.to_toml())
        return issues

    def discard_draft(self, database: str) -> None:
        self._repo.discard_draft(database)

    # ── Publishing ───────────────────────────────────────────────────────

    def publish(
        self,
        database: str,
        *,
        schema: LiveSchema | None = None,
    ) -> list[Issue]:
        """Make the draft authoritative. Raises if it has errors.

        Validated against the live schema, because the rules worth having are
        the ones that compare the document's claims against what actually
        exists — a date role on a TEXT column, a table that was dropped.
        """
        state = self.get_state(database)
        doc = state.draft
        if doc is None:
            raise DocumentNotFoundError(f"No draft to publish for '{database}'.")

        issues = validate_document(doc, schema=schema)
        if has_errors(issues):
            raise ValidationFailedError(issues)

        self._repo.publish(database, doc.to_toml())
        self._invalidate_loaders()
        return issues  # warnings survive publication and are worth showing

    def _invalidate_loaders(self) -> None:
        """Both caches, or the UI and the model disagree about the layer."""
        metadata_store.invalidate()
        metadata_service.invalidate()
