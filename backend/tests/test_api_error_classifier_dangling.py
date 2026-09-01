"""API error classifier regression tests.

2026-08-29 (conv 8e749a1e): a DeepSeek 400 "An assistant message with
'tool_calls' must be followed by tool messages responding to each
'tool_call_id'. (insufficient tool messages following tool_calls message)"
was misclassified as BILLING because the billing markers contained the
generic word "insufficient" — users saw "Billing issue. Check account
quota." while the real problem was a corrupted conversation history.
"""

from app.services.api_error_classifier import (
    classify_api_error,
    FailoverReason,
)

_LIVE_400 = (
    'LLM API error: {"error":{"message":"An assistant message with '
    "'tool_calls' must be followed by tool messages responding to each "
    "'tool_call_id'. (insufficient tool messages following tool_calls "
    'message)","type":"invalid_request_error","param":null,'
    '"code":"invalid_request_error"}} (status 400)'
)


def test_dangling_tool_calls_classified_as_format_error():
    ce = classify_api_error(Exception(_LIVE_400))
    assert ce.reason == FailoverReason.format_error, ce
    assert ce.retryable is False
    assert "start a new chat" in ce.message


def test_dangling_tool_calls_with_status_400():
    ce = classify_api_error(Exception(_LIVE_400), status_code=400)
    assert ce.reason == FailoverReason.format_error, ce


def test_dangling_no_longer_classified_as_billing():
    ce = classify_api_error(Exception(_LIVE_400))
    assert ce.reason != FailoverReason.billing, ce


def test_real_billing_still_classified_as_billing():
    ce = classify_api_error(Exception("402 payment required, insufficient balance"))
    assert ce.reason == FailoverReason.billing, ce


def test_plain_insufficient_no_longer_billing():
    # The generic word "insufficient" alone (e.g. "insufficient tool
    # messages") must NOT route to billing.
    ce = classify_api_error(Exception("insufficient something else"))
    assert ce.reason != FailoverReason.billing, ce
