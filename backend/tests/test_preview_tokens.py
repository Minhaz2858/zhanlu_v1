"""Signed preview tokens — mint/verify round-trip and rejection paths."""
from app.services.preview_tokens import mint_preview_token, verify_preview_token


def test_round_trip_returns_user_id():
    token = mint_preview_token(file_id="f1", user_id="u1")
    assert verify_preview_token(token, "f1") == "u1"


def test_rejects_tampered_signature():
    token = mint_preview_token(file_id="f1", user_id="u1")
    bad = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert verify_preview_token(bad, "f1") is None


def test_rejects_token_minted_for_a_different_file():
    token = mint_preview_token(file_id="f1", user_id="u1")
    assert verify_preview_token(token, "f2") is None


def test_rejects_expired_token():
    token = mint_preview_token(file_id="f1", user_id="u1", ttl_seconds=-1)
    assert verify_preview_token(token, "f1") is None


def test_rejects_garbage():
    assert verify_preview_token("not-a-token", "f1") is None
    assert verify_preview_token("", "f1") is None
    assert verify_preview_token("a.b.c", "f1") is None
