"""Regression tests for model_router.py (Part 2 — Phase 1 model layer)."""

from unittest.mock import patch, MagicMock

from app.services import model_router as mr


class TestClassifyTask:
    """Tests for classify_task."""

    def test_tools_specified_trumps_all(self):
        """When tools_specified=True, always return tool_use."""
        result = mr.classify_task(
            [{"role": "user", "content": "write code for me"}],
            tools_specified=True,
        )
        assert result == "tool_use"

    def test_code_gen_keywords(self):
        messages = [{"role": "user", "content": "write code a python script"}]
        assert mr.classify_task(messages) == "code_gen"

    def test_code_gen_chinese_keywords(self):
        messages = [{"role": "user", "content": "请修复代码"}]
        assert mr.classify_task(messages) == "code_gen"

    def test_reasoning_keywords(self):
        messages = [{"role": "user", "content": "think step by step about this problem"}]
        assert mr.classify_task(messages) == "reasoning"

    def test_reasoning_keyword_analyze(self):
        messages = [{"role": "user", "content": "analyze this data"}]
        assert mr.classify_task(messages) == "reasoning"

    def test_document_gen_keywords(self):
        messages = [{"role": "user", "content": "write a report about the market"}]
        assert mr.classify_task(messages) == "document_gen"

    def test_document_gen_chinese(self):
        messages = [{"role": "user", "content": "撰写报告"}]
        assert mr.classify_task(messages) == "document_gen"

    def test_defaults_to_simple_chat(self):
        messages = [{"role": "user", "content": "hello how are you"}]
        assert mr.classify_task(messages) == "simple_chat"

    def test_no_user_messages_defaults_to_simple_chat(self):
        messages = [{"role": "system", "content": "you are helpful"}]
        assert mr.classify_task(messages) == "simple_chat"

    def test_empty_messages(self):
        assert mr.classify_task([]) == "simple_chat"

    def test_multiple_user_messages_concatenated(self):
        """All user messages should be joined for keyword search."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "debug this please"},
        ]
        assert mr.classify_task(messages) == "code_gen"


class TestRouteModel:
    """Tests for route_model — uses _load_routing_table which reads app.config."""

    def test_known_task_routes_to_configured_model(self):
        mock_settings = type("S", (), {"LLM_MODEL": "default-model",
                                       "MODEL_TASK_ROUTING": '{"code_gen": "code-model"}'})()
        with patch("app.config.settings", mock_settings):
            # _load_routing_table() calls getattr(settings, "MODEL_TASK_ROUTING", ...)
            result = mr.route_model("code_gen")
            assert result == "code-model"

    def test_unknown_task_falls_back_to_llm_model(self):
        mock_settings = type("S", (), {"LLM_MODEL": "default-model",
                                       "MODEL_TASK_ROUTING": "{}"})()
        with patch("app.config.settings", mock_settings):
            result = mr.route_model("unknown_task")
            assert result == "default-model"

    def test_empty_routing_falls_back(self):
        mock_settings = type("S", (), {"LLM_MODEL": "fallback-model",
                                       "MODEL_TASK_ROUTING": ""})()
        with patch("app.config.settings", mock_settings):
            result = mr.route_model("code_gen")
            assert result == "fallback-model"

    def test_invalid_json_routing_falls_back(self):
        mock_settings = type("S", (), {"LLM_MODEL": "safe-model",
                                       "MODEL_TASK_ROUTING": "not json"})()
        with patch("app.config.settings", mock_settings):
            result = mr.route_model("code_gen")
            assert result == "safe-model"

    def test_non_dict_json_routing_falls_back(self):
        mock_settings = type("S", (), {"LLM_MODEL": "safe-model",
                                       "MODEL_TASK_ROUTING": '["array"]'})()
        with patch("app.config.settings", mock_settings):
            result = mr.route_model("code_gen")
            assert result == "safe-model"


class TestGetModelForRequest:
    """Tests for get_model_for_request."""

    def test_explicit_task_overrides_heuristic(self):
        mock_settings = type("S", (), {"LLM_MODEL": "default",
                                       "MODEL_TASK_ROUTING": '{"code_gen": "code-model"}'})()
        with patch("app.config.settings", mock_settings):
            messages = [{"role": "user", "content": "hello"}]
            result = mr.get_model_for_request(messages, explicit_task="code_gen")
            assert result == "code-model"

    def test_no_explicit_task_uses_heuristic(self):
        mock_settings = type("S", (), {"LLM_MODEL": "default",
                                       "MODEL_TASK_ROUTING": '{"reasoning": "reason-model"}'})()
        with patch("app.config.settings", mock_settings):
            messages = [{"role": "user", "content": "analyze this data"}]
            result = mr.get_model_for_request(messages)
            assert result == "reason-model"

    def test_tools_specified_routes_tool_use(self):
        mock_settings = type("S", (), {"LLM_MODEL": "default",
                                       "MODEL_TASK_ROUTING": '{"tool_use": "tool-model"}'})()
        with patch("app.config.settings", mock_settings):
            result = mr.get_model_for_request([], tools_specified=True)
            assert result == "tool-model"
