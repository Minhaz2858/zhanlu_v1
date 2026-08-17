# PHASE 1 — LibreOffice Pre-flight (2026-08-17)

> **Status: PASSED** — `libreoffice --headless` works in the sandbox container. PHASE 1 implementation may proceed.

## What was verified

| Check | Result |
|---|---|
| `libreoffice` binary present | `/usr/bin/libreoffice` (also `/usr/bin/soffice`) |
| Version | `LibreOffice 25.2.3.2 520(Build:2)` |
| Container image | `zhanlu-sandbox-pptx:latest` (1.25 GB) |
| Image CMD | `["python", "/skill/run.py"]` |
| Image WORKDIR | `/output` |
| Image created | 2026-07-30 |
| `--convert-to pdf` (pptx → pdf) | Works (single PDF emitted) |
| Per-slide PNG extraction | Works via **PyMuPDF (`fitz`) 1.28.0** (pre-installed in sandbox) |
| `pdftoppm` / `pdftocairo` / ImageMagick | NOT installed — use PyMuPDF instead |
| `python-pptx` | 1.0.2 (pre-installed in sandbox) |
| `matplotlib` | 3.11.1 (pre-installed in sandbox) |
| `Pillow` | 12.3.0 (pre-installed in sandbox) |
| `numpy` | 2.4.6 (pre-installed in sandbox) |
| Java warning (javaldx) | Benign — does not affect conversion |

## Smoke test

Ran end-to-end inside `zhanlu-sandbox-pptx:latest`:

1. Generate a 4-slide .pptx via `python-pptx` (no title placeholder, textboxes).
2. `libreoffice --headless --convert-to pdf multi.pptx --outdir /tmp/pf3/` → 17.7 KB PDF.
3. `fitz.open(pdf)` → 4 pages.
4. `page.get_pixmap(dpi=150).tobytes("png")` → 4 PNGs, 15-16 KB each.

Output PNGs are 16:9 thumbnails suitable for the chat-preview card.

## Path the implementation will take

Inside the sandbox, every render does:

```python
# sandbox_runner.generate_pptx()
import subprocess, fitz, tempfile, os
with tempfile.TemporaryDirectory() as td:
    pptx_path = os.path.join(td, "deck.pptx")
    open(pptx_path, "wb").write(pptx_bytes)
    subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf",
         pptx_path, "--outdir", td],
        capture_output=True, timeout=60,
    )
    pdf_path = os.path.join(td, "deck.pdf")
    doc = fitz.open(pdf_path)
    thumbnails_b64 = [
        base64.b64encode(page.get_pixmap(dpi=150).tobytes("png")).decode("ascii")
        for page in doc
    ]
return {"pptx_bytes": pptx_bytes, "thumbnails_b64": thumbnails_b64}
```

Estimated cost per render: **~1-3 s** for a 10-slide deck at 150 DPI (libreoffice ~1 s, PyMuPDF ~50 ms, base64 encode <100 ms).

## Where LibreOffice is NOT installed (and that's OK)

- **Backend host** (`zhanlu-backend`) does not have `libreoffice`. The in-process renderer (`pptx_export.render_deck`) does NOT call LibreOffice. Thumbnails are only generated in the sandbox path.
- The `zhanlu-sandbox-worker` container (used for general code execution) does NOT have `libreoffice` either. Only `zhanlu-sandbox-pptx:latest` does.

## Risk: LibreOffice version drift

If the sandbox image is rebuilt with a newer LibreOffice and PDF rendering changes, thumbnail pixel output may differ across rebuilds. Mitigation: `tests/scripts/test_render_visual_diff.py` runs against a pinned reference deck snapshot, not a "live" baseline.
