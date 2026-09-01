from app.services.tool_handlers.artifact_tool import CREATE_ARTIFACT_SCHEMA


def test_schema_includes_source_execution_id():
    params = CREATE_ARTIFACT_SCHEMA["function"]["parameters"]
    assert "source_execution_id" in params["properties"]


def test_schema_title_is_required():
    params = CREATE_ARTIFACT_SCHEMA["function"]["parameters"]
    assert "title" in params["required"]
