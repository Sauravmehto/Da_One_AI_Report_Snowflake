"""Semantic layer authoring — the admin-facing side of the metadata TOMLs.

Reading them is the engine's job (app/engine/metadata_loader.py) and the app's
(app/metadata/loader.py). This package is about *writing* them: a typed
document, a storage seam, validation, and draft/publish.
"""

from app.semantic.document import MetricDoc, SemanticDocument, TableDoc
from app.semantic.repository import LocalDirectoryRepository, MetadataRepository
from app.semantic.service import (
    DocumentNotFoundError,
    LayerState,
    SemanticLayerService,
    ValidationFailedError,
)
from app.semantic.validation import Issue, has_errors, validate_document

__all__ = [
    "DocumentNotFoundError",
    "Issue",
    "LayerState",
    "LocalDirectoryRepository",
    "MetadataRepository",
    "MetricDoc",
    "SemanticDocument",
    "SemanticLayerService",
    "TableDoc",
    "ValidationFailedError",
    "has_errors",
    "validate_document",
]
