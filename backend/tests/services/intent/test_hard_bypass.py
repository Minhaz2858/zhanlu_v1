from app.services.intent.export_intent import detect_export_intent


def test_hard_bypass_strong_match_detected():
    intent = detect_export_intent("give me in docx")
    assert intent is not None
    assert intent.confidence == "strong"
    assert intent.format == "docx"
