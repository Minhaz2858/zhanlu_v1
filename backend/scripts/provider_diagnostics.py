#!/usr/bin/env python
"""Provider configuration diagnostics (Phase 3.5 P5) — NO paid requests.

Reports ONLY booleans, model names, categories and consistency findings.
Never prints, logs or exposes API-key values. Performs ZERO network calls.

Usage:
    PYTHONPATH=. python scripts/provider_diagnostics.py [--json]

Exit codes: 0 = consistent · 1 = findings (config inconsistencies) ·
2 = not configured.

A minimal connectivity check is deliberately NOT part of this command —
run scripts/probe_provider.py once, only after valid credentials are
confirmed, to record a fresh error category.
"""
import argparse
import json
import sys

from app.config import settings
from app.services.provider_diagnostics import provider_diagnostics


def _load_llm_rows():
    """Best-effort llm_models registry read (never crashes diagnostics)."""
    try:
        from app.database import SessionLocal
        from app.models.llm_model import LlmModel

        db = SessionLocal()
        try:
            return db.query(LlmModel).all()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] llm_models registry unavailable: {type(exc).__name__}")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    rows = _load_llm_rows()
    report = provider_diagnostics(settings, rows)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=== Zhanlu Provider Diagnostics (offline — no network calls) ===")
        print(f"Provider configured : {'yes' if report['provider_configured'] else 'NO'}")
        print(f"Credential present  : {'yes' if report['credential_present'] else 'NO'}")
        print(f"Primary model       : {report['primary_model'] or '(none)'}")
        print(f"Model enabled       : {_yn(report['model_enabled'])}")
        print(f"Provider selected   : {'yes' if report['provider_selected'] else 'NO'}")
        print(f"Fallback configured : {'yes' if report['fallback_configured'] else 'no'}")
        print(f"Active registry     : {', '.join(report['registered_active_models']) or '(none)'}")
        print(f"Last error category : {report['last_error_category'] or '(none recorded)'}")
        print("Configuration consistency:")
        if report["consistency_findings"]:
            for f in report["consistency_findings"]:
                print(f"  - {f}")
        else:
            print("  - consistent")

    if not report["provider_configured"]:
        return 2
    return 0 if report["consistent"] else 1


def _yn(v):
    return {True: "yes", False: "NO", None: "unknown"}.get(v, "unknown")


if __name__ == "__main__":
    sys.exit(main())
