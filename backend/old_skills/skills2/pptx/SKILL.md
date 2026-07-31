---
name: pptx
description: Create, read, and edit PowerPoint .pptx presentations — executive decks, quarterly reviews, board presentations, and business proposals. Use for: Zhanlu executive briefings, stakeholder reports, project proposals.
category: productivity
---


     6|
     7|# PPTX Skill
     8|
     9|## Quick Reference
    10|
    11|| Task | Guide |
    12||------|-------|
    13|| Read/analyze content | `python -m markitdown presentation.pptx` |
    14|| Edit or create from template | Read [editing.md](editing.md) |
    15|| Create from scratch | Read [pptxgenjs.md](pptxgenjs.md) |
    16|
    17|---
    18|
    19|## Reading Content
    20|
    21|```bash
    22|# Text extraction
    23|python -m markitdown presentation.pptx
    24|
    25|# Visual overview
    26|python scripts/thumbnail.py presentation.pptx
    27|
    28|# Raw XML
    29|python scripts/office/unpack.py presentation.pptx unpacked/
    30|```
    31|
    32|---
    33|
    34|## Editing Workflow
    35|
    36|**Read [editing.md](editing.md) for full details.**
    37|
    38|1. Analyze template with `thumbnail.py`
    39|2. Unpack → manipulate slides → edit content → clean → pack
    40|
    41|---
    42|
    43|## Creating from Scratch
    44|
    45|**Read [pptxgenjs.md](pptxgenjs.md) for full details.**
    46|
    47|Use when no template or reference presentation is available.
    48|
    49|---
    50|
    51|## Design Ideas
    52|
    53|**Don't create boring slides.** Plain bullets on a white background won't impress anyone. Consider ideas from this list for each slide.
    54|
    55|### Before Starting
    56|
    57|- **Pick a bold, content-informed color palette**: The palette should feel designed for THIS topic. If swapping your colors into a completely different presentation would still "work," you haven't made specific enough choices.
    58|- **Dominance over equality**: One color should dominate (60-70% visual weight), with 1-2 supporting tones and one sharp accent. Never give all colors equal weight.
    59|- **Dark/light contrast**: Dark backgrounds for title + conclusion slides, light for content ("sandwich" structure). Or commit to dark throughout for a premium feel.
    60|- **Commit to a visual motif**: Pick ONE distinctive element and repeat it — rounded image frames, icons in colored circles, thick single-side borders. Carry it across every slide.
    61|
    62|### Color Palettes
    63|
    64|Choose colors that match your topic — don't default to generic blue. Use these palettes as inspiration:
    65|
    66|| Theme | Primary | Secondary | Accent |
    67||-------|---------|-----------|--------|
    68|| **Midnight Executive** | `1E2761` (navy) | `CADCFC` (ice blue) | `FFFFFF` (white) |
    69|| **Forest & Moss** | `2C5F2D` (forest) | `97BC62` (moss) | `F5F5F5` (cream) |
    70|| **Coral Energy** | `F96167` (coral) | `F9E795` (gold) | `2F3C7E` (navy) |
    71|| **Warm Terracotta** | `B85042` (terracotta) | `E7E8D1` (sand) | `A7BEAE` (sage) |
    72|| **Ocean Gradient** | `065A82` (deep blue) | `1C7293` (teal) | `21295C` (midnight) |
    73|| **Charcoal Minimal** | `36454F` (charcoal) | `F2F2F2` (off-white) | `212121` (black) |
    74|| **Teal Trust** | `028090` (teal) | `00A896` (seafoam) | `02C39A` (mint) |
    75|| **Berry & Cream** | `6D2E46` (berry) | `A26769` (dusty rose) | `ECE2D0` (cream) |
    76|| **Sage Calm** | `84B59F` (sage) | `69A297` (eucalyptus) | `50808E` (slate) |
    77|| **Cherry Bold** | `990011` (cherry) | `FCF6F5` (off-white) | `2F3C7E` (navy) |
    78|
    79|### For Each Slide
    80|
    81|**Every slide needs a visual element** — image, chart, icon, or shape. Text-only slides are forgettable.
    82|
    83|**Layout options:**
    84|- Two-column (text left, illustration on right)
    85|- Icon + text rows (icon in colored circle, bold header, description below)
    86|- 2x2 or 2x3 grid (image on one side, grid of content blocks on other)
    87|- Half-bleed image (full left or right side) with content overlay
    88|
    89|**Data display:**
    90|- Large stat callouts (big numbers 60-72pt with small labels below)
    91|- Comparison columns (before/after, pros/cons, side-by-side options)
    92|- Timeline or process flow (numbered steps, arrows)
    93|
    94|**Visual polish:**
    95|- Icons in small colored circles next to section headers
    96|- Italic accent text for key stats or taglines
    97|
    98|### Typography
    99|
   100|**Choose an interesting font pairing** — don't default to Arial. Pick a header font with personality and pair it with a clean body font.
   101|
   102|| Header Font | Body Font |
   103||-------------|-----------|
   104|| Georgia | Calibri |
   105|| Arial Black | Arial |
   106|| Calibri | Calibri Light |
   107|| Cambria | Calibri |
   108|| Trebuchet MS | Calibri |
   109|| Impact | Arial |
   110|| Palatino | Garamond |
   111|| Consolas | Calibri |
   112|
   113|| Element | Size |
   114||---------|------|
   115|| Slide title | 36-44pt bold |
   116|| Section header | 20-24pt bold |
   117|| Body text | 14-16pt |
   118|| Captions | 10-12pt muted |
   119|
   120|### Spacing
   121|
   122|- 0.5" minimum margins
   123|- 0.3-0.5" between content blocks
   124|- Leave breathing room—don't fill every inch
   125|
   126|### Avoid (Common Mistakes)
   127|
   128|- **Don't repeat the same layout** — vary columns, cards, and callouts across slides
   129|- **Don't center body text** — left-align paragraphs and lists; center only titles
   130|- **Don't skimp on size contrast** — titles need 36pt+ to stand out from 14-16pt body
   131|- **Don't default to blue** — pick colors that reflect the specific topic
   132|- **Don't mix spacing randomly** — choose 0.3" or 0.5" gaps and use consistently
   133|- **Don't style one slide and leave the rest plain** — commit fully or keep it simple throughout
   134|- **Don't create text-only slides** — add images, icons, charts, or visual elements; avoid plain title + bullets
   135|- **Don't forget text box padding** — when aligning lines or shapes with text edges, set `margin: 0` on the text box or offset the shape to account for padding
   136|- **Don't use low-contrast elements** — icons AND text need strong contrast against the background; avoid light text on light backgrounds or dark text on dark backgrounds
   137|- **NEVER use accent lines under titles** — these are a hallmark of AI-generated slides; use whitespace or background color instead
   138|
   139|---
   140|
   141|## QA (Required)
   142|
   143|**Assume there are problems. Your job is to find them.**
   144|
   145|Your first render is almost never correct. Approach QA as a bug hunt, not a confirmation step. If you found zero issues on first inspection, you weren't looking hard enough.
   146|
   147|### Content QA
   148|
   149|```bash
   150|python -m markitdown output.pptx
   151|```
   152|
   153|Check for missing content, typos, wrong order.
   154|
   155|**When using templates, check for leftover placeholder text:**
   156|
   157|```bash
   158|python -m markitdown output.pptx | grep -iE "xxxx|lorem|ipsum|this.*(page|slide).*layout"
   159|```
   160|
   161|If grep returns results, fix them before declaring success.
   162|
   163|### Visual QA
   164|
   165|**⚠️ USE SUBAGENTS** — even for 2-3 slides. You've been staring at the code and will see what you expect, not what's there. Subagents have fresh eyes.
   166|
   167|Convert slides to images (see [Converting to Images](#converting-to-images)), then use this prompt:
   168|
   169|```
   170|Visually inspect these slides. Assume there are issues — find them.
   171|
   172|Look for:
   173|- Overlapping elements (text through shapes, lines through words, stacked elements)
   174|- Text overflow or cut off at edges/box boundaries
   175|- Decorative lines positioned for single-line text but title wrapped to two lines
   176|- Source citations or footers colliding with content above
   177|- Elements too close (< 0.3" gaps) or cards/sections nearly touching
   178|- Uneven gaps (large empty area in one place, cramped in another)
   179|- Insufficient margin from slide edges (< 0.5")
   180|- Columns or similar elements not aligned consistently
   181|- Low-contrast text (e.g., light gray text on cream-colored background)
   182|- Low-contrast icons (e.g., dark icons on dark backgrounds without a contrasting circle)
   183|- Text boxes too narrow causing excessive wrapping
   184|- Leftover placeholder content
   185|
   186|For each slide, list issues or areas of concern, even if minor.
   187|
   188|Read and analyze these images:
   189|1. /path/to/slide-01.jpg (Expected: [brief description])
   190|2. /path/to/slide-02.jpg (Expected: [brief description])
   191|
   192|Report ALL issues found, including minor ones.
   193|```
   194|
   195|### Verification Loop
   196|
   197|1. Generate slides → Convert to images → Inspect
   198|2. **List issues found** (if none found, look again more critically)
   199|3. Fix issues
   200|4. **Re-verify affected slides** — one fix often creates another problem
   201|5. Repeat until a full pass reveals no new issues
   202|
   203|**Do not declare success until you've completed at least one fix-and-verify cycle.**
   204|
   205|---
   206|
   207|## Converting to Images
   208|
   209|Convert presentations to individual slide images for visual inspection:
   210|
   211|```bash
   212|python scripts/office/soffice.py --headless --convert-to pdf output.pptx
   213|pdftoppm -jpeg -r 150 output.pdf slide
   214|```
   215|
   216|This creates `slide-01.jpg`, `slide-02.jpg`, etc.
   217|
   218|To re-render specific slides after fixes:
   219|
   220|```bash
   221|pdftoppm -jpeg -r 150 -f N -l N output.pdf slide-fixed
   222|```
   223|
   224|---
   225|
   226|## Dependencies
   227|
   228|- `pip install "markitdown[pptx]"` - text extraction
   229|- `pip install Pillow` - thumbnail grids
   230|- `npm install -g pptxgenjs` - creating from scratch
   231|- LibreOffice (`soffice`) - PDF conversion (auto-configured for sandboxed environments via `scripts/office/soffice.py`)
   232|- Poppler (`pdftoppm`) - PDF to images
   233|