"""Tests for the structured API error classifier."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.api_error_classifier import (
    FailoverReason,
    ClassifiedError,
    classify_api_error,
)


def test_rate_limit_429():
    """HTTP 429 classifies as rate_limit, retryable."""
    ce = classify_api_error(Exception("rate limit"), status_code=429)
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable is True
    assert ce.max_retries == 3


def test_rate_limit_string_match():
    """String 'too many requests' classifies as rate_limit."""
    ce = classify_api_error("too many requests")
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable is True


def test_server_error_500():
    """HTTP 500 classifies as server_error, retryable."""
    ce = classify_api_error(Exception("internal error"), status_code=500)
    assert ce.reason == FailoverReason.server_error
    assert ce.retryable is True


def test_overloaded_503():
    """HTTP 503 classifies as overloaded, retryable."""
    ce = classify_api_error(Exception("overloaded"), status_code=503)
    assert ce.reason == FailoverReason.overloaded
    assert ce.retryable is True


def test_auth_401():
    """HTTP 401 classifies as auth, not retryable."""
    ce = classify_api_error(Exception("unauthorized"), status_code=401)
    assert ce.reason == FailoverReason.auth
    assert ce.retryable is False


def test_billing_402():
    """HTTP 402 classifies as billing, not retryable."""
    ce = classify_api_error(Exception("payment required"), status_code=402)
    assert ce.reason == FailoverReason.billing
    assert ce.retryable is False


def test_model_not_found_404():
    """HTTP 404 classifies as model_not_found with should_fallback."""
    ce = classify_api_error(Exception("model not found"), status_code=404)
    assert ce.reason == FailoverReason.model_not_found
    assert ce.retryable is False
    assert ce.should_fallback is True


def test_context_overflow_400():
    """HTTP 400 with context overflow markers classifies as context_overflow with should_compress."""
    ce = classify_api_error(
        {"error": "context_length_exceeded"}, status_code=400
    )
    assert ce.reason == FailoverReason.context_overflow
    assert ce.retryable is True
    assert ce.should_compress is True


def test_context_overflow_string_match():
    """String 'prompt too long' classifies as context_overflow."""
    ce = classify_api_error("This model's maximum context length is 8192 tokens")
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress is True


def test_timeout_string_match():
    """String 'timed out' classifies as timeout."""
    ce = classify_api_error(Exception("request timed out"))
    assert ce.reason == FailoverReason.timeout
    assert ce.retryable is True


def test_connection_error_string_match():
    """String 'connection reset' classifies as connection_error."""
    ce = classify_api_error(Exception("connection reset by peer"))
    assert ce.reason == FailoverReason.connection_error
    assert ce.retryable is True


def test_content_policy_400():
    """HTTP 400 with content policy markers classifies as content_policy_blocked."""
    ce = classify_api_error(
        {"error": "content policy violation"}, status_code=400
    )
    assert ce.reason == FailoverReason.content_policy_blocked
    assert ce.retryable is False


def test_bad_request_400_no_markers():
    """HTTP 400 without special markers classifies as bad_request."""
    ce = classify_api_error({"error": "invalid field"}, status_code=400)
    assert ce.reason == FailoverReason.bad_request
    assert ce.retryable is False


def test_unknown_error_is_conservatively_retryable():
    """Unknown errors are retryable once (conservative)."""
    ce = classify_api_error(Exception("some weird error"))
    assert ce.reason == FailoverReason.unknown
    assert ce.retryable is True
    assert ce.max_retries == 1


def test_dict_error_extraction():
    """Error dicts with 'message' key are extracted properly."""
    ce = classify_api_error({"message": "rate limit exceeded", "code": 429})
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable is True


def test_none_error_is_unknown():
    """None error classifies as unknown."""
    ce = classify_api_error(None)
    assert ce.reason == FailoverReason.unknown


def test_original_preserved():
    """The original error is preserved for debugging."""
    exc = ValueError("test error")
    ce = classify_api_error(exc)
    assert ce.original is exc
