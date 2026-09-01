from app.services.automation_executor import classify_run_failure_reason


def test_classify_run_failure_reason_quota_for_429_and_402_messages():
    assert classify_run_failure_reason("Provider 429: quota exceeded") == "quota"
    assert classify_run_failure_reason("Provider 402 Payment Required") == "quota"
    assert classify_run_failure_reason("OpenAI rate limit reached") == "quota"


def test_classify_run_failure_reason_approval_for_paused_messages():
    assert classify_run_failure_reason("Agent paused for user confirmation") == "approval"
    assert (
        classify_run_failure_reason("Agent paused for a decision summary (create_agent)")
        == "approval"
    )
    assert (
        classify_run_failure_reason("Run hit the auto-approval cap (3) — too many consecutive pauses")
        == "approval"
    )


def test_classify_run_failure_reason_network_for_timeout_messages():
    assert classify_run_failure_reason("NetworkError: connection reset") == "network"
    assert classify_run_failure_reason("Timed out reading upstream response") == "network"


def test_classify_run_failure_reason_falls_back_to_unknown():
    assert classify_run_failure_reason("") == "unknown"
    assert classify_run_failure_reason("Internal assertion failed") == "unknown"
