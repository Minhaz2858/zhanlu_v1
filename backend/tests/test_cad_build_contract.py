from app.models import CadBuildContract
from app.models.agent_conversation import AgentConversation
from app.models.base import TimestampedBase


def test_contract_table_registered():
    # TimestampedBase.metadata knows every imported model — the table must exist.
    assert "cad_build_contracts" in TimestampedBase.metadata.tables


def test_contract_columns():
    t = TimestampedBase.metadata.tables["cad_build_contracts"]
    cols = {c.name: c for c in t.columns}
    for required in ("id", "org_id", "app_id", "conversation_id", "agent_id",
                     "contract_json", "created_date"):
        assert required in cols, f"missing column {required}"
    # conversation cascade: deleting the conversation row removes the contract
    fk = t.columns["conversation_id"].foreign_keys
    assert fk and list(fk)[0].ondelete == "CASCADE"
    # org_id/app_id must inherit the NOT NULL isolation wall from the base —
    # a nullable override would silently disable tenant isolation.
    assert t.columns["org_id"].nullable is False
    assert t.columns["app_id"].nullable is False


def test_conversation_delete_cascades_contract():
    """Deleting a conversation physically removes its CadBuildContract rows.

    Exercises the REAL FK cascade at runtime (not just the metadata ondelete
    string): insert a conversation + a linked contract, delete the
    conversation, commit, and assert the contract row is gone from the DB.
    SQLite only enforces FKs when ``PRAGMA foreign_keys=ON`` — mirror
    production (app.database sets the same pragma on its engine).
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    import app.models  # noqa: F401  registers every mapper on Base.metadata

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
        # Commit parent (conversation) before the child that FKs it —
        # no relationship() is declared, so the UoW can't infer insert order.
        conv = AgentConversation(id="conv-cascade-1")
        db.add(conv)
        db.commit()

        contract = CadBuildContract(
            id="contract-cascade-1",
            conversation_id="conv-cascade-1",
            agent_id="cad-agent",
            contract_json={"mode": "rebuild"},
        )
        db.add(contract)
        db.commit()
        assert db.get(CadBuildContract, "contract-cascade-1") is not None

        db.delete(conv)
        db.commit()

        # Fresh session so the identity map can't mask a still-present row.
        with Session() as check:
            assert check.get(CadBuildContract, "contract-cascade-1") is None
    finally:
        db.close()
        engine.dispose()
