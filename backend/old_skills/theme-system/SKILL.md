# Theme System

## Purpose

Apply consistent, professional design tokens (colors, fonts, spacing) to any artifact — PPTX decks, DOCX documents, HTML pages, and dashboards. Provides 10 curated pre-set themes plus custom theme generation.

## Use when

- An artifact (slide deck, document, report, HTML page, dashboard) needs professional styling.
- Synexia selects this skill through an approved agent skill binding.
- Layer 5 has created an execution node for this skill.
- The user requests a specific visual theme or asks to "make it look professional."

## Inputs

Inputs must be provided as an approved input package:

```json
{
  "instruction": "Apply the ocean-depths theme to the sales report deck",
  "artifact_type": "pptx",
  "artifact_path": "/workspace/input/sales_report.pptx",
  "theme_name": "ocean-depths",
  "mode": "light",
  "custom_theme": null,
  "options": {}
}
```

Or for a custom theme:

```json
{
  "instruction": "Apply a custom theme based on brand colors",
  "artifact_type": "html",
  "artifact_path": "/workspace/input/landing.html",
  "theme_name": null,
  "mode": "light",
  "custom_theme": {
    "name": "brand-corporate",
    "colors": {
      "primary": "#0052cc",
      "secondary": "#0099ff",
      "surface": "#f4f7fa",
      "background": "#ffffff"
    },
    "fonts": {
      "heading": "Inter",
      "body": "Inter"
    }
  },
  "options": {}
}
```

### Input fields

| Field | Type | Required | Description |
|---|---|---|---|
| `instruction` | string | Yes | Natural language instruction |
| `artifact_type` | string | Yes | One of: `pptx`, `docx`, `html`, `dashboard` |
| `artifact_path` | string | Yes | Path to the input artifact in sandbox `/workspace/input` |
| `theme_name` | string | No | A preset theme name (see Available Themes below) |
| `mode` | string | No | `"light"` (default) or `"dark"` |
| `custom_theme` | object | No | A full theme token object (see theme.schema.json) |
| `options` | object | No | Additional options (e.g., `{"font_scale": 1.1}`) |

> Either `theme_name` or `custom_theme` must be provided.

## Outputs

The skill writes to `/workspace/output`:

```
/workspace/output/
├── styled_artifact.{pptx|docx|html}   # The themed artifact
├── build_manifest.json                 # What was applied
├── validation_report.json              # WCAG contrast check results
└── applied_theme.json                  # The resolved theme tokens
```

### Output schema

```json
{
  "status": "success",
  "artifact_type": "pptx",
  "theme_name": "ocean-depths",
  "mode": "light",
  "files": [
    {
      "path": "/workspace/output/styled_artifact.pptx",
      "size_bytes": 456789
    }
  ],
  "build_manifest": {
    "theme_applied": "ocean-depths",
    "tokens_resolved": 18,
    "colors_replaced": 34,
    "fonts_replaced": 6
  },
  "validation_report": {
    "contrast_checks": 12,
    "passed": 12,
    "failed": 0,
    "wcag_level": "AA"
  }
}
```

## Available Themes

| Theme | Vibe | Best For |
|---|---|---|
| `arctic-frost` | Cool, crisp, professional | Healthcare, tech, clean energy |
| `botanical-garden` | Fresh, organic, lively | Food, natural products, sustainability |
| `desert-rose` | Soft, elegant, muted | Fashion, beauty, interior design |
| `forest-canopy` | Natural, grounded, earthy | Environmental, wellness, outdoor brands |
| `golden-hour` | Warm, inviting, autumnal | Hospitality, lifestyle, artisan products |
| `midnight-galaxy` | Dramatic, cosmic, deep | Entertainment, gaming, luxury brands |
| `modern-minimalist` | Clean, neutral, versatile | Tech, architecture, data visualization |
| `ocean-depths` | Calm, professional, maritime | Corporate, finance, consulting |
| `sunset-boulevard` | Warm, vibrant, energetic | Marketing, creative pitches, events |
| `tech-innovation` | Bold, high-contrast, modern | Startups, software launches, AI/ML |

Each theme provides:

- **Light mode and dark mode** color sets
- **Semantic colors**: primary, secondary, surface, background, text, text_muted, border, success, warning, error, info
- **Font pairing**: heading font, body font, mono font
- **Typography scale**: font sizes and weights
- **Spacing scale**: standard spacing tokens
- **Border radius** and **shadow** tokens

## Theme Token Structure

See `schemas/theme.schema.json` for the full schema. Summary:

```json
{
  "name": "ocean-depths",
  "colors": {
    "light": {
      "primary": "#2d8b8b",
      "secondary": "#1a2332",
      "surface": "#e0f2f1",
      "background": "#f1faee",
      "text": "#1a2332",
      "text_muted": "#4a5a6a",
      "border": "#cbd5e1",
      "success": "#16a34a",
      "warning": "#d97706",
      "error": "#dc2626",
      "info": "#0ea5e9"
    },
    "dark": { ... }
  },
  "fonts": {
    "heading": "Inter",
    "body": "Inter",
    "mono": "JetBrains Mono"
  },
  "typography": {
    "heading_scale": [2.5, 2.0, 1.5, 1.25, 1.0],
    "body_size": "16px",
    "line_height": "1.6"
  },
  "spacing": {
    "unit": 4,
    "scale": [0, 4, 8, 12, 16, 24, 32, 48, 64]
  },
  "radius": { "sm": 4, "md": 8, "lg": 16, "full": 9999 },
  "shadows": { "sm": "0 1px 2px rgba(0,0,0,0.05)", ... }
}
```

## Custom Theme Generation

If none of the presets fit, provide a `custom_theme` object with at minimum:

```json
{
  "name": "my-brand",
  "colors": {
    "primary": "#0052cc",
    "secondary": "#0099ff",
    "surface": "#f4f7fa",
    "background": "#ffffff"
  },
  "fonts": {
    "heading": "Inter",
    "body": "Inter"
  }
}
```

The skill will derive `text`, `text_muted`, `border`, and semantic colors automatically, generate both light and dark mode variants, and run WCAG validation.

## Safety rules

- Do not access raw credentials.
- Do not access unrestricted database connections.
- Do not access internet unless policy allows.
- Do not write outside `/workspace/output`.
- Do not bypass validation.
- All themes must pass WCAG AA contrast validation before output.

## Application Process

1. **Resolve theme**: Load preset by name, or merge custom theme with defaults.
2. **Validate tokens**: Run `validators/contrast_check.py` — all text/background pairs must meet WCAG AA (4.5:1 for normal text, 3:1 for large text).
3. **Apply to artifact**:
   - PPTX: Replace theme colors and fonts in slide master + layouts + shapes.
   - DOCX: Replace styles.xml color and font definitions.
   - HTML: Inject a `<style>` block with CSS custom properties.
   - Dashboard: Return theme tokens as JSON for the frontend to consume.
4. **Write outputs**: Styled artifact + build manifest + validation report + applied theme JSON.
5. **Verify**: Re-run contrast check on the output artifact.
