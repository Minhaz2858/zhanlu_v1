"""Connector-neutral ERP write-back contract (EBOM Workstream C).

Lifecycle: proposed → awaiting_approval → approved → executing →
succeeded | failed. The approval stages are the EXISTING governance flow
(ApprovalRequest + execute_approved_action) — nothing here bypasses them.

SANDBOX HONESTY: the built-in adapter is a deterministic in-process ledger,
NOT a production ERP integration. Connecting a real ERP additionally
requires: connector credentials in a vault, network error taxonomy with
retry/backoff policy, vendor API mapping, and reconciliation — none of
which are claimed here.
"""

from __future__ import annotations

from typing import Protocol

# The sandbox adapter's in-memory proof-of-write: external_ref -> payload.
_LEDGER: dict[str, dict] = {}

_COUNTER = {"n": 0}


class ERPAdapter(Protocol):
    """Any vendor adapter implements exactly this and registers itself."""

    def submit_purchase_order(self, payload: dict) -> dict: ...


class SandboxErpAdapter:
    """Deterministic sandbox: validates the payload and records the order."""

    def submit_purchase_order(self, payload: dict) -> dict:
        if not payload.get("supplier_name"):
            raise ValueError("ERP requires supplier_name")
        amount = payload.get("est_amount")
        items = payload.get("items")
        if amount is None and not items:
            raise ValueError("ERP requires est_amount or line items")
        _COUNTER["n"] += 1
        external_ref = f"SANDBOX-PO-{_COUNTER['n']:06d}"
        _LEDGER[external_ref] = dict(payload)
        return {"external_ref": external_ref, "status": "accepted",
                "adapter": "sandbox"}


ADAPTERS: dict[str, ERPAdapter] = {"sandbox": SandboxErpAdapter()}


def adapter_ledger() -> dict:
    """External-system view for tests/demos — what the 'ERP' received."""
    return dict(_LEDGER)


def reset_adapter_ledger() -> None:
    """Test isolation only — clears the sandbox 'external system'."""
    _LEDGER.clear()
    _COUNTER["n"] = 0


def get_adapter(connector_id: str) -> ERPAdapter:
    try:
        return ADAPTERS[connector_id]
    except KeyError:
        raise ValueError(f"unknown ERP connector: {connector_id!r}") from None
