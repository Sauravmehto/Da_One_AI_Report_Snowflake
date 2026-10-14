from unittest.mock import MagicMock

import pytest

from app.metadata.loader import MetadataService
from app.services.ai_query import AIQueryService


@pytest.fixture
def metadata() -> MetadataService:
    return MetadataService()


def test_chart_catalog_loads(metadata: MetadataService) -> None:
    catalog = metadata.get_chart_catalog()
    assert catalog.get("version") == 1
    assert catalog.get("catalog") == "chart_types"

    types = catalog.get("types") or {}
    expected = {"stat", "table", "bar", "line", "area", "pie", "radar"}
    assert expected.issubset(set(types.keys()))


def test_all_seven_types_present(metadata: MetadataService) -> None:
    types = metadata.get_chart_catalog().get("types") or {}
    assert len(types) >= 7
    for type_id in ("stat", "table", "bar", "line", "area", "pie", "radar"):
        meta = metadata.get_chart_type(type_id)
        assert meta is not None
        assert meta.get("summary")


def test_qa_chart_types(metadata: MetadataService) -> None:
    qa_types = metadata.get_qa_chart_types()
    assert "bar" in qa_types
    assert "line" in qa_types
    assert "area" in qa_types
    assert "pie" in qa_types
    assert "none" in qa_types
    assert "stat" not in qa_types
    assert "radar" not in qa_types


def test_build_chart_catalog_context_v1_only(metadata: MetadataService) -> None:
    context = metadata.build_chart_catalog_context(v1_only=True)
    assert "## Chart Catalog (v1)" in context
    assert "### bar" in context
    assert "### line" in context
    assert "### radar" not in context
    assert "### stat" not in context
    assert "Global selection rules" in context


def test_build_chart_catalog_context_includes_all_when_not_v1_only(
    metadata: MetadataService,
) -> None:
    context = metadata.build_chart_catalog_context(v1_only=False)
    assert "### stat" in context
    assert "### radar" in context


def test_modifier_allowed_values(metadata: MetadataService) -> None:
    assert metadata.get_chart_modifier_allowed("bar", "series") == [
        "single",
        "grouped",
        "stacked",
    ]
    assert metadata.get_chart_modifier_allowed("line", "curve") == [
        "linear",
        "natural",
        "step",
    ]
    assert metadata.get_chart_modifier_allowed("area", "stackOffset") == [
        "default",
        "expand",
    ]
    assert metadata.get_chart_modifier_allowed("pie", "variant") == [
        "pie",
        "donut",
        "nested",
    ]


def test_validate_chart_modifiers_strips_invalid_keys(
    metadata: MetadataService,
) -> None:
    svc = AIQueryService(MagicMock(), MagicMock(), MagicMock(), metadata)
    spec = {
        "type": "bar",
        "x": "state",
        "y": ["total", "count"],
        "title": "By State",
        "modifiers": {
            "orientation": "horizontal",
            "series": "grouped",
            "invalid_modifier": "nope",
            "negative": True,
        },
    }
    result = svc._validate_chart(spec, ["state", "total", "count"])
    assert result is not None
    assert result["type"] == "bar"
    assert result["modifiers"] == {
        "orientation": "horizontal",
        "series": "grouped",
        "negative": True,
    }


def test_validate_chart_color_scale_sequential(metadata: MetadataService) -> None:
    svc = AIQueryService(MagicMock(), MagicMock(), MagicMock(), metadata)
    bar_result = svc._validate_chart(
        {
            "type": "bar",
            "x": "state",
            "y": ["total"],
            "title": "By State",
            "modifiers": {"colorScale": "sequential", "colorScale_invalid": "nope"},
        },
        ["state", "total"],
    )
    assert bar_result is not None
    assert bar_result["modifiers"] == {"colorScale": "sequential"}

    pie_result = svc._validate_chart(
        {
            "type": "pie",
            "x": "category",
            "y": ["share"],
            "title": "Mix",
            "modifiers": {"colorScale": "sequential", "variant": "donut"},
        },
        ["category", "share"],
    )
    assert pie_result is not None
    assert pie_result["modifiers"] == {
        "colorScale": "sequential",
        "variant": "donut",
    }

    stripped = svc._validate_chart(
        {
            "type": "bar",
            "x": "state",
            "y": ["total"],
            "title": "By State",
            "modifiers": {"colorScale": "invalid"},
        },
        ["state", "total"],
    )
    assert stripped is not None
    assert "colorScale" not in stripped.get("modifiers", {})


def test_validate_chart_none_returns_none(metadata: MetadataService) -> None:
    svc = AIQueryService(MagicMock(), MagicMock(), MagicMock(), metadata)
    assert svc._validate_chart({"type": "none"}, ["a", "b"]) is None
