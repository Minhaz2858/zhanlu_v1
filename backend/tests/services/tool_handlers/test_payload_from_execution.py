from app.services.tool_handlers._payload_from_execution import _payload_from_execution


class FakeExecution:
    def __init__(self, tool_name, result, summary_text=""):
        self.tool_name = tool_name
        self.result = result
        self.summary_text = summary_text
        from datetime import datetime, timezone
        self.created_at = datetime.now(timezone.utc)


def test_payload_from_empty_result():
    payload = _payload_from_execution(FakeExecution("ask_data_agent", {}))
    assert payload["title"] == "Report from ask_data_agent"
    assert "0 rows" in payload["methodology"]


def test_payload_from_single_row_aggregate():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent",
        {"rows": [[100, 200, 300]], "columns": ["a", "b", "c"], "summary": "Q3"},
    ))
    assert len(payload["kpis"]) == 3
    assert payload["kpis"][0]["value"] == 100
    assert payload["summary"] == "Q3"


def test_payload_from_multi_row_creates_chart():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent",
        {"rows": [["A", 10], ["B", 20], ["C", 30]], "columns": ["x", "y"]},
    ))
    assert payload["chart"] is not None
    assert payload["chart"]["data"]["x"] == ["A", "B", "C"]


def test_payload_uses_result_title_when_present():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent", {"rows": [], "title": "Custom Title"},
    ))
    assert payload["title"] == "Custom Title"
