from app.models import CadBuildContract
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
