#!/usr/bin/env python3
"""Mutation-proof the retrieval / selection fixes.

For each mutation: copy the source aside, apply a STRING-REPLACE that reverts a
fix, run the test that is supposed to catch it, restore the file, and compare
sha256 to prove the restore was exact. A test that passes both before and after
a fix is decoration.

Run:  /opt/anaconda3/bin/python3.13 scripts/mutate_retrieval_tests.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
TR = BACKEND / "app/services/db/table_retrieval.py"
NLS = BACKEND / "app/services/db/nl_answer_service.py"
SL = BACKEND / "app/services/knowledge_graph/schema_linker.py"
PY = sys.executable or "/opt/anaconda3/bin/python3.13"

MUTATIONS = [
    (
        "M1 render the catalog tail first (the alphabetical-starvation shape)",
        SL,
        '        out.append("-- Ranked candidates:")\n        out.extend(detail)',
        '        out.append("-- Rest of the catalog:")\n        out.extend(rest)\n        out.append("-- Ranked candidates:")\n        out.extend(detail)',
        "tests/test_table_retrieval.py::test_the_selection_surface_never_hides_a_candidate",
    ),
    (
        "M2 restore the hallucinated-name passthrough",
        NLS,
        """                    if not validated:
                        logger.warning(
                            "NLAnswer: table selection named no existing table "
                            "(%s) — treating as empty selection",
                            selected[:4],
                        )
                    return validated[:4]""",
        """                    return validated[:4] if validated else selected[:4]""",
        "tests/test_table_retrieval.py::test_selection_of_only_invented_names_is_empty",
    ),
    (
        "M3 require BOTH coverage dates before convicting a stale table",
        TR,
        "    if window is None or end is None:\n        return None\n    q_start, q_end = window\n    if end < q_start:\n        return False\n    if start is None:\n        return None",
        "    if window is None or start is None or end is None:\n        return None\n    q_start, q_end = window\n    if end < q_start:\n        return False",
        "tests/test_table_retrieval.py::test_a_table_without_data_in_the_window_is_labelled_not_dropped",
    ),
    (
        "M4 hardcode MySQL backticks again in the coverage probe",
        NLS,
        '        stmt = f"SELECT 1 FROM {quoted}{where} LIMIT 1"',
        '        stmt = f"SELECT 1 FROM `{name}`{where} LIMIT 1"',
        'tests/test_table_retrieval.py::test_coverage_probe_quotes_for_every_dialect',
    ),
    (
        "M5 drop the coverage annotation (tables look fresh by default)",
        TR,
        "        covers = _covers(window, start, end)",
        "        covers = True if window else None",
        "tests/test_table_retrieval.py::test_all_candidates_stale_discloses_instead_of_reporting_zero",
    ),
    (
        "M6 let a table outside the catalog be offered as a candidate",
        TR,
        "        if name not in by_name:\n            return False  # never offer a table outside the catalog the user may read",
        "        if name not in by_name:\n            by_name[name] = {'table_name': name}\n            scores[name] = 0.0",
        "tests/test_table_retrieval.py::test_retrieval_never_offers_a_table_outside_the_catalog",
    ),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    failures = []
    for label, path, old, new, test in MUTATIONS:
        before = sha(path)
        src = path.read_text()
        if old not in src:
            print(f"!! {label}: anchor not found in {path.name} — mutation NOT applied")
            failures.append(label)
            continue
        backup = Path("/tmp") / f"{path.name}.mutbak"
        shutil.copy2(path, backup)
        path.write_text(src.replace(old, new, 1))
        try:
            r = subprocess.run(
                [PY, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=BACKEND, capture_output=True, text=True, timeout=600,
            )
            caught = r.returncode != 0
            tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
            print(f"{'CAUGHT ' if caught else 'MISSED '} {label}")
            print(f"          {tail[-1] if tail else r.stdout[-160:]}")
        finally:
            shutil.copy2(backup, path)
        after = sha(path)
        if after != before:
            print(f"!! {label}: restore was not byte-exact ({before[:12]} -> {after[:12]})")
            failures.append(label)
        if not caught:
            failures.append(label)

    print()
    print(f"{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} mutations caught")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
