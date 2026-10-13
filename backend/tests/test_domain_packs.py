"""Domain pack tests (IBM Gap 5, 2026-10-13).

Packaged vertical value: industry vocabulary + starter questions,
installed idempotently per org, glossary merged into terminologies.
"""
import pytest
from fastapi import HTTPException


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/packs_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_list_packs_returns_available(tmp_path):
    from app.services.domain_packs import list_packs

    db = _fresh_db(tmp_path)
    res = list_packs(db, org_id="org-a")
    keys = {p["pack_key"] for p in res["packs"]}
    assert "insurance_claims" in keys
    assert "manufacturing_c5" in keys
    assert all(p["installed"] is False for p in res["packs"])


def test_install_merges_glossary_and_is_idempotent(tmp_path):
    from app.models.terminology import Terminology
    from app.services.domain_packs import install_pack

    db = _fresh_db(tmp_path)
    first = install_pack(db, "org-a", "app-1", "insurance_claims")
    assert first["glossary_terms_added"] > 0

    count1 = db.query(Terminology).filter(Terminology.org_id == "org-a").count()
    # Re-install: no duplicate glossary rows, version snapshot updated.
    second = install_pack(db, "org-a", "app-1", "insurance_claims")
    count2 = db.query(Terminology).filter(Terminology.org_id == "org-a").count()
    assert second["glossary_terms_added"] == 0
    assert count1 == count2

    from app.models.forecasting import DomainPackInstall
    rows = db.query(DomainPackInstall).filter(DomainPackInstall.org_id == "org-a").all()
    assert len(rows) == 1  # idempotent install record
    assert rows[0].config["pack_key"] == "insurance_claims"


def test_install_scopes_to_org(tmp_path):
    from app.models.terminology import Terminology
    from app.services.domain_packs import install_pack

    db = _fresh_db(tmp_path)
    install_pack(db, "org-a", "app-1", "insurance_claims")
    install_pack(db, "org-b", "app-1", "insurance_claims")

    a = db.query(Terminology).filter(Terminology.org_id == "org-a").count()
    b = db.query(Terminology).filter(Terminology.org_id == "org-b").count()
    assert a > 0 and b == a  # both tenants get the pack, isolated


def test_unknown_pack_404(tmp_path):
    from app.services.domain_packs import install_pack

    db = _fresh_db(tmp_path)
    with pytest.raises(HTTPException) as e:
        install_pack(db, "org-a", "app-1", "no-such-pack")
    assert e.value.status_code == 404


def test_customer_glossary_edits_win(tmp_path):
    """Re-install never overwrites an existing word's description."""
    from app.models.terminology import Terminology
    from app.services.domain_packs import install_pack

    db = _fresh_db(tmp_path)
    install_pack(db, "org-a", "app-1", "insurance_claims")
    # Customer edits a term
    term = db.query(Terminology).filter(
        Terminology.org_id == "org-a", Terminology.word == "claim"
    ).first()
    term.description = "OUR edited definition"
    db.commit()

    install_pack(db, "org-a", "app-1", "insurance_claims")
    db.expire_all()
    term = db.query(Terminology).filter(
        Terminology.org_id == "org-a", Terminology.word == "claim"
    ).first()
    assert term.description == "OUR edited definition"
