from app.services.tool_handlers.fusion360_granular import (
    _load_contract, _contract_issues, _feature_matches_body,
)

CONTRACT = {"part": "hex_bolt", "features": [
    {"kind": "hex", "across_flats": 8, "height": 14},
    {"kind": "cylinder", "diameter": 5, "height": 20},
]}
BODIES = [
    {"index": 0, "min": [-0.462, -0.400, 0.0], "max": [0.462, 0.400, 1.4], "faces": 14},
    {"index": 1, "min": [-0.250, -0.250, 1.4], "max": [0.250, 0.250, 3.4], "faces": 3},
]


def test_feature_matches_hex():
    b = BODIES[0]
    assert _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 14}, b) == []


def test_feature_mismatch_hex_height():
    b = BODIES[0]
    issues = _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 12}, b)
    assert any("height" in i for i in issues)


def test_feature_matches_box():
    # body0 bbox: w = 9.24mm, d = 8.0mm, h = 14mm
    b = BODIES[0]
    assert _feature_matches_body({"kind": "box", "width": 9.24, "depth": 8, "height": 14}, b) == []
    # 'length' alias for width
    assert _feature_matches_body({"kind": "box", "length": 9.24, "depth": 8, "height": 14}, b) == []


def test_feature_mismatch_box_width():
    b = BODIES[0]
    issues = _feature_matches_body({"kind": "box", "width": 10, "depth": 8, "height": 14}, b)
    assert issues and any("width" in i for i in issues)


def test_feature_non_numeric_dim_reports_issue():
    # "8mm" must NOT raise — it becomes an explicit issue instead
    b = BODIES[0]
    issues = _feature_matches_body({"kind": "hex", "across_flats": "8mm", "height": 14}, b)
    assert issues and any("not numeric" in i for i in issues)


def test_cylinder_non_z_axis_uses_near_equal_pair():
    # Cylinder lying along X: bbox 25 x 5 x 5 mm → the near-equal pair is
    # (d,h) → diameter 5, and height = z span = 5.
    body = {"index": 0, "min": [-0.25, -0.25, -0.25], "max": [2.25, 0.25, 0.25], "faces": 3}
    assert _feature_matches_body({"kind": "cylinder", "diameter": 5, "height": 5}, body) == []
    # wrong diameter must fail
    issues = _feature_matches_body({"kind": "cylinder", "diameter": 6, "height": 5}, body)
    assert any("diameter" in i for i in issues)


def test_contract_issues_ok():
    assert _contract_issues(CONTRACT, BODIES) == []


def test_contract_issues_missing_feature():
    # only one body declared kind matches: hex matches body0; cylinder needs a
    # 5mm-dia 20mm-tall body — none, so a per-feature failure must be reported
    issues = _contract_issues(CONTRACT, [BODIES[0]])
    assert issues and any("cylinder" in i for i in issues)


def test_contract_empty_features_reports_issue():
    issues = _contract_issues({"part": "x", "features": []}, BODIES)
    assert issues and any("no features" in i for i in issues)


def test_load_contract_use_last_and_id():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import CadBuildContract
    from app.models.agent_conversation import AgentConversation
    import app.models  # noqa: F401 — registers every mapper on Base.metadata

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):  # noqa: D401
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        # Commit the parent conversation before children that FK it
        conv = AgentConversation(id="conv-load-contract")
        db.add(conv)
        db.commit()

        now = datetime.now(timezone.utc)
        older = CadBuildContract(
            id="contract-old", conversation_id="conv-load-contract",
            agent_id="cad-agent", contract_json={"part": "old", "features": []},
            created_date=now - timedelta(minutes=5),
        )
        newer = CadBuildContract(
            id="contract-new", conversation_id="conv-load-contract",
            agent_id="cad-agent", contract_json={"part": "new", "features": []},
            created_date=now,
        )
        db.add_all([older, newer])
        db.commit()

        # use_last returns the newest row for the conversation
        latest = _load_contract(db, "use_last", "conv-load-contract")
        assert latest is not None and latest["part"] == "new"
        # returns a COPY, never the live ORM-held dict
        assert latest is not newer.contract_json
        # explicit id lookup
        assert _load_contract(db, "contract-old", None)["part"] == "old"
        # unknown id → None
        assert _load_contract(db, "no-such-id", None) is None
        # use_last without / without-matching conversation → None
        assert _load_contract(db, "use_last", None) is None
        assert _load_contract(db, "use_last", "no-such-conv") is None
    finally:
        db.close()
        engine.dispose()
