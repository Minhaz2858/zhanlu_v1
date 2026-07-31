# PPTX Delivery Pitfalls

## Terminal stdout cap (50KB)

The terminal tool caps stdout at 50KB. When a .pptx includes embedded charts
(pie, doughnut, bar), the chart XML embeds a full Excel workbook. This often
pushes the raw .pptx over 100KB, making the base64 ≈150KB — well over the cap.

**Symptom:** `print()` or `console.log()` output is silently truncated. The
◤PPTX◤ marker appears incomplete and the file won't render.

**Check:** Always run `ls -la *.pptx` before emitting. If raw .pptx > 37KB,
you will hit the cap (37KB × 4/3 ≈ 50KB base64).

**Workaround — build a minimal PPTX with python-pptx (NO charts, minimal tables):**

The marker-file + read_file approach does NOT work for large files — read_file
truncates content at ~2-3K chars. The only reliable path is to keep the raw
.pptx under 37KB so the base64 marker fits within the 50KB terminal stdout cap.

**Strategy: use python-pptx from terminal (not execute_code sandbox).**
Build with text boxes and simple shapes only — NO embedded charts (they bloat
the XML with Excel workbooks), NO styled tables with per-cell formatting.

```python
# Minimal PPTX from terminal — fits under 37KB
python3 -c "
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()
prs.slide_width = Inches(10)
prs.slide_height = Inches(5.625)

def tb(slide, l, t, w, h, text, fs=14, b=False, c=RGBColor(0x1E,0x29,0x3B)):
    tx = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tx.text_frame.word_wrap = True
    p = tx.text_frame.paragraphs[0]
    p.text = text; p.font.size = Pt(fs); p.font.bold = b; p.font.color.rgb = c

# Build slides with tb() calls only — no add_chart, no add_table
prs.save('output.pptx')
"
```

**Key rules for minimal PPTX:**
- Use python-pptx, NOT pptxgenjs (pptgenjs tables bloat to 200KB+)
- No `slide.add_chart()` — embeds Excel workbook, guarantees >50KB
- No `slide.add_table()` with per-cell formatting — use text boxes with `\n`
- Keep under 5 slides, use blank layout (`slide_layouts[6]`)
- Check: `ls -la output.pptx` — must be <37KB raw
- Emit directly: `python3 -c "...base64 encode + print marker..."`

```bash
# Emit directly from terminal — fits under 50KB stdout cap
python3 -c "
import base64
with open('output.pptx','rb') as f:
    b64 = base64.b64encode(f.read()).decode()
print(f'\u25e4PPTX\u25e4{{\"filename\":\"Report.pptx\",\"data\":\"{b64}\"}}\u25e4END_PPTX\u25e4')
"
```

## execute_code vs terminal — different Python environments

The `execute_code` tool runs in an isolated sandbox with a limited Python
environment. The `terminal` tool uses the system Python (or active venv).

If execute_code reports `ModuleNotFoundError: No module named 'openpyxl'`
but `pip install openpyxl` succeeds in terminal, use the terminal for Python
scripts that need installed packages.

## PPTX without charts first

For small files that fit under the cap, build the PPTX without charts first
and emit directly. Charts are the main contributor to file bloat.
