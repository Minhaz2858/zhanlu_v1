#!/usr/bin/env python3
"""
Zhanlu Evaluation Report Generator — unified runner.

Calls all 4 generation scripts in sequence:
    gen_report_part1.py  →  creates workbook + first 4 sheets
    gen_report_part2a.py →  populates tasks (ARCH/API/SEC/TEST/OBS)
    gen_report_part2b.py →  populates tasks (DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP)
    gen_report_part3.py  →  adds detail sheets (assessments, risks, KPIs)

Usage:
    backend/venv/bin/python generate_evaluation.py
    backend/venv/bin/python generate_evaluation.py --output /tmp/report.xlsx
"""

import importlib.util
import os
import sys


def _import_script(filename: str):
    """Import a Python script by filename (supports hyphenated names)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(script_dir, filename)
    modname = filename.replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(modname, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filepath}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


def generate(output_path: str) -> None:
    """Run the full pipeline: part1 → part2a → part2b → part3."""
    parts = [
        ("gen_report_part1.py",     "Part 1: Workbook + Dashboard, Task List, P0 Sprint, Architecture"),
        ("gen_report_part2a.py",    "Part 2a: Populate tasks (ARCH/API/SEC/TEST/OBS)"),
        ("gen_report_part2b.py",    "Part 2b: Populate tasks (DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP)"),
        ("gen_report_part3.py",     "Part 3: Detail sheets (assessments, risks, KPIs)"),
    ]

    print("=" * 60)
    print("Zhanlu Evaluation Report Generator")
    print(f"Output: {output_path}")
    print("=" * 60)

    for filename, description in parts:
        print(f"\n>>> {description}")
        mod = _import_script(filename)
        mod.main(output_path)

    print(f"\n{'=' * 60}")
    print(f"Report generated: {output_path}")
    print(f"{'=' * 60}")


def main() -> None:
    import argparse

    # Check openpyxl availability
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("ERROR: openpyxl is not installed.", file=sys.stderr)
        print("Please run with: backend/venv/bin/python generate_evaluation.py", file=sys.stderr)
        sys.exit(1)

    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    parser = argparse.ArgumentParser(
        description="Zhanlu Evaluation Report Generator — runs all 4 generation scripts in sequence"
    )
    parser.add_argument(
        "--output", "-o",
        default=default,
        help=f"Output xlsx path (default: {default})",
    )
    args = parser.parse_args()
    generate(args.output)


if __name__ == "__main__":
    main()
