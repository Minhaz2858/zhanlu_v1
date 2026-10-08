# Enterprise Neutralization Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Remove every trace of the Ecisco/EDIA enterprise and its petrochemical (C5/C9) vertical from the platform's own source, keeping the forecasting engine and dashboard framework as domain-agnostic capabilities, and enforce it with an automated no-token gate.

**Architecture:** Gate-first. Build the denylist + checker as the first commit so the guard exists before the surgery; then delete enterprise artifacts, then genericize the surviving code driven by the checker's output, then scrub DB/memory/skills, and finally verify on the finished tree.

**Tech Stack:** Python 3 (FastAPI/SQLAlchemy backend, alembic), ripgrep, pytest, React/Vite frontend, PostgreSQL (docker `zhanlu-copy-postgres` :5401), Make.

**Spec:** `docs/superpowers/specs/2026-10-08-enterprise-neutralization-design.md` (commit `44fec46dd`)

**Base commit:** `2dbc5a6a0`

---

## Ground Rules

- **One commit per task.** Report the listed items, then stop.
- **Never** `git add -A` / `git commit -a`. The tree carries 9 pre-existing unrelated
  dirty files (synexia fsm/plan_dag, chroma sqlite, 3 tests, 2 untracked dirs) that must
  never enter a commit from this plan. Stage explicit paths only.
- **Never touch** `NL2SQL/`, `n8n-master/`, `deepseek-harness-master/`, `superpowers-main/`,
  `node_modules/`, `backups/`, `data/`, `uploads/`, `agent_qa/`.
- Verification runs on the **final** tree — never cite an earlier green run.
- Delete, don't tombstone.

## How to run things in this repo (verified 2026-10-08)

There is **no** `backend/pyproject.toml` / `uv.lock` — backend has `requirements.txt` and
runs **inside Docker**. Do not use `uv run`. Use the container:

```bash
# backend python / pytest / alembic (python3 static checks run on the host)
docker compose run --rm --no-deps --entrypoint "" backend python -m pytest <path> -v
docker compose run --rm --no-deps --entrypoint "" backend alembic heads
docker compose run --rm --no-deps --entrypoint "" backend python -c "import app.main"

# stack + health
docker compose up -d --build --remove-orphans
curl -sf http://localhost:5002/healthz

# E2E smoke
make smoke
```

The token checker is stdlib-only and runs on the **host**: `python3 scripts/check_no_enterprise_tokens.py`.


## Baseline invariants (from spec §4)

Denylist tokens: `ecisco ecisco_bi EDIA edia_mysql c5c9 c5_c9 C5_RESIN raffinate
md_t_lz_price lz_v_ petrochemical aipdp erp_v_sale_orderentry 惠州 伊斯科 异戊二烯 戊烷`
+ 4 slugs: `c5c9-daily-sales c5c9-live-market-reference sales-perf-dashboard-c5c9
sales-performance-c5c9`.

---

## Phase 0 — Baseline

### Task 1: Record the baseline

**Objective:** Capture measurable starting state so the end state is provable.

**Files:** none modified.

**Step 1: Record counts**

Run:
```bash
cd /Users/minhazulislam/Desktop/zhanlu_n8n_main_version
echo "BASE=$(git rev-parse --short HEAD)"
for t in ecisco edia_mysql c5c9 c5_c9 C5_RESIN raffinate md_t_lz_price lz_v_ petrochemical; do
  printf '%s %s\n' "$t" "$(rg -ilw --no-messages "$t" backend frontend/src scripts docs | wc -l | tr -d ' ')"
done
git status --porcelain > /tmp/neutralize-baseline-dirty.txt
```

**Step 2: Confirm the dirty-file baseline**

Run: `cat /tmp/neutralize-baseline-dirty.txt`
Expected: 9 lines (the unrelated in-flight work). Keep this file — Task 27 compares against it.

---

## Phase 1 — The gate (spec S5)

### Task 2: Create the denylist file

**Objective:** One file that defines what "enterprise token" means.

**Files:**
- Create: `scripts/enterprise_token_denylist.txt`

**Step 1: Write the file**

```
# Enterprise-neutralization denylist.
# One token per line. '#' starts a comment. Blank lines ignored.
# Matching rule (implemented by scripts/check_no_enterprise_tokens.py):
#   - token matching [A-Za-z0-9_]+  -> word-boundary, case-insensitive
#   - any other token (dots/dashes/CJK) -> literal, case-sensitive substring
# Company / platform
ecisco
ecisco_bi
EDIA
edia_mysql
aipdp
# Vertical products & tables
c5c9
c5_c9
C5_RESIN
raffinate
md_t_lz_price
lz_v_
petrochemical
erp_v_sale_orderentry
# CJK product/company terms
惠州
伊斯科
异戊二烯
戊烷
# Dashboard slugs (punctuated -> substring match)
c5c9-daily-sales
c5c9-live-market-reference
sales-perf-dashboard-c5c9
sales-performance-c5c9
```

**Step 2: Commit**

```bash
git add scripts/enterprise_token_denylist.txt
git commit -m "chore(neutralize): add enterprise token denylist"
```

### Task 3: Write the checker's failing test

**Objective:** TDD — prove the checker detects tokens before it exists.

**Files:**
- Create: `backend/tests/neutralization/test_check_no_enterprise_tokens.py`

**Step 1: Write failing test**

```python
import importlib.util, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check_no_enterprise_tokens.py"


def _load():
    spec = importlib.util.spec_from_file_location("checker", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_token_matching_rules():
    m = _load()
    assert m.token_matches("ecisco", "ECISCO_BI is here")          # word-boundary, case-insensitive
    assert not m.token_matches("ecisco", "precisco")               # no boundary -> no match
    assert m.token_matches("c5c9-daily-sales", "x c5c9-daily-sales y")  # punctuated -> substring
    assert m.token_matches("惠州", "基地在惠州")                     # CJK -> substring


def test_excluded_trees_are_skipped():
    m = _load()
    for p in ("NL2SQL/DB-GPT-main/a.py", "n8n-master/b.ts", "node_modules/c.js",
              "superpowers-main/d.md", "agent_qa/e.md"):
        assert m.is_excluded(REPO / p), p


def test_binary_files_are_skipped():
    m = _load()
    assert m.is_binary_name(Path("x/font.woff2"))
    assert m.is_binary_name(Path("x/app.min.js"))


def test_checker_exits_nonzero_on_repo_with_hits(tmp_path):
    """End-to-end: a scratch tree with a planted token must fail."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "enterprise_token_denylist.txt").write_text("ecisco\n")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "leak.py").write_text("x = 'ecisco'\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--quiet"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "ecisco" in r.stdout
```

**Step 2: Run it to verify failure**

Run: `docker compose run --rm --no-deps --entrypoint "" backend python -m pytest tests/neutralization/test_check_no_enterprise_tokens.py -v`
Expected: FAIL — `ValueError: FileNotFoundError` / `scripts/check_no_enterprise_tokens.py` missing.

> Fallback if the stack is down: `python3 -m pytest` from `backend/` on the host works too,
> since this test only needs stdlib + subprocess. Prefer the container form for parity.

### Task 4: Implement the checker

**Objective:** Make Task 3 pass.

**Files:**
- Create: `scripts/check_no_enterprise_tokens.py`

**Step 1: Write the implementation**

```python
#!/usr/bin/env python3
"""Fail if any enterprise-specific token appears in the platform's own source.

See docs/superpowers/specs/2026-10-08-enterprise-neutralization-design.md.

Rules:
  * Scans OWN source only; vendored third-party trees and binaries are skipped.
  * ASCII word tokens  -> word-boundary, case-insensitive.
  * Punctuated / CJK   -> literal substring.
  * scripts/enterprise_token_allowlist.txt may justify individual survivors
    as "<path-substring>:<token>  # reason".
Exit 1 on any un-allowlisted hit.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOTS = [
    "backend/app", "backend/tests", "backend/alembic", "backend/skills",
    "frontend/src", "scripts", "docs",
]
ROOT_FILES = ["Makefile", "README.md", "PROJECT.md", "ZHANLU_PROJECT_OVERVIEW.md"]

EXCLUDE_PARTS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".mypy_cache", ".pytest_cache", "NL2SQL", "n8n-master",
    "deepseek-harness-master", "superpowers-main", "backups", "data", "uploads",
    "agent_qa", "outputs",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".dylib",
    ".pyc", ".sqlite3", ".db", ".mp3", ".mp4", ".xlsx", ".pptx", ".docx", ".bin",
}
_WORD = re.compile(r"[A-Za-z0-9_]+$")


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_PARTS for part in path.parts)


def is_binary_name(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXT:
        return True
    return path.name.endswith(".min.js") or path.name.endswith(".min.css")


def load_tokens(denylist: Path) -> list[str]:
    tokens = []
    for line in denylist.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tokens.append(line)
    return tokens


def token_matches(token: str, text: str) -> bool:
    if _WORD.match(token):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                         text, re.IGNORECASE) is not None
    return token in text


def load_allowlist(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and ":" in line:
            p, t = line.rsplit(":", 1)
            out.append((p.strip(), t.strip()))
    return out


def _iter_files(root: Path):
    for rel in ROOTS:
        base = root / rel
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file() and not is_excluded(p.relative_to(root)) and not is_binary_name(p):
                    yield p
    for rel in ROOT_FILES:
        p = root / rel
        if p.is_file():
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    tokens = load_tokens(root / "scripts" / "enterprise_token_denylist.txt")
    allow = load_allowlist(root / "scripts" / "enterprise_token_allowlist.txt")

    hits = []
    for path in sorted(_iter_files(root)):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(path.relative_to(root))
        for i, line in enumerate(text.splitlines(), 1):
            for token in tokens:
                if token_matches(token, line):
                    if any(rp in rel and t == token for rp, t in allow):
                        continue
                    hits.append(f"{rel}:{i}: {token}: {line.strip()[:120]}")

    if hits:
        if not args.quiet:
            print(f"{len(hits)} enterprise token hit(s):\n")
            print("\n".join(hits))
        else:
            for h in hits:
                print(h)
        return 1
    if not args.quiet:
        print(f"OK — no enterprise tokens in own source ({len(tokens)} tokens checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 2: Run the tests to verify pass**

Run: `docker compose run --rm --no-deps --entrypoint "" backend python -m pytest tests/neutralization/test_check_no_enterprise_tokens.py -v`
Expected: PASS — 4 passed

**Step 3: Create an empty allowlist file**

```
# Justified survivors: "<path-substring>:<token>  # reason".
# Target end state: this file is EMPTY.
```

Save as `scripts/enterprise_token_allowlist.txt`.

**Step 4: Commit**

```bash
git add scripts/check_no_enterprise_tokens.py scripts/enterprise_token_allowlist.txt \
        backend/tests/neutralization/test_check_no_enterprise_tokens.py
git commit -m "feat(neutralize): automated enterprise-token checker + tests"
```

### Task 5: Run the gate — expect FAIL

**Objective:** Prove the checker detects the real contamination (not just synthetic).

**Files:** none.

**Step 1: Run**

Run: `python3 scripts/check_no_enterprise_tokens.py --quiet | wc -l`
Expected: **≈ 4,358** line-hits (measured 2026-10-08 on base `2dbc5a6a0` with this exact
denylist; note it counts *lines*, so a line naming both `Ecisco` and `EDIA` counts twice).
Save it:
```bash
python3 scripts/check_no_enterprise_tokens.py --quiet > /tmp/neutralize-hits-before.txt
wc -l < /tmp/neutralize-hits-before.txt
```
This file is the worklist for Phases 2–3.

> Sanity check that the checker is not lying: it must report **0** hits for
> `NL2SQL/`, `n8n-master/`, `node_modules/`, `superpowers-main/` and binaries.
> `grep -cE 'NL2SQL/|n8n-master/|node_modules/|superpowers-main/|\.woff|\.ttf' /tmp/neutralize-hits-before.txt` → `0`.

### Task 6: Wire the gate into the build

**Objective:** Make the check runnable by CI/devs.

**Files:**
- Modify: `Makefile`

**Step 1: Add target**

Add to `Makefile`:
```make
.PHONY: check-tokens
check-tokens: ## Fail if enterprise-specific tokens appear in own source
	python3 scripts/check_no_enterprise_tokens.py
```

**Step 2: Wire into the stack's smoke path**

The Makefile has **no `lint` target** (targets are `up/down/build/logs/reset/db-*/smoke/smoke-ui`).
Add `check-tokens` as a prerequisite of `smoke` so the E2E path fails fast if tokens return:

```make
smoke: check-tokens
	@echo "Running E2E smoke test..."
```

(Add `check-tokens` to the `.PHONY` line as well.)

Run: `make check-tokens` → Expected: exit 1 (still contaminated at this phase).

**Step 3: Commit**

```bash
git add Makefile
git commit -m "chore(neutralize): wire check-tokens into make"
```

---

## Phase 2 — Deletions (spec S1)

### Task 7: Delete the 7 enterprise dashboards

**Objective:** Remove the C5/C9 dashboards from the filesystem.

**Files (delete):**
- `backend/app/dashboards/c5_c9_financial_overview/`
- `backend/app/dashboards/c5c9_daily_sales/`
- `backend/app/dashboards/c5c9_live_market_reference/`
- `backend/app/dashboards/sales_perf_dashboard_c5c9/`
- `backend/app/dashboards/sales_performance_c5c9/`
- `backend/app/dashboards/sales_performance_dashboard_5/`
- `backend/app/dashboards/ceo_demo_001/`

**Step 1: Confirm they are orphaned** (spec §5 note P5)

Run:
```bash
docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -tAc \
  "select count(*) from dashboard_apps where slug ~* 'c5c9|c5_c9|ceo-demo-001';"
```
Expected: `0` (already verified — dashboard dirs are leftovers, not DB-backed).

**Step 2: Hard delete**

```bash
git rm -r --quiet backend/app/dashboards/c5_c9_financial_overview \
  backend/app/dashboards/c5c9_daily_sales \
  backend/app/dashboards/c5c9_live_market_reference \
  backend/app/dashboards/sales_perf_dashboard_c5c9 \
  backend/app/dashboards/sales_performance_c5c9 \
  backend/app/dashboards/sales_performance_dashboard_5 \
  backend/app/dashboards/ceo_demo_001
rm -rf backend/app/dashboards/__pycache__
```

**Step 3: Verify no dangling references** (spec P6)

Run: `rg -n --no-messages 'c5_c9_financial_overview|c5c9_daily_sales|c5c9_live_market_reference|sales_perf_dashboard_c5c9|sales_performance_c5c9|sales_performance_dashboard_5|ceo_demo_001' backend frontend scripts docs`
Expected: only Phase-3 genericization targets (e.g. `routers/agents.py` comment); no import errors.

**Step 4: Commit**

```bash
git commit -m "chore(neutralize): delete C5/C9 enterprise dashboards"
```

### Task 8: Delete the 3 enterprise alembic revisions and repair the chain

**Objective:** Remove `036`/`037`/`042` without breaking `alembic upgrade head`.

**Files:**
- Delete: `backend/alembic/versions/036_rename_ecisco_bi_agent.py`
- Delete: `backend/alembic/versions/037_ensure_12_ecisco_forecast_targets.py`
- Delete: `backend/alembic/versions/042_ecisco_bi_silent_agent.py`
- Modify: `backend/alembic/versions/038_resource_shares_and_type.py` (`down_revision`)
- Modify: `backend/alembic/versions/043_agent_runs.py` (`down_revision`)

**Chain (verified):** `035 → 036 → 037 → 038` and `041_forecast_mlops_hitl → 042 → 043`.

**Step 1: Delete**

```bash
git rm --quiet backend/alembic/versions/036_rename_ecisco_bi_agent.py \
  backend/alembic/versions/037_ensure_12_ecisco_forecast_targets.py \
  backend/alembic/versions/042_ecisco_bi_silent_agent.py
```

**Step 2: Repair `038.down_revision`**

- Modify `backend/alembic/versions/038_resource_shares_and_type.py:25`
- Change: `down_revision = "037"` → `down_revision = "035"`

**Step 3: Repair `043.down_revision`**

- Modify `backend/alembic/versions/043_agent_runs.py:11`
- Change: `down_revision = "042_ecisco_bi_silent_agent"` → `down_revision = "041_forecast_mlops_hitl"`

**Step 4: Verify exactly one head**

Run: `docker compose run --rm --no-deps --entrypoint "" backend alembic heads`
Expected: exactly one head line, and:
```bash
rg -n --no-messages 'down_revision\s*=\s*"(036|037|042_ecisco_bi_silent_agent)"' backend/alembic/versions/
```
Expected: no output.

**Step 5: Commit**

```bash
git add backend/alembic/versions/038_resource_shares_and_type.py \
        backend/alembic/versions/043_agent_runs.py
git commit -m "chore(neutralize): drop ecisco alembic revisions, repair down_revision chain"
```

### Task 9: Delete enterprise tests

**Objective:** Remove tests whose subject is the enterprise feature.

**Files:** enumerated by the command below.

**Step 1: Enumerate**

```bash
rg -ilw --no-messages 'ecisco|edia_mysql|c5c9|c5_c9|C5_RESIN|raffinate|md_t_lz_price|lz_v_|petrochemical|aipdp' backend/tests | sort > /tmp/neutralize-tests.txt
wc -l < /tmp/neutralize-tests.txt
```
Expected: ~55 files.

**Step 2: Apply the criterion (spec §5 S1)**

- **Delete** a test whose *subject* is enterprise-only (`test_scoped_to_ecisco_bi.py`,
  `test_config_edia.py`, `test_edia_delegation_harness.py`, any `test_*c5c9*`, etc.).
- **Genericize** (do not delete) a test that merely uses an enterprise name as a fixture
  value while exercising generic behaviour — rename the fixture, keep the assertions.

Record which files were deleted vs genericized (needed for the Task 27 report).

**Step 3: Delete**

```bash
git rm --quiet <each deleted path>
```

**Step 4: Verify the remaining suite still collects**

Run: `docker compose run --rm --no-deps --entrypoint "" backend python -m pytest --collect-only -q 2>&1 | tail -5`
Expected: no collection errors.

**Step 5: Commit**

```bash
git commit -m "chore(neutralize): delete enterprise-specific tests"
```

### Task 10: Delete enterprise scripts

**Objective:** Remove scripts hardcoded to the enterprise warehouse.

**Files (delete):**
- `scripts/e2e_c5c9_ui.py`
- `scripts/migrate_erp_datasources.py`
- `scripts/probe_warehouse_deep.py`
- `scripts/probe_warehouse_tier3.py`

**KEEP** (matched only on context-dependent tokens): `e2e_ceo_dynamic.py`,
`e2e_ceo_ui.py`, `e2e_contract_docx.py`, `e2e_qwen_docx.py`, `run_copy_stack.sh`.

**Forecast ops scripts** (`backdate_forecast_runs.py`, `backfill_forecast_runs.py`,
`backfill_upstream_forecast.py`, `seed_and_run_upstream_forecast.py`,
`verify_forecast_pipeline.py`, `test_forecast_run2.py`) are **genericized in Phase 3**,
not deleted here.

**Step 1: Delete + commit**

```bash
git rm --quiet scripts/e2e_c5c9_ui.py scripts/migrate_erp_datasources.py \
  scripts/probe_warehouse_deep.py scripts/probe_warehouse_tier3.py
git commit -m "chore(neutralize): delete enterprise warehouse scripts"
```

### Task 11: Delete enterprise docs and stray dirs

**Objective:** Remove the paper trail.

**Files:**
- `docs/superpowers/plans/*ecisco*`, `docs/superpowers/plans/*media-dashboard*`,
  `docs/superpowers/specs/*ecisco*`, `docs/superpowers/specs/*edia*`
- `docs/plans/ecisco*`
- `docs/archive/root-scratch/EDIA_Sales_Report_2026-07.html`
- `docs/archive/root-scratch/live_editable_c5c9.pptx`
- `.codebuddy/plans/ecisco-*`
- top-level `ecisco_dw_catalog/`

**Step 1: Enumerate then delete**

```bash
rg -ilw --no-messages 'ecisco|edia_mysql|c5c9|c5_c9|C5_RESIN|md_t_lz_price|petrochemical' docs | sort
git rm -r --quiet <each path>
git rm -r --quiet .codebuddy/plans/ecisco-alerts-integration_cb256ab5.md \
  .codebuddy/plans/ecisco-bi-F_PAEZ_JC-migration_15bfb83b.md \
  .codebuddy/plans/ecisco-bi-dashboard-chat-widget_0ce9045d.md \
  .codebuddy/plans/ecisco-bi-single-file-and-doc-quality_c3942b03.md
git rm -r --quiet ecisco_dw_catalog
```

**Step 2: Commit**

```bash
git commit -m "chore(neutralize): delete enterprise docs and stray dirs"
```

### Task 12: Checkpoint — gate delta after deletions

**Objective:** Prove deletions moved the needle before touching code.

**Step 1: Run**

```bash
python3 scripts/check_no_enterprise_tokens.py --quiet > /tmp/neutralize-hits-after-del.txt
wc -l < /tmp/neutralize-hits-before.txt
wc -l < /tmp/neutralize-hits-after-del.txt
```
Expected: a material drop; the remainder is `backend/app` (Phase 3) + fixture-only tests.

---

## Phase 3 — Genericization (spec S2)

Each task below: read the file, remove the enterprise binding, replace with the neutral
mechanism, then confirm the checker no longer reports those lines for that file.

**Universal per-task recipe:**

```bash
# 1. See exactly what is enterprise-specific in this file
python3 scripts/check_no_enterprise_tokens.py --quiet | grep '^<path>'
# 2. Transform
# 3. Confirm clean
python3 scripts/check_no_enterprise_tokens.py --quiet | grep -c '^<path>'   # expect 0
docker compose run --rm --no-deps --entrypoint "" backend python -m pytest -q <related test(s)>
git add <path> && git commit -m "refactor(neutralize): <file> -> domain-agnostic"
```

### Task 13: Forecasting — datasource binding

**Files:**
- Modify: `backend/app/services/forecasting/mysql_data_source.py`
- Modify: `backend/app/services/forecasting/datasource_registry.py`

**Objective:** The forecasting data source must read *whatever* the bound datasource
exposes (already the platform rule: users bind any DB) — delete the hardcoded
`aipdp_data_warehouse_prod` / `md_t_lz_price` / `lz_v_*` / `erp_v_sale_orderentry`
bindings. Keep the driver pattern (normalise whatever columns exist).

**Verification:** `python3 scripts/check_no_enterprise_tokens.py --quiet | grep -c 'mysql_data_source\|datasource_registry'` → `0`; then
`docker compose run --rm --no-deps --entrypoint "" backend python -m pytest -q tests/services/forecasting` → pass.

### Task 14: Forecasting — loaders and preprocessing

**Files:**
- Modify: `backend/app/services/forecasting/features/exogenous_loaders.py`
- Modify: `backend/app/services/forecasting/features/warehouse_loaders.py`
- Modify: `backend/app/services/forecasting/preprocess.py`

**Objective:** Replace hardcoded petrochemical product/price references with the generic
target-series concept supplied by config.

### Task 15: Forecasting — domain signals

**Files:**
- Modify: `backend/app/services/forecasting/domain_signals.py`
- Modify: `backend/app/services/forecasting/domain_signals_ab.py`
- Modify: `backend/app/services/forecasting/domain_signal_calibration.py`
- Modify: `backend/app/services/forecasting/ops/realization_spread.py`
- Modify: `backend/app/services/forecasting/accuracy_tracker.py`

**Objective:** Signals must be registered by config, not named after the vertical. Keep
the A/B mechanism and calibration; delete the petrochemical signal definitions.

### Task 16: Forecasting — finetuning + ops scripts

**Files:**
- Modify: `backend/app/services/forecasting/finetuning/dataset_builder.py`
- Modify: `backend/app/services/forecasting/finetuning/finetune_runner.py`
- Modify: `scripts/backdate_forecast_runs.py`
- Modify: `scripts/backfill_forecast_runs.py`
- Modify: `scripts/backfill_upstream_forecast.py`
- Modify: `scripts/seed_and_run_upstream_forecast.py`
- Modify: `scripts/verify_forecast_pipeline.py`
- Modify: `scripts/test_forecast_run2.py`

**Objective:** Same treatment for the ML/ops path; scripts must take the target series
from arguments/config instead of a hardcoded product.

### Task 17: Services — agents, prompts, routing

**Files:**
- Modify: `backend/app/services/agent_prompts.py`
- Modify: `backend/app/services/system_agents.py`
- Modify: `backend/app/services/response_cache.py`
- Modify: `backend/app/services/skill_routing/post_router_hook.py`

**Objective:** Delete enterprise names/examples from prompts and the router hook. Prompts
must not name the customer or its products.

### Task 18: Services — deep research + db helpers

**Files:**
- Modify: `backend/app/services/deep_research/llm_backend.py`
- Modify: `backend/app/services/deep_research/orchestrator.py`
- Modify: `backend/app/services/db/join_partner.py`

### Task 19: Tool handlers

**Files:**
- Modify: `backend/app/services/tool_handlers/dashboard_tools.py`
- Modify: `backend/app/services/tool_handlers/forecast_tool.py`
- Modify: `backend/app/services/tool_handlers/web_search_tool.py`

**Objective:** Remove per-agent enterprise defaults; tools must be generic per the
platform doctrine (no hardcoded per-agent tool lists).

### Task 20: Router — agents

**Files:**
- Modify: `backend/app/routers/agents.py`

**Objective:** Remove the enterprise comment at ~line 13548 and any enterprise branch.

**Verification:** `python3 scripts/check_no_enterprise_tokens.py --quiet | grep -c 'routers/agents.py'` → `0`.

### Task 21: Domain config — the neutral default

**Files:**
- Modify: `backend/app/domain_configs/default.json`

**Objective:** This is where the neutral default belongs. Remove enterprise identifiers;
keep only generic fields.

### Task 22: Dashboard template + frontend

**Files:**
- Modify: `backend/app/dashboards/_template/frontend/src/index.css`
- Modify: `backend/app/dashboards/_template/frontend/src/panels/index.jsx`
- Modify: `backend/app/dashboards/_template/frontend/src/widgets/index.jsx`
- Modify: `frontend/src/lib/__tests__/runAutomationTask.test.jsx`

**Objective:** `_template` is the neutral base every generated dashboard inherits from —
scrub its 4 occurrences. Genericize the single frontend test.

### Task 23: Backend skill

**Files:**
- Modify: `backend/skills/weekly-report-generation/` (2 hits)

**Objective:** The weekly-report skill must not name the enterprise or its tables.

### Task 24: Checkpoint — gate green

**Objective:** Confirm Phases 2–3 achieved zero own-source hits.

**Step 1: Run**

```bash
python3 scripts/check_no_enterprise_tokens.py
```
Expected: `OK — no enterprise tokens in own source`. If any hit remains, either fix it or
add a justified line to `scripts/enterprise_token_allowlist.txt` — and report every
allowlist entry explicitly.

**Step 2: Commit any residual allowlist/fix**

```bash
git add -p scripts/enterprise_token_allowlist.txt   # explicit paths only
git commit -m "chore(neutralize): gate green — zero own-source enterprise tokens"
```

---

## Phase 4 — Data / DB (spec S3)

### Task 25: Remove the enterprise warehouse KB binding

**Objective:** Delete the `aipdp_data_warehouse_prod` knowledge base and its stored
credentials (decision D1).

**Files:**
- Modify: (copy-stack DB, no repo file)

**Schema note (verified):** `knowledge_bases` holds the connection *in the row itself* —
`host, port, database_name, username, password, db_type` — there is **no** separate
`data_source_id` or credentials table. Deleting the row deletes the credentials. The table
has an `is_deleted` column; **hard-delete the row** (no soft delete — user preference).

**Step 1: Inspect before deleting**

```bash
docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -c \
  "select id, name, db_type, host, port, database_name, username from knowledge_bases \
   where name = 'aipdp_data_warehouse_prod';"
# capture the id, then find dependents (agent_apps.knowledge_bases is a JSON/text list)
docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -c \
  "select id, name from agent_apps where knowledge_bases::text ilike '%aipdp%';"
```
If any agent references it, unbind there too (JSON edit), so no dangling KB id remains.

**Step 2: Hard delete**

```bash
docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -c \
  "delete from knowledge_bases where name = 'aipdp_data_warehouse_prod';"
```

**Step 3: Verify**

```bash
docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -tAc \
  "select count(*) from knowledge_bases where name ilike '%aipdp%' or name ilike '%ecisco%';"
```
Expected: `0`.

---

## Phase 5 — Memory + skills (spec S4)

### Task 26: Scrub memory and skills

**Files:**
- Modify: `~/.hermes/MEMORY.md` (via the `memory` tool: replace/remove entries)
- Modify: `~/.hermes/USER.md` (via the `memory` tool)
- Delete: skill `ecisco-dw-catalog`
- Delete: skill `ecisco-daily-dashboard`
- Modify: skill `zhanlu-forecasting` + its ecisco reference file → generic
- Modify: skill `petrochemical-dashboard-design` → de-brand

**Step 1:** Remove the three memory entries (ERP Ecisco schema, aipdp DW, "not neutral").
**Step 2:** Remove the `Project: Ecisco (NOT NovaChem)` line from USER.md, preserving the
platform-generic directives (DB-agnostic, any DB bound dynamically, pro-grade decks).
**Step 3:** `skill_manage(action="delete", ...)` for the two enterprise skills.
**Step 4:** `skill_manage(action="patch", ...)` the forecasting + dashboard-design skills
to drop petrochemical specifics while keeping the operational knowledge.

---

## Phase 6 — Final verification (spec §6)

### Task 27: Full definition-of-done run on the final tree

**Objective:** Prove the end state. No earlier green run counts.

**Step 1: Gate**

```bash
make check-tokens                                   # expect exit 0
```

**Step 2: Backend imports + boot + health**

```bash
docker compose run --rm --no-deps --entrypoint "" backend python -c "import app.main"
docker compose up -d --build --remove-orphans
curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:5002/healthz   # expect 200
```

**Step 3: Tests**

```bash
docker compose run --rm --no-deps --entrypoint "" backend python -m pytest -q   # expect green
```

**Step 4: Alembic single head**

```bash
docker compose run --rm --no-deps --entrypoint "" backend alembic heads   # exactly one head
```

**Step 5: Dashboard registry neutral**

```bash
ls -1 backend/app/dashboards/ | grep -v __pycache__
```
Expected: no c5c9/c9/ecisco dirs; `_template` + the 5 neutral apps remain.

**Step 6: Frontend build**

```bash
cd frontend && npm run build                        # expect success
```
(If the frontend is served from the built `dist/` by nginx in the stack, this is the
build that must succeed — the stack serves `frontend/dist`.)

**Step 7: Dirty-tree check**

```bash
git status --porcelain > /tmp/neutralize-final-dirty.txt
diff /tmp/neutralize-baseline-dirty.txt /tmp/neutralize-final-dirty.txt
```
Expected: the only differences are this plan's own commits having been made (the 9
pre-existing unrelated files must still be unmodified).

**Step 8: Report**

Report, explicitly:
- every file deleted,
- every file genericized,
- every allowlist entry (target: none),
- the §6 verification results,
- the dirty-tree diff result.

Then stop.

---

## Pitfalls (carry from spec §8)

- **P1** — never use `git grep -E '\b...\b'`; POSIX ERE has no `\b` and silently matches
  nothing. Use the checker (ripgrep-`-w`-equivalent semantics).
- **P2** — vendored trees are excluded by design; do not "fix" them.
- **P3** — never commit the 9 pre-existing dirty files. Stage explicit paths only.
- **P4** — repair the alembic chain or `upgrade head` breaks.
- **P5** — generated dashboards are `DO NOT EDIT`; delete the app dirs, don't hand-edit.
- **P6** — grep every deleted slug/id after deletion for stranded references.
- **P7** — verify on the final tree.
