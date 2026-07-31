"""
Theme System — apply_theme.py

Applies a structured design token theme to an artifact (PPTX, DOCX, HTML, or dashboard).
Runs in a zhanlu sandbox container. Reads input from stdin or /workspace/input/input.json,
writes outputs to /workspace/output/.

Usage (sandbox entrypoint):
    python scripts/apply_theme.py

Usage (standalone):
    python scripts/apply_theme.py --input <input.json> --output-dir <dir>

Dependencies (available in zhanlu-sandbox-python:latest):
    - python-pptx (for PPTX)
    - python-docx (for DOCX)
    - No external deps for HTML (string manipulation)
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# Add validators to path for contrast check
sys.path.insert(0, str(Path(__file__).parent.parent / "validators"))
from contrast_check import check_theme  # noqa: E402


THEMES_DIR = Path(__file__).parent.parent / "themes"
OUTPUT_DIR = Path(os.environ.get("WORKSPACE_OUTPUT", "/workspace/output"))
INPUT_DIR = Path(os.environ.get("WORKSPACE_INPUT", "/workspace/input"))


# ---------------------------------------------------------------------------
# Theme Loading
# ---------------------------------------------------------------------------

def load_preset_theme(name: str) -> dict:
    """Load a preset theme by name from the themes/ directory."""
    theme_path = THEMES_DIR / f"{name}.json"
    if not theme_path.exists():
        raise FileNotFoundError(
            f"Preset theme '{name}' not found. Available: "
            f"{sorted(p.stem for p in THEMES_DIR.glob('*.json'))}"
        )
    return json.loads(theme_path.read_text(encoding="utf-8"))


def merge_custom_theme(custom: dict) -> dict:
    """
    Merge a partial custom theme into a full token set.
    Derives missing colors (text, text_muted, border, semantic) from the provided palette.
    """
    colors_in = custom.get("colors", {})
    light = colors_in.get("light", {})

    # If only flat colors were provided (no light/dark split), treat as light
    if not light and "primary" in colors_in:
        light = dict(colors_in)
        colors_in = {}

    # Derive missing tokens
    # text defaults to a dark neutral (#1e293b), NOT the primary color
    # text_muted is derived from text lightened slightly toward background
    text_default = "#1e293b"
    text_val = light.get("text", text_default)
    bg_val = light.get("background", "#ffffff")
    defaults = {
        "text": text_val,
        "text_muted": _mix(text_val, bg_val, 0.30),
        "border": _mix(text_val, bg_val, 0.82),
        "surface": light.get("surface", _mix(text_val, bg_val, 0.93)),
        "background": bg_val,
        "success": "#16a34a",
        "warning": "#d97706",
        "error": "#dc2626",
        "info": "#0ea5e9",
    }
    for k, v in defaults.items():
        light.setdefault(k, v)

    # Generate dark mode if not provided
    dark = colors_in.get("dark", _generate_dark_mode(light))

    # Merge with custom theme, ensuring all required fields
    merged = {
        "name": custom.get("name", "custom-theme"),
        "description": custom.get("description", "Custom generated theme"),
        "best_for": custom.get("best_for", []),
        "colors": {"light": light, "dark": dark},
        "fonts": custom.get("fonts", {"heading": "Inter", "body": "Inter", "mono": "JetBrains Mono"}),
        "typography": custom.get("typography", {
            "heading_scale": [2.5, 2.0, 1.5, 1.25, 1.0],
            "body_size": "16px",
            "line_height": "1.6",
        }),
        "spacing": custom.get("spacing", {"unit": 4, "scale": [0, 4, 8, 12, 16, 24, 32, 48, 64]}),
        "radius": custom.get("radius", {"sm": 4, "md": 8, "lg": 16, "full": 9999}),
        "shadows": custom.get("shadows", {
            "sm": "0 1px 2px rgba(0,0,0,0.05)",
            "md": "0 4px 6px rgba(0,0,0,0.07)",
            "lg": "0 10px 15px rgba(0,0,0,0.1)",
        }),
    }
    return merged


# ---------------------------------------------------------------------------
# Color Utilities
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    hex_str = hex_str.lstrip("#")
    return int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix(hex_a: str, hex_b: str, ratio: float) -> str:
    """Mix two hex colors. ratio=0 → all a, ratio=1 → all b."""
    ar, ag, ab = _hex_to_rgb(hex_a)
    br, bg, bb = _hex_to_rgb(hex_b)
    r = round(ar + (br - ar) * ratio)
    g = round(ag + (bg - ag) * ratio)
    b = round(ab + (bb - ab) * ratio)
    return _rgb_to_hex(r, g, b)


def _darken(hex_str: str, amount: float = 0.3) -> str:
    return _mix(hex_str, "#000000", amount)


def _lighten(hex_str: str, amount: float = 0.3) -> str:
    return _mix(hex_str, "#ffffff", amount)


def _invert_luminance(hex_str: str) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return _rgb_to_hex(255 - r, 255 - g, 255 - b)


def _generate_dark_mode(light: dict) -> dict:
    """Derive a dark mode palette from a light mode palette."""
    return {
        "primary": _lighten(light.get("primary", "#4a6fa5"), 0.15),
        "secondary": _lighten(light.get("secondary", "#7b9ccc"), 0.1),
        "surface": _darken(light.get("primary", "#1e293b"), 0.85),
        "background": _darken(light.get("primary", "#0f172a"), 0.92),
        "text": _lighten(light.get("background", "#f1f5f9"), 0.05),
        "text_muted": _mix(light.get("primary", "#94a3b8"), "#ffffff", 0.4),
        "border": _darken(light.get("primary", "#334155"), 0.7),
        "success": "#22c55e",
        "warning": "#fbbf24",
        "error": "#ef4444",
        "info": "#38bdf8",
    }


# ---------------------------------------------------------------------------
# Artifact Applicators
# ---------------------------------------------------------------------------

def apply_to_pptx(artifact_path: str, theme: dict, mode: str) -> dict:
    """Apply theme to a PPTX file using python-pptx."""
    try:
        from pptx import Presentation
        from pptx.util import Pt
        from pptx.dml.color import RGBColor
    except ImportError:
        return {"error": "python-pptx not available in sandbox"}

    colors = theme["colors"][mode]
    fonts = theme["fonts"]

    color_map = {k: RGBColor(*_hex_to_rgb(v)) for k, v in colors.items()}

    prs = Presentation(artifact_path)
    colors_replaced = 0
    fonts_replaced = 0

    # Replace theme colors in slide master
    for slide_master in prs.slide_masters:
        for shape in slide_master.shapes:
            _replace_shape_colors(shape, color_map, colors, colors_replaced, fonts_replaced, fonts)

    # Replace in each slide
    for slide in prs.slides:
        for shape in slide.shapes:
            colors_replaced, fonts_replaced = _replace_shape_colors(
                shape, color_map, colors, colors_replaced, fonts_replaced, fonts
            )

    output_path = OUTPUT_DIR / "styled_artifact.pptx"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))

    return {
        "output_path": str(output_path),
        "colors_replaced": colors_replaced,
        "fonts_replaced": fonts_replaced,
    }


def _replace_shape_colors(shape, color_map, colors, cr, fr, fonts):
    """Replace fill and text colors in a shape."""
    # Fill color
    if shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.color and run.font.color.type is not None:
                    try:
                        run.font.color.rgb = color_map["primary"]
                        cr += 1
                    except Exception:
                        pass
                if run.font.name:
                    run.font.name = fonts["body"]
                    fr += 1
    # Fill
    try:
        if shape.fill and shape.fill.type is not None:
            shape.fill.solid()
            shape.fill.fore_color.rgb = color_map["surface"]
            cr += 1
    except Exception:
        pass
    return cr, fr


def apply_to_docx(artifact_path: str, theme: dict, mode: str) -> dict:
    """Apply theme to a DOCX file using python-docx."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
    except ImportError:
        return {"error": "python-docx not available in sandbox"}

    colors = theme["colors"][mode]
    fonts = theme["fonts"]

    doc = Document(artifact_path)
    colors_replaced = 0
    fonts_replaced = 0

    # Update styles
    for style in doc.styles:
        try:
            if hasattr(style, "font"):
                if style.font.name:
                    style.font.name = fonts["body"]
                    fonts_replaced += 1
                if hasattr(style.font, "color") and style.font.color:
                    style.font.color.rgb = RGBColor(*_hex_to_rgb(colors["text"]))
                    colors_replaced += 1
        except Exception:
            pass

    # Update each paragraph's runs
    for para in doc.paragraphs:
        for run in para.runs:
            try:
                run.font.name = fonts["body"]
                fonts_replaced += 1
                if run.font.color and run.font.color.rgb:
                    run.font.color.rgb = RGBColor(*_hex_to_rgb(colors["text"]))
                    colors_replaced += 1
            except Exception:
                pass

    output_path = OUTPUT_DIR / "styled_artifact.docx"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    return {
        "output_path": str(output_path),
        "colors_replaced": colors_replaced,
        "fonts_replaced": fonts_replaced,
    }


def apply_to_html(artifact_path: str, theme: dict, mode: str) -> dict:
    """Apply theme to an HTML file by injecting CSS custom properties."""
    colors = theme["colors"][mode]
    fonts = theme["fonts"]
    radius = theme.get("radius", {})

    css_vars = []
    for name, value in colors.items():
        css_key = name.replace("_", "-")
        css_vars.append(f"  --color-{css_key}: {value};")
    font_heading = fonts["heading"]
    font_body = fonts["body"]
    font_mono = fonts.get("mono", "monospace")
    css_vars.append(f"  --font-heading: {font_heading}, sans-serif;")
    css_vars.append(f"  --font-body: {font_body}, sans-serif;")
    css_vars.append(f"  --font-mono: {font_mono}, monospace;")
    if radius:
        for k, v in radius.items():
            css_vars.append(f"  --radius-{k}: {v}px;")

    theme_name = theme["name"]
    body_size = theme.get("typography", {}).get("body_size", "16px")
    line_height = theme.get("typography", {}).get("line_height", "1.6")
    shadow_sm = theme.get("shadows", {}).get("sm", "none")

    style_block = f"""
<style data-theme="{theme_name}" data-mode="{mode}">
:root {{
{chr(10).join(css_vars)}
  --font-size-body: {body_size};
  --line-height: {line_height};
}}
body {{
  background-color: var(--color-background);
  color: var(--color-text);
  font-family: var(--font-body);
  font-size: var(--font-size-body);
  line-height: var(--line-height);
}}
h1, h2, h3, h4, h5, h6 {{
  color: var(--color-primary);
  font-family: var(--font-heading);
}}
a {{ color: var(--color-primary); }}
a:hover {{ color: var(--color-secondary); }}
table {{ border-color: var(--color-border); }}
th {{ background-color: var(--color-surface); color: var(--color-primary); }}
td {{ border-color: var(--color-border); }}
button, .btn {{
  background-color: var(--color-primary);
  color: var(--color-background);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  padding: 8px 16px;
}}
.card {{
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: {shadow_sm};
}}
</style>
"""

    html = Path(artifact_path).read_text(encoding="utf-8")

    # Remove any previously injected theme style block
    import re
    html = re.sub(r'<style data-theme="[^"]*"[^>]*>.*?</style>\s*', "", html, flags=re.DOTALL)

    # Inject before </head> or prepend
    if "</head>" in html:
        html = html.replace("</head>", f"{style_block}\n</head>")
    else:
        html = style_block + "\n" + html

    output_path = OUTPUT_DIR / "styled_artifact.html"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    return {
        "output_path": str(output_path),
        "colors_replaced": len(colors),
        "fonts_replaced": len(fonts),
    }


def apply_to_dashboard(artifact_path: str, theme: dict, mode: str) -> dict:
    """For dashboard: return theme tokens as JSON for the frontend to consume."""
    tokens = {
        "name": theme["name"],
        "mode": mode,
        "colors": theme["colors"][mode],
        "fonts": theme["fonts"],
        "typography": theme.get("typography", {}),
        "spacing": theme.get("spacing", {}),
        "radius": theme.get("radius", {}),
        "shadows": theme.get("shadows", {}),
    }

    output_path = OUTPUT_DIR / "styled_artifact.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    return {
        "output_path": str(output_path),
        "colors_replaced": len(tokens["colors"]),
        "fonts_replaced": len(tokens["fonts"]),
    }


APPLICATORS = {
    "pptx": apply_to_pptx,
    "docx": apply_to_docx,
    "html": apply_to_html,
    "dashboard": apply_to_dashboard,
}


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run(input_data: dict) -> dict:
    """Main entry: takes input dict, returns output dict."""
    artifact_type = input_data.get("artifact_type", "")
    artifact_path = input_data.get("artifact_path", "")
    theme_name = input_data.get("theme_name")
    custom_theme = input_data.get("custom_theme")
    mode = input_data.get("mode", "light")
    options = input_data.get("options", {})

    # 1. Resolve theme
    if theme_name:
        theme = load_preset_theme(theme_name)
    elif custom_theme:
        theme = merge_custom_theme(custom_theme)
    else:
        return {"status": "failed", "error": "Either theme_name or custom_theme must be provided"}

    # Ensure the requested mode exists
    if mode not in theme.get("colors", {}):
        # Generate the missing mode
        if mode == "dark" and "light" in theme["colors"]:
            theme["colors"]["dark"] = _generate_dark_mode(theme["colors"]["light"])
        elif mode == "light" and "dark" in theme["colors"]:
            theme["colors"]["light"] = _generate_dark_mode(theme["colors"]["dark"])

    # 2. Validate contrast (unless skipped)
    if not options.get("skip_validation", False):
        validation_report = check_theme(theme, mode=mode)
    else:
        validation_report = {
            "contrast_checks": 0,
            "passed": 0,
            "failed": 0,
            "wcag_level": "AA",
            "failures": [],
            "skipped": True,
        }

    # 3. Apply to artifact
    applicator = APPLICATORS.get(artifact_type)
    if not applicator:
        return {
            "status": "failed",
            "error": f"Unsupported artifact type: {artifact_type}. Supported: {list(APPLICATORS.keys())}",
        }

    if not artifact_path or not Path(artifact_path).exists():
        return {"status": "failed", "error": f"Artifact not found: {artifact_path}"}

    apply_result = applicator(artifact_path, theme, mode)

    if "error" in apply_result:
        return {"status": "failed", "error": apply_result["error"]}

    # 4. Write applied theme JSON
    applied_theme_path = OUTPUT_DIR / "applied_theme.json"
    applied_theme_path.write_text(json.dumps(theme, indent=2), encoding="utf-8")

    # 5. Write build manifest
    build_manifest = {
        "theme_applied": theme["name"],
        "mode": mode,
        "tokens_resolved": len(theme["colors"][mode]) + len(theme["fonts"]),
        "colors_replaced": apply_result.get("colors_replaced", 0),
        "fonts_replaced": apply_result.get("fonts_replaced", 0),
    }
    manifest_path = OUTPUT_DIR / "build_manifest.json"
    manifest_path.write_text(json.dumps(build_manifest, indent=2), encoding="utf-8")

    # 6. Write validation report
    report_path = OUTPUT_DIR / "validation_report.json"
    report_path.write_text(json.dumps(validation_report, indent=2), encoding="utf-8")

    # 7. Build output
    output_path = apply_result["output_path"]
    file_size = Path(output_path).stat().st_size if Path(output_path).exists() else 0

    status = "success"
    if validation_report.get("failed", 0) > 0:
        status = "partial"

    return {
        "status": status,
        "artifact_type": artifact_type,
        "theme_name": theme["name"],
        "mode": mode,
        "files": [{"path": output_path, "size_bytes": file_size}],
        "build_manifest": build_manifest,
        "validation_report": validation_report,
    }


def main():
    parser = argparse.ArgumentParser(description="Apply a theme to an artifact")
    parser.add_argument("--input", default=None, help="Path to input JSON. Defaults to /workspace/input/input.json")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to /workspace/output")
    args = parser.parse_args()

    if args.output_dir:
        global OUTPUT_DIR
        OUTPUT_DIR = Path(args.output_dir)

    input_path = Path(args.input) if args.input else INPUT_DIR / "input.json"

    if not input_path.exists():
        print(json.dumps({"status": "failed", "error": f"Input file not found: {input_path}"}))
        sys.exit(1)

    input_data = json.loads(input_path.read_text(encoding="utf-8"))
    result = run(input_data)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
