"""Regression tests for audit E1 — KnowledgeBase password at-rest encryption.

The `password` attribute transparently encrypts on write (fail-closed: a
crypto failure raises instead of storing plaintext) and decrypts on read, and
`to_dict()` must never serialize the credential column.
"""
from app.models.knowledge_base import KnowledgeBase


def test_kb_password_encrypts_on_set():
    kb = KnowledgeBase(name="t", host="h", port=3306)
    kb.password = "supersecret"
    # Stored column is encrypted, not plaintext.
    assert kb._password != "supersecret"
    assert kb._password.startswith("enc:v1:")
    # Read property returns the original plaintext for in-memory use.
    assert kb.password == "supersecret"


def test_kb_password_to_dict_strips_credential():
    kb = KnowledgeBase(name="t", host="h", port=3306)
    kb.password = "supersecret"
    d = kb.to_dict()
    assert "password" not in d
    assert "_password" not in d


def test_kb_legacy_plaintext_password_decrypts():
    kb = KnowledgeBase(name="t", host="h", port=3306)
    # Simulate a legacy row whose stored column was never encrypted.
    kb._password = "plainlegacy"
    assert kb.password == "plainlegacy"


def test_kb_none_password_round_trips():
    kb = KnowledgeBase(name="t", host="h", port=3306)
    kb.password = None
    assert kb.password is None
    assert kb._password is None
