"""
WCAG contrast ratio validator for theme color pairs.

Checks that text/background color combinations meet WCAG AA standards:
  - Normal text (< 18pt or < 14pt bold): minimum contrast ratio 4.5:1
  - Large text (>= 18pt or >= 14pt bold): minimum contrast ratio 3.0:1
  - UI components (borders, etc.): minimum contrast ratio 3.0:1

Usage:
    python contrast_check.py <theme.json> [--mode light|dark] [--large-text]

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
    2 — invalid input
"""

import sys
import json
import argparse
from pathlib import Path


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert a hex color string (#RRGGBB) to an (R, G, B) tuple."""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    return (
        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
    )


def relative_luminance(r: int, g: int, b: int) -> float:
    """Calculate the relative luminance of an RGB color per WCAG 2.1."""
    def channel(c: int) -> float:
        cs = c / 255.0
        return cs / 12.92 if cs <= 0.03928 else ((cs + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Calculate the WCAG contrast ratio between two hex colors."""
    fg_r, fg_g, fg_b = hex_to_rgb(fg_hex)
    bg_r, bg_g, bg_b = hex_to_rgb(bg_hex)
    l1 = relative_luminance(fg_r, fg_g, fg_b)
    l2 = relative_luminance(bg_r, bg_g, bg_b)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def check_theme(
    theme: dict,
    mode: str = "light",
    large_text: bool = False,
) -> dict:
    """
    Validate all text/background pairs in a theme.

    Returns a report dict:
    {
        "contrast_checks": int,
        "passed": int,
        "failed": int,
        "wcag_level": "AA",
        "failures": [...]
    }
    """
    colors = theme.get("colors", {}).get(mode, {})
    if not colors:
        return {
            "contrast_checks": 0,
            "passed": 0,
            "failed": 1,
            "wcag_level": "AA",
            "failures": [{"error": f"No colors found for mode '{mode}'"}],
        }

    # Minimum contrast ratios per WCAG 2.1
    text_min = 3.0 if large_text else 4.5   # 1.4.3 Contrast (Minimum)
    ui_min = 3.0                             # 1.4.11 Non-text Contrast

    bg = colors.get("background", "#ffffff")
    surface = colors.get("surface", "#ffffff")

    # (foreground, background, context, required_ratio, is_hard_fail)
    # Hard fails = body text + primary text + error (must be readable)
    # Soft fails = accents, indicators, borders (warnings only)
    checks = [
        # Body text: must meet 4.5:1 (hard fail)
        (colors.get("text"), bg, "text on background", text_min, True),
        (colors.get("text"), surface, "text on surface", text_min, True),
        (colors.get("text_muted"), bg, "text_muted on background", text_min, True),
        (colors.get("text_muted"), surface, "text_muted on surface", text_min, True),
        # Primary as text (links, headings): must meet 4.5:1 (hard fail)
        (colors.get("primary"), bg, "primary on background", text_min, True),
        (colors.get("primary"), surface, "primary on surface", text_min, True),
        # Secondary accent: 3:1 per 1.4.11 (warning only)
        (colors.get("secondary"), bg, "secondary on background", ui_min, False),
        (colors.get("secondary"), surface, "secondary on surface", ui_min, False),
        # Semantic indicators: 3:1 per 1.4.11 (warning, except error = hard)
        (colors.get("success"), bg, "success on background", ui_min, False),
        (colors.get("warning"), bg, "warning on background", ui_min, False),
        (colors.get("error"), bg, "error on background", ui_min, True),
        (colors.get("info"), bg, "info on background", ui_min, False),
        # Border: 3:1 per 1.4.11 (warning only)
        (colors.get("border"), bg, "border on background", ui_min, False),
    ]

    checks = [(fg, bk, ctx, req, h) for fg, bk, ctx, req, h in checks if fg and bk]

    failures = []
    warnings = []
    passed = 0

    for fg, bk, context, required, hard_fail in checks:
        ratio = contrast_ratio(fg, bk)
        if ratio >= required:
            passed += 1
        elif hard_fail:
            failures.append({
                "foreground": fg,
                "background": bk,
                "ratio": round(ratio, 2),
                "required": required,
                "context": context,
                "severity": "hard",
            })
        else:
            warnings.append({
                "foreground": fg,
                "background": bk,
                "ratio": round(ratio, 2),
                "required": required,
                "context": context,
                "severity": "warning",
            })

    return {
        "contrast_checks": len(checks),
        "passed": passed,
        "failed": len(failures),
        "warnings": len(warnings),
        "wcag_level": "AA",
        "failures": failures,
        "warnings_detail": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description="WCAG contrast validator for theme tokens")
    parser.add_argument("theme_file", help="Path to theme JSON file")
    parser.add_argument("--mode", choices=["light", "dark"], default="light", help="Color mode to check")
    parser.add_argument("--large-text", action="store_true", help="Use large text threshold (3.0:1)")
    args = parser.parse_args()

    theme_path = Path(args.theme_file)
    if not theme_path.exists():
        print(f"Error: theme file not found: {theme_path}", file=sys.stderr)
        sys.exit(2)

    try:
        theme = json.loads(theme_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(2)

    report = check_theme(theme, mode=args.mode, large_text=args.large_text)

    print(json.dumps(report, indent=2))
    sys.exit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
