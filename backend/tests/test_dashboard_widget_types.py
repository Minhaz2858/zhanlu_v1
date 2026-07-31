import pytest
from pydantic import ValidationError

from app.services.tool_handlers.dashboard_tools import (
    WidgetSpec, CREATE_DASHBOARD_SCHEMA,
)

ALLOWED = ["kpi", "line", "bar", "pie", "table",
           "area", "stacked-bar", "scatter", "gauge", "radar"]


def _base(**over):
    d = dict(id="w1", type="kpi", title="t", sql="SELECT 1 AS v", options={})
    d.update(over)
    return d


@pytest.mark.parametrize("t", ALLOWED)
def test_widget_spec_accepts_all_types(t):
    assert WidgetSpec(**_base(type=t)).type == t


def test_widget_spec_rejects_unknown_type():
    with pytest.raises(ValidationError):
        WidgetSpec(**_base(type="bogus"))


def test_schema_enum_covers_all_types():
    enum = (CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]
            ["widgets"]["items"]["properties"]["type"]["enum"])
    assert set(ALLOWED).issubset(set(enum))


def test_schema_options_documents_new_types():
    desc = (CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]
            ["widgets"]["items"]["properties"]["options"]["description"])
    for kw in ("area", "stacked-bar", "scatter", "gauge", "radar", "motion", "zoom"):
        assert kw in desc
