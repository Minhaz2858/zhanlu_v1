---
name: pptx
description: Create, read, and edit PowerPoint .pptx presentations — executive decks, quarterly reviews, board presentations, and business proposals. Use for: Zhanlu executive briefings, stakeholder reports, project proposals.
category: productivity
---



# PPTX Skill

## Pre-Generation Decision Tree

**Answer these 6 questions in order before writing any code or opening pptxgenjs / python-pptx.** Each answer pins a default that the rest of the skill follows. If the user has not specified, pick the most conservative default and commit to it across the whole deck.

1. **Audience** — Who is reading this?
   - *C-suite / board* → 5-8 slides max, big numbers, no jargon, 60-72pt data callouts
   - *Executives / directors* → 8-12 slides, balance story and data, narrative arc
   - *Peers / ICs* → 12-20 slides, technical depth OK, charts welcome
   - *External / clients* → assume no prior context, glossary any acronyms, polished cover

2. **Length** — How many slides? **One screen per thought.** Never pad. If you can't justify a slide in one sentence, cut it. Common sweet spots: 5, 8, 12, 20.

3. **Formality** — Pick one and apply uniformly:
   - *Board-formal* — dark cover, no exclamation marks, serif title font
   - *Business-casual* — clean sans-serif, friendly tone, light accent color
   - *Conference / pitch* — bold colors, big imagery, one-liner slides

4. **Data density** — How much numeric content?
   - *Low* (story deck) — 1-2 data callouts across the whole deck
   - *Medium* (review deck) — most slides have a stat or chart
   - *High* (analyst report) — tables and charts dominate; consider an appendix slide

5. **Brand / template** — Is there an existing template or palette?
   - *Yes (template provided)* — Read [editing.md](editing.md); do NOT redesign
   - *Yes (palette / brand colors specified)* — use them; no improvisation
   - *No* — pick a palette from *Color Palettes* below; commit to it

6. **Intent** — What's the single thing the audience should remember tomorrow?
   - Write it in one sentence. If you can't, the deck isn't ready.
   - Use that sentence as the cover subtitle AND the closing summary headline.

**Output of this step:** a 5-line brief (audience / length / formality / density / brand / one-sentence intent) you keep open while building. The brief is the spec — if a later slide contradicts it, the slide is wrong, not the brief.

---

## Quick Reference

| Task | Guide |
|------|-------|
| Read/analyze content | `python -m markitdown presentation.pptx` |
| Edit or create from template | Read [editing.md](editing.md) |
| Create from scratch | Read [pptxgenjs.md](pptxgenjs.md) |
| Choose slide type & layout | Read [references/slide-types.md](references/slide-types.md) |
| Choose visual style (corner radius, spacing) | Read [references/style-recipes.md](references/style-recipes.md) |
| Chart colors, data validation, chart styling | Read [references/chart-patterns.md](references/chart-patterns.md) |

---

## Reading Content

```bash
# Text extraction
python -m markitdown presentation.pptx

# Visual overview
python scripts/thumbnail.py presentation.pptx

# Raw XML
python scripts/office/unpack.py presentation.pptx unpacked/
```

---

## Editing Workflow

**Read [editing.md](editing.md) for full details.**

1. Analyze template with `thumbnail.py`
2. Unpack → manipulate slides → edit content → clean → pack

---

## Creating from Scratch

**Read [pptxgenjs.md](pptxgenjs.md) for full details.**

Use when no template or reference presentation is available.

---

## Design Ideas

**Don't create boring slides.** Plain bullets on a white background won't impress anyone. Consider ideas from this list for each slide.

### Before Starting

- **Pick a bold, content-informed color palette**: The palette should feel designed for THIS topic. If swapping your colors into a completely different presentation would still "work," you haven't made specific enough choices.
- **Dominance over equality**: One color should dominate (60-70% visual weight), with 1-2 supporting tones and one sharp accent. Never give all colors equal weight.
- **Dark/light contrast**: Dark backgrounds for title + conclusion slides, light for content ("sandwich" structure). Or commit to dark throughout for a premium feel.
- **Commit to a visual motif**: Pick ONE distinctive element and repeat it — rounded image frames, icons in colored circles, thick single-side borders. Carry it across every slide.

### Color Palettes

Choose colors that match your topic — don't default to generic blue. Use these palettes as inspiration:

| Theme | Primary | Secondary | Accent |
|-------|---------|-----------|--------|
| **Midnight Executive** | `1E2761` (navy) | `CADCFC` (ice blue) | `FFFFFF` (white) |
| **Forest & Moss** | `2C5F2D` (forest) | `97BC62` (moss) | `F5F5F5` (cream) |
| **Coral Energy** | `F96167` (coral) | `F9E795` (gold) | `2F3C7E` (navy) |
| **Warm Terracotta** | `B85042` (terracotta) | `E7E8D1` (sand) | `A7BEAE` (sage) |
| **Ocean Gradient** | `065A82` (deep blue) | `1C7293` (teal) | `21295C` (midnight) |
| **Charcoal Minimal** | `36454F` (charcoal) | `F2F2F2` (off-white) | `212121` (black) |
| **Teal Trust** | `028090` (teal) | `00A896` (seafoam) | `02C39A` (mint) |
| **Berry & Cream** | `6D2E46` (berry) | `A26769` (dusty rose) | `ECE2D0` (cream) |
| **Sage Calm** | `84B59F` (sage) | `69A297` (eucalyptus) | `50808E` (slate) |
| **Cherry Bold** | `990011` (cherry) | `FCF6F5` (off-white) | `2F3C7E` (navy) |
| **Modern & Wellness** | `006D77` (teal) | `83C5BE` (light teal) | `E29578` (coral) |
| **Nature & Outdoors** | `606C38` (olive) | `283618` (dark green) | `BC6C25` (burnt orange) |
| **Vintage & Academic** | `780000` (deep red) | `003049` (navy) | `669BBC` (steel blue) |
| **Bohemian** | `CCD5AE` (sage green) | `D4A373` (tan) | `414833` (dark olive) |
| **Craft & Artisan** | `7F5539` (brown) | `656D4A` (moss) | `414833` (dark olive) |
| **Tech & Night** | `000814` (black) | `003566` (deep blue) | `FFC300` (gold) |
| **Coastal Coral** | `0081A7` (ocean blue) | `00AFB9` (teal) | `F07167` (coral) |
| **Platinum White-Gold** | `0A0A0A` (black) | `0070F3` (blue) | `D4AF37` (gold) |

### For Each Slide

**Every slide needs a visual element** — image, chart, icon, or shape. Text-only slides are forgettable.

**Layout options:**
- Two-column (text left, illustration on right)
- Icon + text rows (icon in colored circle, bold header, description below)
- 2x2 or 2x3 grid (image on one side, grid of content blocks on other)
- Half-bleed image (full left or right side) with content overlay

**Data display:**
- Large stat callouts (big numbers 60-72pt with small labels below)
- Comparison columns (before/after, pros/cons, side-by-side options)
- Timeline or process flow (numbered steps, arrows)

**Visual polish:**
- Icons in small colored circles next to section headers
- Italic accent text for key stats or taglines

### Typography

**Choose an interesting font pairing** — don't default to Arial. Pick a header font with personality and pair it with a clean body font.

**Chinese font support**: Use **Microsoft YaHei** for Chinese text. For mixed Chinese-English content, use Microsoft YaHei for Chinese and one of the fonts below for English.

| Header Font | Body Font |
|-------------|-----------|
| Georgia | Calibri |
| Arial Black | Arial |
| Calibri | Calibri Light |
| Cambria | Calibri |
| Trebuchet MS | Calibri |
| Impact | Arial |
| Palatino | Garamond |
| Consolas | Calibri |

| Element | Size | Weight |
|---------|------|--------|
| Slide title | 36-44pt | Bold |
| Section header | 20-24pt | Bold |
| Body text | 14-16pt | Regular (never bold) |
| Captions | 10-12pt muted | Regular |

**No bold for body text** — reserve bold for titles and headings only. Body paragraphs, descriptions, captions, and footnotes use regular weight.

### Spacing

- 0.5" minimum margins
- 0.3-0.5" between content blocks
- Leave breathing room—don't fill every inch

### Avoid (Common Mistakes)

- **Don't repeat the same layout** — vary columns, cards, and callouts across slides
- **Don't center body text** — left-align paragraphs and lists; center only titles
- **Don't skimp on size contrast** — titles need 36pt+ to stand out from 14-16pt body
- **Don't default to blue** — pick colors that reflect the specific topic
- **Don't mix spacing randomly** — choose 0.3" or 0.5" gaps and use consistently
- **Don't style one slide and leave the rest plain** — commit fully or keep it simple throughout
- **Don't create text-only slides** — add images, icons, charts, or visual elements; avoid plain title + bullets
- **Don't forget text box padding** — when aligning lines or shapes with text edges, set `margin: 0` on the text box or offset the shape to account for padding
- **Don't use low-contrast elements** — icons AND text need strong contrast against the background; avoid light text on light backgrounds or dark text on dark backgrounds
- **NEVER use accent lines under titles** — these are a hallmark of AI-generated slides; use whitespace or background color instead
- **Don't bold body text** — reserve bold for titles and headings only; body paragraphs and captions use regular weight
- **Don't skip the page number badge** — all slides except the cover MUST include a page number badge (see [pptxgenjs.md → Page Number Badge](pptxgenjs.md#page-number-badge) for code)

### Style Recipes

The same palette can look dramatically different depending on corner radius and spacing. Choose a style that matches the presentation tone — **Sharp & Compact** for data reports, **Soft & Balanced** for corporate, **Rounded & Spacious** for marketing, **Pill & Airy** for premium brand.

See [references/style-recipes.md](references/style-recipes.md) for the full 4-style system with rectRadius values, spacing specifications, component mapping, and mixing rules.

---

## Content Density

**The single most violated rule in agent-generated decks: too much text per slide.** Use this section as a hard ceiling — if a slide exceeds it, cut content or split the slide.

### The 6 × 6 Rule

A standard 16:9 content slide holds at most **6 lines of body text, 6 words per line** (≈ 36 words / 200 characters). This is not a suggestion; it's a presentation-design ceiling that the audience can actually read at normal projector distance.

| Slide type | Body word budget | Lines | Notes |
|---|---|---|---|
| Cover | 8-12 | 2 | Title + subtitle only |
| Section divider | 5-8 | 1-2 | Number + name + optional one-liner |
| TOC | 30-40 | 6-8 | Numbered list of section names only |
| Content | ≤ 36 | ≤ 6 | Hard ceiling for any "title + body" slide |
| Data callout | 6-8 | 2 | Big number + label + one supporting line |
| Comparison | 40-50 | 6-8 | Two columns, parallel structure |
| Quote | 30-40 | 4-6 | Quote + attribution |
| Summary | 30-40 | 5-6 | 3-5 bullets, action-oriented |
| Thank you | 2-4 | 1 | Mirror cover |

### White-Space Rules

White space is not "empty slide" — it's deliberate breathing room that signals confidence.

- **0.5" minimum margin** on all four sides (overlaps with self-audit #7; this is the design rationale).
- **0.3" minimum gap** between content blocks on the same slide.
- **At least one slide per section** is 50%+ white space. This anchors the eye.
- **Never center body text** — left-align everything except titles and callouts.
- **No two content blocks are flush** — leave at least 0.3" vertical gap between them.

### Density Anti-Patterns

- **Cramming 8 bullets onto one slide** — split into two slides OR convert to a 2x4 icon grid.
- **Filling every inch of the slide** — looks like a Word document, not a presentation.
- **Wordy labels** — "Strategic initiatives and key priorities for Q3 2026" becomes "Q3 priorities".
- **Repeating the title in the body** — the audience can read; trust them.
- **A chart with a 12-line legend** — move the legend below the chart, or use direct labels.

### Density Sanity Check

Before saving the deck, run this check on each content slide:

```python
# Count body words per slide using python-pptx
from pptx import Presentation
prs = Presentation("output.pptx")
for idx, slide in enumerate(prs.slides, start=1):
    body_words = 0
    body_lines = 0
    for shape in slide.shapes:
        if shape.has_text_frame and shape != slide.shapes.title:
            tf = shape.text_frame
            for p in tf.paragraphs:
                txt = p.text.strip()
                if txt:
                    body_lines += 1
                    body_words += len(txt.split())
    if body_words > 36 or body_lines > 6:
        print(f"Slide {idx}: {body_words} words / {body_lines} lines (OVER BUDGET)")
```

Any "OVER BUDGET" line means: cut or split. The slide is not shippable as-is.

---

## Color & Contrast

**Color is the most over-specified and under-validated part of an agent-generated deck.** The `### Color Palettes` table under *Design Ideas* shows you 10 inspiring palettes; this section gives you the rule for picking one and the math for checking it.

### The 60-30-10 Rule

Every palette you ship has exactly three roles:

- **Dominant (60-70% of visual weight)** — backgrounds, large blocks, primary fill.
- **Supporting (20-30%)** — secondary surfaces, panel backgrounds, subtle separators.
- **Accent (5-10%)** — callouts, stats, one number per slide max.

If you find yourself using a fourth color, demote one of the existing three; don't add.

### WCAG Contrast Scoring

Body text and large text have different minimum contrast ratios (WCAG 2.1 AA):

| Text size | Min ratio | Pass grade |
|---|---|---|
| Body text (< 18pt, < 14pt bold) | **4.5 : 1** | AA (good) |
| Large text (≥ 18pt or ≥ 14pt bold) | **3.0 : 1** | AA (good) |
| Body text | **7.0 : 1** | AAA (excellent) |
| Large text | **4.5 : 1** | AAA (excellent) |

**Target for a professional deck: every body-text pair ≥ 4.5:1; every large-text pair ≥ 3.0:1.** Aim for AAA on the cover and section dividers — those are the slides the audience sees longest.

### Pre-validated combinations (all pass AA for body)

| Use | Foreground | Background | Ratio | Pass |
|---|---|---|---|---|
| Body on white | `#212121` | `#FFFFFF` | 16.1 | AAA |
| Body on white | `#36454F` | `#FFFFFF` | 10.4 | AAA |
| Body on cream | `#1E2761` | `#ECE2D0` | 11.2 | AAA |
| Body on dark navy | `#F2F2F2` | `#1E2761` | 14.6 | AAA |
| Body on charcoal | `#F5F5F5` | `#36454F` | 12.1 | AAA |
| Accent on white | `#990011` | `#FFFFFF` | 9.4 | AAA |
| Subtle on cream | `#6D2E46` | `#ECE2D0` | 6.7 | AA |

**These cover the 10 palettes in the *Design Ideas* table, but with foreground/background pairings that actually pass.** If a palette from that table fails your check, do not invent a new color — pick a pre-validated combo from this list instead and tell the user the substitution.

### Combinations to NEVER ship

These look pretty but fail AA — do not use them in body text:

- Light gray (`#9CA3AF` or lighter) on white or cream
- Yellow text on white or any light background
- Pastel pink on white (`#F8B4B4` on `#FFFFFF` is 1.7:1)
- Cyan text on dark backgrounds without enough weight (light cyan + dark navy can be OK if the cyan is dark enough; light cyan does not work)
- Body text in a gradient fill

### Contrast sanity check

Before emitting the deck, score every text/background pair. Use this snippet in the sandbox:

```python
def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def relative_luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def contrast_ratio(fg_hex, bg_hex):
    L1 = relative_luminance(hex_to_rgb(fg_hex))
    L2 = relative_luminance(hex_to_rgb(bg_hex))
    lighter, darker = max(L1, L2), min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)

# Example: dark navy body on cream
print(f"{contrast_ratio('#1E2761', '#ECE2D0'):.1f}:1")  # 11.2:1 — AAA pass
```

If any foreground/background pair in your deck scores below 4.5:1 for body text, swap the foreground (or background — but only one) for a pre-validated combo above.

### Color-blind safety

A useful quick check: run your deck through a color-blindness simulator (https://www.color-blindness.com/coblis-color-blindness-simulator/) and verify that:

- **Dominant vs supporting colors** are still distinguishable in deuteranopia and protanopia (red-green).
- **Accent color** doesn't disappear in tritanopia (blue-yellow).

The pre-validated combos in this section are all chosen to survive the three common forms of color blindness. If you customize a palette, run the simulator — color is the most common accessibility failure in agent-generated decks.

---

## Chart Quality

**Charts are the second most common failure mode after text density.** Two rules govern all chart creation:

### 1. No fabricated data

**Only create charts for slides backed by real quantitative data from the source content.** Do NOT fabricate, estimate, or invent data to fill charts. If the source content is qualitative (narrative, opinions, strategy), use text-based layouts — cards, tables, diagrams — instead.

### 2. Categorical vs Semantic colors

- **Categorical palette (CAT)** — for data series where color is just a distinguisher (10 Tableau-inspired colors)
- **Semantic colors (SEM)** — only when color carries meaning (green = good, red = bad, amber = warning)

**Rule of thumb**: If you could swap two colors without losing information, use CAT. If swapping would confuse meaning, use semantic.

For full chart styling defaults, color constants, critical pitfalls, and code examples for each chart type, see [references/chart-patterns.md](references/chart-patterns.md).

---

## Typography Hierarchy

**The single biggest reason a slide looks "designed" vs. "amateur" is the type hierarchy.** This section gives the four-level sizing system, weight rules, and paragraph rhythm that professional decks use.

### The 4-Level Type System

Every slide uses AT MOST four type levels. More than four and the hierarchy collapses; fewer and the slide looks like an essay.

| Level | Use | Size (16:9 13.33"×7.5") | Weight | Letter-spacing | Color |
|---|---|---|---|---|---|
| **H1 — Slide title** | The takeaway of the slide | 36-44pt | Bold (700) | -0.5 to -1.0 (tight) | Dominant or supporting, full strength |
| **H2 — Section header** | Sub-section label on a dense slide | 20-24pt | Bold (600) | 0 (default) | Supporting, full strength |
| **H3 — Label / callout** | Small stat, KPI tag, icon label | 14-16pt | Semi-bold (600) | +0.5 to +1.0 (slight open) | Accent, full strength |
| **Body** | Paragraph text, bullets | 14-16pt | Regular (400) | 0 (default) | Dominant on supporting background |
| **Caption** | Source line, footer, page number | 10-12pt | Regular (400) | 0 (default) | Supporting at 60% opacity |

**One deck, one pair.** Pick a header font and a body font from the *Typography* table in *Design Ideas* and use them across the whole deck. Headers gain hierarchy from size and weight, not from changing family.

### Hierarchy Rules

- **Size contrast is the loudest hierarchy signal.** H1 at 44pt vs body at 14pt is 3.1× — that's the minimum spread. Less than 2× and the audience can't tell what to read first.
- **Weight contrast is the second signal.** H1 = Bold, body = Regular. Mixing two Regulars at different sizes looks like an error.
- **Color contrast is the third signal.** A header in the dominant color, body in a slightly muted shade of the same color, makes the slide cohesive without changing the palette.
- **Never use more than 2 weights per slide.** If you need a third, you have a hierarchy problem, not a styling problem.
- **All-caps is for short labels only** (3-5 words, like "Q3 RESULTS"). Long all-caps text is unreadable and aggressive.

### Paragraph Rhythm

The audience doesn't read slides — they scan them. Rhythm is what makes a slide scannable.

- **Line spacing 1.2 to 1.4** for body text. Tighter (1.0) looks like a Word document; looser (1.5+) wastes space.
- **Paragraph spacing 6-12pt** between paragraphs (not 0 — that creates a wall).
- **Bullet spacing 4-8pt** between bullets, not the default 0.
- **Sentence case for titles, ALL CAPS only for short labels.** Title Case In Every Title Is A Style Of The 1990s.
- **Left-align body text.** Center-aligned body is the second-biggest AI-slides tell (after the accent line). Only center titles, callouts, and quotes.

### Typography Sanity Check

Before emitting, confirm the deck uses 2 fonts max and 4 sizes max:

```python
from pptx import Presentation
from pptx.util import Pt
from collections import Counter

prs = Presentation("output.pptx")
fonts = Counter()
sizes = Counter()
for slide in prs.slides:
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if r.font.name:
                    fonts[r.font.name] += 1
                if r.font.size:
                    sizes[r.font.size.pt] += 1

print(f"Fonts used ({len(fonts)}): {dict(fonts)}")
print(f"Sizes used ({len(sizes)}): {dict(sizes)}")
assert len(fonts) <= 2, f"Too many fonts: {len(fonts)}"
assert len(sizes) <= 4, f"Too many sizes: {len(sizes)}"
```

If the script reports more than 2 fonts or more than 4 sizes, the deck is using typography as decoration, not hierarchy. Pick the most-used font and size for body, the second-most for headers, and delete the rest.

### Typography Anti-Patterns

- **Two fonts in the same word** (e.g., a serif header on a sans-serif body in the same sentence).
- **Bolding a single word inside a body paragraph** to fake a header — that's what H2 is for.
- **Different header sizes on every slide** ("creative" sizing) — looks like a ransom note.
- **Italics for emphasis across an entire bullet** — italics are for citations and quotes, not for emphasis.
- **Underlines on anything other than hyperlinks** — underlines on body text scream "1998 Word doc."

---

## Accessibility Quick Pass

**Accessibility is not optional — it's a baseline.** A deck that fails the quick pass below will lose ~8% of viewers immediately (low-vision, color-blind, projector wash-out, screen-reader-only). Each rule is a single line; together they take under a minute to apply.

### The 5-Minute Quick Pass

Run these five checks on the finished file. All five must pass before you emit the marker.

1. **Alt text on every image.**
   Every picture, logo, chart, and icon gets a non-empty `descr` attribute or `alt` text in the XML. In python-pptx, set it on the shape's `_element.nvPicPr.cNvPr` (see code below). Decorative-only images get `alt=""` explicitly.
2. **Body font ≥ 14pt.**
   Open the file and check the slide master. Body text smaller than 14pt is unreadable on a projector at the back of a conference room. Use the 4-level system in *Typography Hierarchy* — the body level is 14-16pt, the caption is 10-12pt and only for sources/footers.
3. **Color is not the only signal.**
   Every chart that uses color to differentiate must ALSO use shape, position, or label. Color-blind viewers (8% of men, 0.5% of women) cannot tell a red line from a green line if that's the only cue. The pre-validated palettes in *Color & Contrast* survive this; verify any custom palette does too.
4. **No reliance on color alone for status.**
   "Green = good, red = bad" is a fail by itself. Add a checkmark / X icon, or a "✓" / "✗" glyph, or the words "pass" / "fail" alongside the color.
5. **Reading order is logical.**
   Screen readers read top-to-bottom, left-to-right, in the order shapes were added (not the visual order). Open the file in LibreOffice and use Tab to step through; the focus order must match the visual order. If it doesn't, re-order the shapes in the slide (right-click → "Send to Back" / "Bring to Front" / arrange).

### Color-blind-safe palette tags

When you pick a palette from *Design Ideas*, the pre-validated combos in *Color & Contrast* are all safe. If you must use a custom palette, tag it as one of:

- **Mono** — one hue, varying lightness. Always safe. Examples: charcoal scale, navy scale.
- **Diverging** — two hues from opposite sides of the color wheel, meeting at a neutral midpoint. Safe IF lightness differs between the two sides. Example: red-to-blue diverging.
- **Sequential** — one hue, light to dark. Safe. Example: light blue to dark blue.
- **Categorical** — distinct hues for distinct categories. NOT automatically safe — verify with a color-blindness simulator.

The 10 palettes in *Design Ideas* are mostly Categorical. The pre-validated combos in *Color & Contrast* filter them to ones that survive deuteranopia and protanopia.

### Alt-text patterns

Use these patterns for common image types:

| Image type | Alt text pattern | Example |
|---|---|---|
| Logo | "Company X logo" | "Zhanlu logo" |
| Decorative | `""` (empty) | n/a |
| Chart | "Chart: <type>, <axes>, <key finding>" | "Chart: bar, revenue by quarter, Q3 was highest at 12.4M" |
| Icon | "Icon: <what it represents>" | "Icon: rocket, representing growth" |
| Screenshot | "Screenshot of <app> showing <feature>" | "Screenshot of dashboard showing monthly revenue" |
| Photo | "Photo: <subject>, <context>" | "Photo: team in office, casual attire" |
| Diagram | "Diagram: <structure>" | "Diagram: 3-tier architecture with API gateway" |

**Never** leave alt text empty for an informative image. **Always** set `alt=""` for a decorative image so screen readers skip it.

### Programmatically setting alt text

In python-pptx:

```python
from pptx import Presentation
from lxml import etree

prs = Presentation("output.pptx")
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.shape_type == 13:  # PICTURE
            # Find the descr attribute on cNvPr
            cNvPr = shape._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/picture}cNvPr")
            if cNvPr is not None:
                cNvPr.set("descr", "Your alt text here")
prs.save("output.pptx")
```

**Sanity check** before emitting:

```bash
python3 -c "
import zipfile, re
z = zipfile.ZipFile('output.pptx')
for name in z.namelist():
    if name.startswith('ppt/slides/slide') and name.endswith('.xml'):
        xml = z.read(name).decode()
        pics = re.findall(r'<p:pic[^>]*>.*?</p:pic>', xml, re.DOTALL)
        for p in pics:
            if 'descr' not in p:
                print(f'{name}: picture with no alt text!')
"
```

If anything prints, fix the alt text and re-emit.

### Accessibility floor

A deck passes the accessibility quick pass when:

- All 5 quick-pass checks above pass.
- The file validates with `libreoffice --headless --convert-to pdf output.pptx` without warnings about missing alt text or color contrast.
- A screen reader (NVDA on Windows, VoiceOver on Mac, Orca on Linux) can read the slide titles in order, and the slide titles alone communicate the deck's intent.

A deck that fails any of these is not shippable for a public audience.

---

## Pre-Emit Self-Audit (12 points)

**Run this checklist on the finished file BEFORE emitting the ◤PPTX◤ marker.** Every box must be ✓. If any item is ✗, fix and re-verify. Skipping this is the #1 reason agent decks look bad on the first pass.

For the long-form rubric (with examples, anti-patterns, scoring), see [references/quality-checklist.md](references/quality-checklist.md).

### Content
1. **No placeholder text** — `grep -iE "xxxx|lorem|ipsum|todo|tbd|click to add"` returns nothing.
2. **One idea per slide** — every slide can be summarized in ≤ 6 words; if not, split it.
3. **Headline not body** — the slide title is a takeaway, not a topic label ("Revenue grew 18%" not "Revenue").
4. **Source citations on data slides** — every chart / number cites the source and date inline (small text, bottom-corner).

### Visual
5. **No accent line under titles** — those are an AI-slides tell; use whitespace or a background color block instead.
6. **No off-canvas or cropped elements** — every shape, text box, image sits fully inside the 13.33" × 7.5" 16:9 frame.
7. **≥ 0.5" margin on all sides** — no element starts within half an inch of the slide edge.
8. **Contrast is readable** — body text ≥ 4.5:1 against its background; large text ≥ 3:1. Run the rubric in the long-form reference.

### Polish
9. **Consistent fonts across slides** — pick a header + body pair in step 5 of the decision tree; never mix.
10. **Consistent palette across slides** — the same 3-4 colors repeat; one dominates, 1-2 support, one accents.
11. **No two consecutive slides use the same layout** — vary columns, callouts, grids, half-bleeds.
12. **Has a summary / next-steps slide** — decks that stop at data feel unfinished.

**If any check fails, do not emit. Fix and re-verify.** Then convert the deck to images (see *Converting to Images* below) and visually inspect with a subagent.

---

## QA (Required)

**Assume there are problems. Your job is to find them.**

Your first render is almost never correct. Approach QA as a bug hunt, not a confirmation step. If you found zero issues on first inspection, you weren't looking hard enough.

### Content QA

```bash
python -m markitdown output.pptx
```

Check for missing content, typos, wrong order.

**When using templates, check for leftover placeholder text:**

```bash
python -m markitdown output.pptx | grep -iE "xxxx|lorem|ipsum|this.*(page|slide).*layout"
```

If grep returns results, fix them before declaring success.

### Visual QA

**⚠️ USE SUBAGENTS** — even for 2-3 slides. You've been staring at the code and will see what you expect, not what's there. Subagents have fresh eyes.

Convert slides to images (see [Converting to Images](#converting-to-images)), then use this prompt:

```
Visually inspect these slides. Assume there are issues — find them.

Look for:
- Overlapping elements (text through shapes, lines through words, stacked elements)
- Text overflow or cut off at edges/box boundaries
- Decorative lines positioned for single-line text but title wrapped to two lines
- Source citations or footers colliding with content above
- Elements too close (< 0.3" gaps) or cards/sections nearly touching
- Uneven gaps (large empty area in one place, cramped in another)
- Insufficient margin from slide edges (< 0.5")
- Columns or similar elements not aligned consistently
- Low-contrast text (e.g., light gray text on cream-colored background)
- Low-contrast icons (e.g., dark icons on dark backgrounds without a contrasting circle)
- Text boxes too narrow causing excessive wrapping
- Leftover placeholder content

For each slide, list issues or areas of concern, even if minor.

Read and analyze these images:
1. /path/to/slide-01.jpg (Expected: [brief description])
2. /path/to/slide-02.jpg (Expected: [brief description])

Report ALL issues found, including minor ones.
```

### Verification Loop

1. Generate slides → Convert to images → Inspect
2. **List issues found** (if none found, look again more critically)
3. Fix issues
4. **Re-verify affected slides** — one fix often creates another problem
5. Repeat until a full pass reveals no new issues

**Do not declare success until you've completed at least one fix-and-verify cycle.**

---

## Anti-Patterns Gallery

**Eight patterns that are the most common reasons an agent-generated deck looks bad on the first pass.** Each pattern shows the failure mode in one line, the symptom, and a one-sentence fix. Use this gallery as a pre-emit sanity check after running the 12-point self-audit.

### 1. Accent line under every title

- **Symptom:** a thin horizontal line spans the slide width just below the title.
- **Why it's bad:** it's the single biggest AI-slides tell. Default templates add it; designers don't.
- **Fix:** delete the line shape. Use whitespace or a colored title background block instead.

### 2. Wall of bullets

- **Symptom:** 5-7 bullets on a slide, each 2-3 lines long, plain text, no images or icons.
- **Why it's bad:** the audience will read slide 1, then stop.
- **Fix:** split into multiple slides OR convert each bullet into a labeled icon row.

### 3. Off-canvas image

- **Symptom:** an image extends past the slide edge, or a logo is clipped on the corner.
- **Why it's bad:** looks broken; viewers assume the deck was rushed.
- **Fix:** resize the image to ≤ 90% slide width and recenter.

### 4. Leftover placeholder text

- **Symptom:** `Click to add text`, `Lorem ipsum`, `Your title here`, `Sample text`.
- **Why it's bad:** it means the agent didn't read its own output. Whole deck loses credibility.
- **Fix:** grep for it (`python -m markitdown output.pptx | grep -iE "xxxx|lorem|todo|click to add"`); replace or remove the affected slide.

### 5. Same layout 3 slides in a row

- **Symptom:** slides 4, 5, 6 are all "title + 4 bullets" in the same font and column structure.
- **Why it's bad:** visual monotony = the audience checks out.
- **Fix:** vary layouts — two-column, icon row, data callout, comparison, quote — never two adjacent slides the same.

### 6. Inconsistent fonts

- **Symptom:** slide 1 in Georgia, slide 3 in Calibri, slide 5 in Arial.
- **Why it's bad:** mixed fonts = no design system = no trust.
- **Fix:** set the body font in the slide master; delete per-slide overrides.

### 7. Inconsistent palette

- **Symptom:** each slide uses a different color from a different palette.
- **Why it's bad:** the deck looks like a mood board, not a narrative.
- **Fix:** pick a 3-color palette (dominant + support + accent) and apply it via the slide master.

### 8. No summary / next-steps slide

- **Symptom:** the deck ends with a content slide. No synthesis, no close.
- **Why it's bad:** leaves the audience to do the work. They won't.
- **Fix:** add a Summary slide with 3-5 bullets that mirror the one-sentence intent from the *Pre-Generation Decision Tree*. End with a Thank-you / Questions slide that mirrors the cover.

---

## Converting to Images

Convert presentations to individual slide images for visual inspection:

```bash
python scripts/office/soffice.py --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

This creates `slide-01.jpg`, `slide-02.jpg`, etc.

To re-render specific slides after fixes:

```bash
pdftoppm -jpeg -r 150 -f N -l N output.pdf slide-fixed
```

---

## Dependencies

- `pip install "markitdown[pptx]"` - text extraction
- `pip install Pillow` - thumbnail grids
- `npm install -g pptxgenjs` - creating from scratch
- LibreOffice (`soffice`) - PDF conversion (auto-configured for sandboxed environments via `scripts/office/soffice.py`)
- Poppler (`pdftoppm`) - PDF to images


## Slide-type Conventions

Every deck uses a small set of repeatable slide types. Pick the right type per slide — don't improvise a new layout for each one.

**For detailed layouts, font hierarchies, content checklists, and per-slide workflows for each type, see [references/slide-types.md](references/slide-types.md).**

| Type | When to use | Layout |
|---|---|---|
| **Cover** | Slide 1 only | Dark bg, large title (44-56pt), subtitle (18-24pt), optional logo/image. No body text. No page badge. |
| **Section divider** | Between major sections | Dark or accent bg, section number + name, optional one-line takeaway. Page badge required. |
| **TOC** | After cover, if deck >8 slides | Numbered list of sections, right-aligned page refs optional. Page badge required. |
| **Content** | The bulk of the deck | Title + body, following the layout options in *Design Ideas* (two-column, icon rows, 2x2 grid, half-bleed). Page badge required. |
| **Data callout** | Big number / KPI slide | One giant stat (60-72pt) + small label, plus a supporting sentence or chart. Page badge required. |
| **Comparison** | Before/after, pros/cons, options A/B/C | Two or three columns of equal width, headers aligned, parallel structure. Page badge required. |
| **Quote** | Customer or exec quote | Large italic quote, attribution below, optional photo. Page badge required. |
| **Summary / Next steps** | Final content slide | 3-5 bullets, action-oriented, owner + date if applicable. Page badge required. |
| **Thank you / Contact** | Last slide | Mirrors cover style, simple. Page badge required. |

Rules:
- A deck usually has 1 cover, 0-1 TOC, 1+ section dividers, N content slides, 1 summary, 1 thank-you.
- Don't skip the summary slide — decks without one feel unfinished.
- Don't use a "Content" layout for the cover or summary.
- **Page number badge required** on all slides except Cover (see [pptxgenjs.md → Page Number Badge](pptxgenjs.md#page-number-badge)).


### Emitting the PPTX marker (headless / managed-agent flow)

When running in a headless environment (no live Office), after producing the slide-spec JSON file, emit at the END of your reply:

```
◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤
```

The platform will detect the marker and call `create_artifact` with `type="pptx"`, rendering the file via the sandbox pipeline and surfacing an inline preview card. The marker is stripped from the visible reply.

Rules:
- `slides_path` is relative to your workspace root and points to the slide-spec JSON (see "Creating from Scratch").
- `filename` must end in `.pptx`.
- Emit the marker exactly once per deck, at the very end of your reply.

---

## Related Skills

- **`docx`** — for the parallel Word-document track. Share the [Professional Output Principles](#professional-output-principles) below; the docx skill has the same content.
- **`theme-system`** — provides 10 WCAG-validated JSON theme tokens with light/dark modes. Use it as the source of truth for palettes when available. See [Theme Object Contract](#theme-object-contract) below for the bridge between theme-system and pptxgenjs.

## Theme Object Contract

When creating slides programmatically with PptxGenJS, the `compile.js` script passes a `theme` object to each slide's `createSlide(pres, theme)` function. This object MUST use these **exact keys**:

| Key | Purpose | Example |
|-----|---------|---------|
| `theme.primary` | Darkest color, titles, dominant backgrounds | `"22223B"` |
| `theme.secondary` | Dark accent, body text, secondary surfaces | `"4A4E69"` |
| `theme.accent` | Mid-tone accent, highlights, badges | `"9A8C98"` |
| `theme.light` | Light accent, card backgrounds | `"C9ADA7"` |
| `theme.bg` | Background color | `"F2E9E4"` |

**NEVER use other key names** like `background`, `text`, `muted`, `darkest`, `lightest`.

### Bridge to `theme-system` skill

The `theme-system` skill provides structured JSON theme tokens with more semantic keys (`primary`, `secondary`, `text`, `background`, `surface`, etc.). When using a theme from `theme-system`, map it to the pptx contract:

```javascript
// Map theme-system tokens → pptx theme contract
const pptxTheme = {
  primary:   tsTheme.light.primary,      // darkest, for titles
  secondary: tsTheme.light.text,         // body text color
  accent:    tsTheme.light.secondary,    // highlight color
  light:     tsTheme.light.surface,      // card backgrounds
  bg:        tsTheme.light.background,   // slide background
};
```

For full modular slide architecture with `compile.js` and per-slide JS files, see [pptxgenjs.md → Modular Slide Architecture](pptxgenjs.md#modular-slide-architecture).

## Professional Output Principles

These principles apply to BOTH the `pptx` and `docx` skills. They are the shared design floor that every artifact must clear, regardless of format.

1. **Know your audience before writing.** The decision tree at the top of this file is not optional — it is the first thing you read.
2. **One idea per slide / one idea per section.** A 6-word summary is the test for "one idea." If the slide/section needs more than 6 words to describe, split it.
3. **Headline not topic.** Titles are sentences, not labels. "Revenue grew 18%" not "Revenue".
4. **Source every data point.** A number without a source is unfalsifiable. Cite inline, not in speaker notes.
5. **Stay within the type's template.** A memo with a TOC is wrong. A pitch deck labeled as a report is wrong. Use the Choosing a Document Type section in `docx`, and the slide-type conventions in this file.
6. **Pre-emit audit, then emit.** Both skills have a numbered audit (12 for pptx, 10 for docx). Run the audit; only emit if every box is ✓.
7. **Long-form rubric is the deep-dive.** Each skill has a `references/quality-checklist.md` with failure modes and copy-paste fixes. Read it the first time you generate in a new domain; skip it once the audit consistently passes.
8. **No new external lookups.** The 18 palettes in *Color Palettes*, the 5 slide types in [slide-types.md](references/slide-types.md), the 4 style recipes in [style-recipes.md](references/style-recipes.md), the chart patterns in [chart-patterns.md](references/chart-patterns.md), the 5 doc types, the 4-level heading system — these are opinionated defaults. Default to them; deviate only with a documented reason.
