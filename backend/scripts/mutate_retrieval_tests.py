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
LEX = BACKEND / "app/services/db/lexical.py"
EV = BACKEND / "app/services/db/retrieval_evidence.py"
PY = sys.executable or "/opt/anaconda3/bin/python3.13"
RANK = "tests/test_retrieval_ranking.py"

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
    (
        "M7 stop cutting Chinese into n-grams (the one-giant-token defect)",
        LEX,
        "            if _CJK_CHAR_RE.match(chunk):\n                out.extend(_cjk_terms(chunk))",
        "            if _CJK_CHAR_RE.match(chunk):\n                out.append(chunk)",
        f"{RANK}::test_a_chinese_question_is_no_longer_one_giant_token",
    ),
    (
        "M8 read only the English description (the bilingual catalog defect)",
        EV,
        "    bag.extend(token_list(desc_zh))",
        "    bag.extend(token_list(''))",
        f"{RANK}::test_a_chinese_contract_question_ranks_contract_tables_first",
    ),
    (
        "M9 give every matched domain a boost again (the auth_* top-8)",
        TR,
        "            terms = domain_support(question, d)\n            if not terms:",
        "            terms = domain_support(question, d) or ['domain']\n            if not terms:",
        f"{RANK}::test_a_misscoped_domain_does_not_boost_auth_tables",
    ),
    (
        "M10 drop the embedding signal gate (use a flat distribution anyway)",
        EV,
        "    if spread < _EMB_MIN_SPREAD:",
        "    if False:",
        f"{RANK}::test_a_flat_similarity_distribution_is_declared_not_used",
    ),
    (
        "M11 make BM25 idf uniform (ignore document frequency)",
        LEX,
        "        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))",
        "        return 1.0",
        f"{RANK}::test_a_term_in_every_document_carries_no_discrimination",
    ),
    (
        "M12 disable the verified-metric component",
        EV,
        '            for label in labels[:_METRIC_CAP]:\n                hit.add("metric", _W_METRIC, f"metric:{label}")',
        '            for label in labels[:_METRIC_CAP]:\n                hit.add("metric", 0.0, f"metric:{label}")',
        f"{RANK}::test_a_verified_metric_definition_scores_the_table_it_names",
    ),
    (
        "M13 let embedding similarity create a candidate on its own",
        EV,
        "        hit = out.hits.get(name)\n        if hit is None:\n            continue\n        hit.add(\"embedding\", _W_EMBED * value, f\"similarity {raw_sims.get(name, 0.0):.2f}\")",
        "        hit = out.hits.get(name) or out.hits.setdefault(name, EvidenceHit(table_name=name))\n        hit.add(\"embedding\", _W_EMBED * value, f\"similarity {raw_sims.get(name, 0.0):.2f}\")",
        f"{RANK}::test_similarity_alone_never_creates_a_candidate",
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
