# Fix Part 2: Evaluation Report Scripts — Design Spec

**Date:** 2026-08-06
**Status:** Approved

## Problem

The evaluation report generation pipeline (`gen_report_part1/2a/2b/3.py`) has two issues:

1. **Wrong output path**: All 4 scripts hardcode `/root/zhanlu/zhanlu_evaluation_report.xlsx` — a directory that is inaccessible (permission denied) on the current dev server. The actual project lives at `/home/ysk2025/zhanlu_7_30/`.
2. **Empty runner scripts**: `generate_evaluation.py` and `gen_report.py` are both 0 bytes — there's no unified entry point to run the full pipeline.

The report xlsx itself already exists and is complete (51KB, 7 sheets, 140 tasks), but the scripts cannot be re-run to regenerate it.

## Solution

### 1. Refactor each `gen_report_part*.py` into a callable function

Wrap all top-level logic in each script into `def main(output_path: str) -> None:`. Add an `if __name__ == "__main__":` block with argparse:

```python
if __name__ == "__main__":
    import argparse, os
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    p = argparse.ArgumentParser(description="Generate evaluation report — Part N")
    p.add_argument("--output", "-o", default=default, help="Output xlsx path")
    args = p.parse_args()
    main(args.output)
```

- Default path is **script-relative** (`os.path.dirname(os.path.abspath(__file__))`) — works from any CWD.
- Replace every hardcoded `/root/zhanlu/zhanlu_evaluation_report.xlsx` with the `output_path` parameter.
- **Zero content changes**: all task data, scores, styling, sheet layout stays identical.

### 2. Fill both runner scripts (identical alias pattern)

Both `generate_evaluation.py` and `gen_report.py` become the same unified runner:

- Accept `--output` CLI arg (same script-relative default).
- Pre-check: verify `openpyxl` is importable; exit with helpful message if not (tells user to use `backend/venv/bin/python`).
- Use `importlib.util.spec_from_file_location` to import the 4 hyphenated-name scripts as modules.
- Call `part1.main(output)` → `part2a.main(output)` → `part2b.main(output)` → `part3.main(output)` in sequence.
- Print progress after each step.

### 3. Execution model

```bash
# Regenerate report (from project root):
backend/venv/bin/python generate_evaluation.py

# Or with custom output path:
backend/venv/bin/python generate_evaluation.py --output /tmp/report.xlsx

# Or run individual parts:
backend/venv/bin/python gen_report_part1.py --output ./report.xlsx
```

## What stays unchanged

- Report content: 15 dimensions, 140 tasks, 63 detailed assessments, 15 risks, 15 KPIs
- Sheet structure: 7 sheets (评估总览, 详细任务清单, P0紧急冲刺计划, 架构详细评估, 各维度详细评估, 风险矩阵, 交付评审与KPI)
- All styling: fonts, fills, borders, colors, column widths

## Files affected

| File | Action |
|------|--------|
| `gen_report_part1.py` | Wrap in `main(output_path)`, add argparse, replace hardcoded path |
| `gen_report_part2a.py` | Same |
| `gen_report_part2b.py` | Same |
| `gen_report_part3.py` | Same |
| `generate_evaluation.py` | Fill with unified runner (importlib + sequential calls) |
| `gen_report.py` | Identical copy of `generate_evaluation.py` |

## Verification

After implementation:
1. Run `backend/venv/bin/python generate_evaluation.py` — should produce `zhanlu_evaluation_report.xlsx` in project root.
2. Load the output with openpyxl and verify: 7 sheets, 141 rows in task list (1 header + 140 tasks), 32 rows in dashboard.
3. Run with `--output /tmp/test.xlsx` — should write to the specified path.
4. Run `gen_report.py` — should produce identical output.
