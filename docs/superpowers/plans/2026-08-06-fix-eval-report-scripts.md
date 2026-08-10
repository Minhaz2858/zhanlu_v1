# Fix Evaluation Report Scripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 4 evaluation report generation scripts that hardcode an inaccessible output path (`/root/zhanlu/`), and fill in the 2 empty unified runner scripts so the full pipeline can be re-run from the project directory.

**Architecture:** Each `gen_report_part*.py` script is refactored to wrap its top-level logic in a `main(output_path: str)` function with an argparse `--output` CLI parameter (script-relative default). Both `generate_evaluation.py` and `gen_report.py` become identical unified runners that use `importlib` to import the 4 hyphenated-name scripts as modules and call `main()` in sequence.

**Tech Stack:** Python 3.11, openpyxl 3.1.5 (backend venv), argparse, importlib

## Global Constraints

- **Python interpreter:** Must use `backend/venv/bin/python` — openpyxl is only installed in the backend venv, not system Python.
- **Output path default:** `os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")` — script-relative, no hardcoded absolute paths.
- **Zero content changes:** All task data, scores, styling, sheet layout, and formatting must stay identical to the existing scripts. Only the path handling and script structure change.
- **Both runners identical:** `generate_evaluation.py` and `gen_report.py` must be byte-for-byte identical (alias pattern).

---

### Task 1: Refactor `gen_report_part1.py` into callable function

**Files:**
- Modify: `gen_report_part1.py` (full file — wrap top-level code in function)

**Interfaces:**
- Produces: `def main(output_path: str) -> None` — creates a new Workbook with 4 sheets (评估总览, 详细任务清单, P0紧急冲刺计划, 架构详细评估) and saves to `output_path`.

- [ ] **Step 1: Wrap top-level code in `main(output_path)` function**

Open `gen_report_part1.py`. The entire file body (lines 8-185) is top-level code. Indent all of it inside a new function:

```python
def main(output_path: str) -> None:
    wb = Workbook()
    HDR_FILL = PatternFill("solid", fgColor="1B2A4A")
    # ... all existing code stays exactly the same ...
    
    # Last line changes from:
    #   wb.save("/root/zhanlu/zhanlu_evaluation_report.xlsx")
    # to:
    wb.save(output_path)
    print("Part 1 saved successfully (Sheets: Dashboard, Task List header, P0 Sprint, Architecture Detail)")
```

Replace the hardcoded `"/root/zhanlu/zhanlu_evaluation_report.xlsx"` on line 184 with `output_path`.

- [ ] **Step 2: Add argparse `__main__` block**

Append after the function:

```python
if __name__ == "__main__":
    import argparse
    import os

    default_output = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "zhanlu_evaluation_report.xlsx",
    )
    parser = argparse.ArgumentParser(
        description="Part 1: Generate evaluation report — Dashboard + Task list header"
    )
    parser.add_argument(
        "--output", "-o",
        default=default_output,
        help="Output xlsx path (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output)
```

- [ ] **Step 3: Run script to verify it works**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python gen_report_part1.py --output /tmp/test_part1.xlsx
```
Expected: prints "Part 1 saved successfully..." and creates `/tmp/test_part1.xlsx`.

Verify:
```bash
backend/venv/bin/python -c "from openpyxl import load_workbook; wb=load_workbook('/tmp/test_part1.xlsx'); print('Sheets:', wb.sheetnames); print('Dashboard rows:', wb['评估总览'].max_row)"
```
Expected: 4 sheets `['评估总览', '详细任务清单', 'P0紧急冲刺计划', '架构详细评估']`, dashboard has 32 rows.

- [ ] **Step 4: Commit**

```bash
git add gen_report_part1.py
git commit -m "refactor: wrap gen_report_part1.py in main(output_path) with argparse CLI"
```

---

### Task 2: Refactor `gen_report_part2a.py` into callable function

**Files:**
- Modify: `gen_report_part2a.py` (full file — wrap top-level code in function)

**Interfaces:**
- Consumes: `output_path` must point to an xlsx created by `gen_report_part1.py.main()` (needs "详细任务清单" sheet).
- Produces: `def main(output_path: str) -> None` — loads existing workbook, populates 53 tasks (ARCH/API/SEC/TEST/OBS) into "详细任务清单" sheet, saves.

- [ ] **Step 1: Wrap top-level code in `main(output_path)` function**

Open `gen_report_part2a.py`. Lines 5-108 are top-level code. Indent all inside:

```python
def main(output_path: str) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    wb = load_workbook(output_path)
    ws = wb["详细任务清单"]
    # ... all existing code stays exactly the same ...
    
    # Line 107 changes from:
    #   wb.save("/root/zhanlu/zhanlu_evaluation_report.xlsx")
    # to:
    wb.save(output_path)
    print(f"Part 2a done: {len(T)} tasks written (rows 2-{1+len(T)})")
```

Replace both occurrences of `"/root/zhanlu/zhanlu_evaluation_report.xlsx"` (lines 5 and 107) with `output_path`.

Note: The `from openpyxl import load_workbook` and `from openpyxl.styles import ...` imports (lines 3-4) should move inside `main()` since they're only needed when the function is called, not at import time. The `T = [...]` task data list can stay inside `main()` or move to module level — either is fine, but keeping it inside `main()` is simplest.

- [ ] **Step 2: Add argparse `__main__` block**

Append after the function:

```python
if __name__ == "__main__":
    import argparse
    import os

    default_output = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "zhanlu_evaluation_report.xlsx",
    )
    parser = argparse.ArgumentParser(
        description="Part 2a: Populate tasks (first half) into existing workbook"
    )
    parser.add_argument(
        "--output", "-o",
        default=default_output,
        help="Path to existing xlsx workbook (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output)
```

- [ ] **Step 3: Run script to verify it works**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python gen_report_part2a.py --output /tmp/test_part1.xlsx
```
Expected: prints "Part 2a done: 53 tasks written (rows 2-54)".

Verify:
```bash
backend/venv/bin/python -c "from openpyxl import load_workbook; wb=load_workbook('/tmp/test_part1.xlsx'); ws=wb['详细任务清单']; print('Max row:', ws.max_row); print('Row 2:', ws.cell(row=2,column=2).value)"
```
Expected: max_row = 54, row 2 col 2 = "ARCH-01".

- [ ] **Step 4: Commit**

```bash
git add gen_report_part2a.py
git commit -m "refactor: wrap gen_report_part2a.py in main(output_path) with argparse CLI"
```

---

### Task 3: Refactor `gen_report_part2b.py` into callable function

**Files:**
- Modify: `gen_report_part2b.py` (full file — wrap top-level code in function)

**Interfaces:**
- Consumes: `output_path` must point to an xlsx that has been through `gen_report_part2a.py.main()` (needs "详细任务清单" sheet with 53 tasks already populated).
- Produces: `def main(output_path: str) -> None` — loads existing workbook, populates 68 tasks (DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP) starting from row 55, saves.

- [ ] **Step 1: Wrap top-level code in `main(output_path)` function**

Open `gen_report_part2b.py`. Lines 5-149 are top-level code. Indent all inside:

```python
def main(output_path: str) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    wb = load_workbook(output_path)
    ws = wb["详细任务清单"]
    # ... all existing code stays exactly the same ...
    
    # Line 147 changes from:
    #   wb.save("/root/zhanlu/zhanlu_evaluation_report.xlsx")
    # to:
    wb.save(output_path)
    print(f"Part 2b done: {len(T)} tasks written from row {start_row} to {start_row+len(T)-1}")
    print(f"Total tasks in task list: {start_row+len(T)-2}")
```

Replace both occurrences of `"/root/zhanlu/zhanlu_evaluation_report.xlsx"` (lines 5 and 147) with `output_path`.

- [ ] **Step 2: Add argparse `__main__` block**

Append after the function:

```python
if __name__ == "__main__":
    import argparse
    import os

    default_output = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "zhanlu_evaluation_report.xlsx",
    )
    parser = argparse.ArgumentParser(
        description="Part 2b: Populate remaining tasks into existing workbook"
    )
    parser.add_argument(
        "--output", "-o",
        default=default_output,
        help="Path to existing xlsx workbook (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output)
```

- [ ] **Step 3: Run script to verify it works**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python gen_report_part2b.py --output /tmp/test_part1.xlsx
```
Expected: prints "Part 2b done: 68 tasks written from row 55 to 122" and "Total tasks in task list: 120".

Verify:
```bash
backend/venv/bin/python -c "from openpyxl import load_workbook; wb=load_workbook('/tmp/test_part1.xlsx'); ws=wb['详细任务清单']; print('Max row:', ws.max_row); print('Row 55:', ws.cell(row=55,column=2).value)"
```
Expected: max_row = 122, row 55 col 2 = "DATA-01".

- [ ] **Step 4: Commit**

```bash
git add gen_report_part2b.py
git commit -m "refactor: wrap gen_report_part2b.py in main(output_path) with argparse CLI"
```

---

### Task 4: Refactor `gen_report_part3.py` into callable function

**Files:**
- Modify: `gen_report_part3.py` (full file — wrap top-level code in function)

**Interfaces:**
- Consumes: `output_path` must point to an xlsx that has been through `gen_report_part2b.py.main()`.
- Produces: `def main(output_path: str) -> None` — loads existing workbook, adds 3 sheets (各维度详细评估 with 63 items, 风险矩阵 with 15 risks, 交付评审与KPI with 15 KPIs), saves.

- [ ] **Step 1: Wrap top-level code in `main(output_path)` function**

Open `gen_report_part3.py`. Lines 5-191 are top-level code. Indent all inside:

```python
def main(output_path: str) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    wb = load_workbook(output_path)
    # ... all existing code stays exactly the same ...
    
    # Line 189 changes from:
    #   wb.save("/root/zhanlu/zhanlu_evaluation_report.xlsx")
    # to:
    wb.save(output_path)
    print(f"Part 3 done. Sheets added: 各维度详细评估 ({len(assessments)} items), 风险矩阵 ({len(risks)} items), 交付评审与KPI ({len(kpis)} items)")
    print(f"Final file: {output_path}")
```

Replace both occurrences of `"/root/zhanlu/zhanlu_evaluation_report.xlsx"` (lines 5 and 189) with `output_path`. Also update line 191's print statement to use `output_path` instead of the hardcoded path.

- [ ] **Step 2: Add argparse `__main__` block**

Append after the function:

```python
if __name__ == "__main__":
    import argparse
    import os

    default_output = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "zhanlu_evaluation_report.xlsx",
    )
    parser = argparse.ArgumentParser(
        description="Part 3: Add remaining detail sheets (assessment, risk, KPI)"
    )
    parser.add_argument(
        "--output", "-o",
        default=default_output,
        help="Path to existing xlsx workbook (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output)
```

- [ ] **Step 3: Run script to verify it works**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python gen_report_part3.py --output /tmp/test_part1.xlsx
```
Expected: prints "Part 3 done. Sheets added: 各维度详细评估 (63 items), 风险矩阵 (15 items), 交付评审与KPI (15 items)".

Verify:
```bash
backend/venv/bin/python -c "from openpyxl import load_workbook; wb=load_workbook('/tmp/test_part1.xlsx'); print('Sheets:', wb.sheetnames); print('Total sheets:', len(wb.sheetnames))"
```
Expected: 7 sheets `['评估总览', '详细任务清单', 'P0紧急冲刺计划', '架构详细评估', '各维度详细评估', '风险矩阵', '交付评审与KPI']`.

- [ ] **Step 4: Commit**

```bash
git add gen_report_part3.py
git commit -m "refactor: wrap gen_report_part3.py in main(output_path) with argparse CLI"
```

---

### Task 5: Create unified runner — `generate_evaluation.py` and `gen_report.py`

**Files:**
- Create (overwrite): `generate_evaluation.py` (currently 0 bytes)
- Create (overwrite): `gen_report.py` (currently 0 bytes)

**Interfaces:**
- Consumes: `main(output_path)` function from each of `gen_report_part1.py`, `gen_report_part2a.py`, `gen_report_part2b.py`, `gen_report_part3.py`.
- Produces: A unified runner script that imports all 4 part-scripts via `importlib` and calls `main()` in sequence. Both files are byte-for-byte identical.

- [ ] **Step 1: Write `generate_evaluation.py`**

```python
#!/usr/bin/env python3
"""Unified runner: generates the full Zhanlu evaluation report xlsx.

Runs all 4 parts in sequence:
  1. gen_report_part1.py  — creates workbook (Dashboard, Task List header, P0 Sprint, Architecture Detail)
  2. gen_report_part2a.py — populates 53 tasks (ARCH/API/SEC/TEST/OBS)
  3. gen_report_part2b.py — populates 68 tasks (DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP)
  4. gen_report_part3.py  — adds 3 sheets (Dimension Assessment, Risk Matrix, KPI)

Usage:
    backend/venv/bin/python generate_evaluation.py
    backend/venv/bin/python generate_evaluation.py --output /tmp/report.xlsx
"""
import argparse
import importlib.util
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PART_SCRIPTS = [
    "gen_report_part1.py",
    "gen_report_part2a.py",
    "gen_report_part2b.py",
    "gen_report_part3.py",
]


def _import_script(filename: str):
    """Import a hyphenated-name .py file as a module via importlib."""
    filepath = os.path.join(SCRIPT_DIR, filename)
    modname = filename.replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(modname, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(output_path: str) -> None:
    # Pre-check: openpyxl must be available
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print(
            "ERROR: openpyxl is not installed. "
            "Use backend/venv/bin/python to run this script:",
            file=sys.stderr,
        )
        print(
            f"  {os.path.join('backend', 'venv', 'bin', 'python')} {os.path.basename(sys.argv[0])}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Output: {output_path}")
    print(f"Python: {sys.executable}")
    print(f"openpyxl: {openpyxl.__version__}")
    print()

    total_start = time.time()

    for i, script in enumerate(PART_SCRIPTS, 1):
        print(f"[{i}/{len(PART_SCRIPTS)}] Running {script}...")
        start = time.time()
        mod = _import_script(script)
        mod.main(output_path)
        elapsed = time.time() - start
        print(f"  Done in {elapsed:.1f}s")

    total_elapsed = time.time() - total_start
    print()
    print(f"All parts complete. Total time: {total_elapsed:.1f}s")
    print(f"Report saved to: {output_path}")

    # Quick validation
    from openpyxl import load_workbook

    wb = load_workbook(output_path)
    print(f"Sheets ({len(wb.sheetnames)}): {', '.join(wb.sheetnames)}")
    task_ws = wb["详细任务清单"]
    print(f"Task list rows: {task_ws.max_row} (1 header + {task_ws.max_row - 1} tasks)")


if __name__ == "__main__":
    default_output = os.path.join(SCRIPT_DIR, "zhanlu_evaluation_report.xlsx")
    parser = argparse.ArgumentParser(
        description="Generate the full Zhanlu evaluation report xlsx (runs all 4 parts)"
    )
    parser.add_argument(
        "--output", "-o",
        default=default_output,
        help="Output xlsx path (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output)
```

- [ ] **Step 2: Copy to `gen_report.py` (identical)**

```bash
cp /home/ysk2025/zhanlu_7_30/generate_evaluation.py /home/ysk2025/zhanlu_7_30/gen_report.py
```

Verify they're identical:
```bash
diff /home/ysk2025/zhanlu_7_30/generate_evaluation.py /home/ysk2025/zhanlu_7_30/gen_report.py
```
Expected: no output (files are identical).

- [ ] **Step 3: Run unified runner to verify end-to-end**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python generate_evaluation.py --output /tmp/test_full_report.xlsx
```
Expected output:
```
Output: /tmp/test_full_report.xlsx
Python: .../backend/venv/bin/python
openpyxl: 3.1.5

[1/4] Running gen_report_part1.py...
  Done in Xs
[2/4] Running gen_report_part2a.py...
  Done in Xs
[3/4] Running gen_report_part2b.py...
  Done in Xs
[4/4] Running gen_report_part3.py...
  Done in Xs

All parts complete. Total time: Xs
Report saved to: /tmp/test_full_report.xlsx
Sheets (7): 评估总览, 详细任务清单, P0紧急冲刺计划, 架构详细评估, 各维度详细评估, 风险矩阵, 交付评审与KPI
Task list rows: 141 (1 header + 140 tasks)
```

- [ ] **Step 4: Verify `gen_report.py` produces identical output**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python gen_report.py --output /tmp/test_full_report2.xlsx
```
Then compare:
```bash
backend/venv/bin/python -c "
from openpyxl import load_workbook
wb1 = load_workbook('/tmp/test_full_report.xlsx')
wb2 = load_workbook('/tmp/test_full_report2.xlsx')
assert wb1.sheetnames == wb2.sheetnames, 'Sheet names differ'
for name in wb1.sheetnames:
    ws1, ws2 = wb1[name], wb2[name]
    assert ws1.max_row == ws2.max_row, f'{name}: row count differs ({ws1.max_row} vs {ws2.max_row})'
    assert ws1.max_column == ws2.max_column, f'{name}: col count differs'
print('Both reports are structurally identical')
"
```
Expected: "Both reports are structurally identical".

- [ ] **Step 5: Commit**

```bash
git add generate_evaluation.py gen_report.py
git commit -m "feat: fill unified evaluation report runners (generate_evaluation.py + gen_report.py)"
```

---

### Task 6: Regenerate production report and clean up

**Files:**
- Output: `zhanlu_evaluation_report.xlsx` (project root — overwrite existing 51KB file)

- [ ] **Step 1: Regenerate the production report**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30 && backend/venv/bin/python generate_evaluation.py
```
Expected: creates/overwrites `zhanlu_evaluation_report.xlsx` in project root.

- [ ] **Step 2: Verify the production report**

Run:
```bash
backend/venv/bin/python -c "
from openpyxl import load_workbook
wb = load_workbook('/home/ysk2025/zhanlu_7_30/zhanlu_evaluation_report.xlsx')
print('Sheets:', wb.sheetnames)
print('Sheet count:', len(wb.sheetnames))
ws = wb['详细任务清单']
print('Task list rows:', ws.max_row)
# Spot-check a few cells
print('ARCH-01 title:', ws.cell(row=2, column=3).value)
print('COMP-11 title:', ws.cell(row=122, column=3).value)
ws_dash = wb['评估总览']
print('Dashboard rows:', ws_dash.max_row)
ws_risk = wb['风险矩阵']
print('Risk rows:', ws_risk.max_row)
ws_kpi = wb['交付评审与KPI']
print('KPI rows:', ws_kpi.max_row)
"
```
Expected:
- 7 sheets
- Task list: 141 rows
- ARCH-01 title: "引入DDD分层架构(Domain/Application/Infra/Interface)"
- COMP-11 title: "第三方依赖安全与供应链"
- Dashboard: 32 rows
- Risk: 16 rows (1 header + 15 risks)
- KPI: 16 rows (1 header + 15 KPIs)

- [ ] **Step 3: Clean up temp files**

```bash
rm -f /tmp/test_part1.xlsx /tmp/test_full_report.xlsx /tmp/test_full_report2.xlsx
```

- [ ] **Step 4: Commit**

```bash
git add zhanlu_evaluation_report.xlsx
git commit -m "chore: regenerate evaluation report with fixed scripts"
```

- [ ] **Step 5: Update memory**

Append to `/home/ysk2025/zhanlu_7_30/.codebuddy/memory/2026-08-06.md`:

```
## Part 2 Fix: Evaluation Report Scripts

- Refactored 4 `gen_report_part*.py` scripts: wrapped top-level code in `main(output_path)`, added argparse `--output` CLI with script-relative default path
- Filled both `generate_evaluation.py` and `gen_report.py` as identical unified runners using importlib to import hyphenated-name scripts
- All hardcoded `/root/zhanlu/zhanlu_evaluation_report.xlsx` paths replaced with configurable `output_path` parameter
- Report content unchanged: 7 sheets, 140 tasks, 63 assessments, 15 risks, 15 KPIs
- Scripts must run with `backend/venv/bin/python` (openpyxl only in venv)
- Verified end-to-end: `backend/venv/bin/python generate_evaluation.py` regenerates the full report
```
