"""Phase 10 / W2 — marketplace "my-skills" ownership boundary.

Audit correction (S2.2): the original audit flagged an IDOR on
DELETE /marketplace/my-skills/{id}; re-verification shows an owner check
(403 "Not your skill") DOES exist. This test pins it so it cannot regress,
while the shared-directory name-collision residual remains part of the
blocked S2.1 tenancy spec (per-org skill dirs).
"""
import pytest
from fastapi import HTTPException


def test_cannot_delete_another_users_skill():
    from app.routers.marketplace import remove_my_skill

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return SimpleNamespace(
                id="t1", created_by_id="owner-1", name="shared-skill",
                is_deleted=False)

    from types import SimpleNamespace
    db = SimpleNamespace(query=lambda *a: _Q())
    other = SimpleNamespace(id="attacker", org_id="org-B")

    with pytest.raises(HTTPException) as exc:
        remove_my_skill("t1", db=db, user=other)
    assert exc.value.status_code == 403
