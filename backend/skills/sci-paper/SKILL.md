---
name: sci-paper
description: Use for writing, revising, or typesetting scientific research papers for top-tier venues (CVPR, ICCV, NeurIPS, ICML, ACL, ICLR, etc.). Covers section-by-section drafting, figure design, LaTeX typesetting, narrative logic, and pre-submission polishing into camera-ready or preprint PDFs.
---

# Scientific Paper Writing

Structured guidance for composing, formatting, and presenting scientific research papers for top-tier venues.

## When to use

- Writing a research paper from scratch (CVPR/ICCV/NeurIPS/ICML/ACL/ICLR style)
- Revising an existing manuscript
- Typesetting into camera-ready or preprint PDF (LaTeX)
- Figure design and paper polishing before submission

## Workflow

1. **Frame the contribution** — one sentence: what problem, what gap, what method, what evidence. The contribution must be clear before writing a word.
2. **Outline** — standard ML/NLP/CV skeleton:
   - Abstract (150-250 words: problem, method, results, significance)
   - Introduction (motivation → gap → contribution list → results summary → paper organization)
   - Related Work (positioning, not a dump — organize by theme and contrast with YOUR method)
   - Method (notation → formulation → algorithm → complexity; each design choice justified)
   - Experiments (setup → baselines → main results → ablations → analysis → limitations)
   - Conclusion (contributions, limitations, future work)
3. **Draft section by section** — never linearly from abstract; write experiments first (they define what's true), then method, then intro, abstract last.
4. **Figure design** — every figure must carry one message; readable at 2-column width; consistent style across figures; axes labeled; no 3D pie charts.
5. **LaTeX typesetting** — use the venue template; correct \cite, \ref, cross-references; tables with booktabs; algorithms in algorithm2e/algorithmicx.
6. **Narrative pass** — check claim-evidence alignment: every claim in intro/abstract has a result behind it; every result is discussed, not just listed.
7. **Pre-submission polish** — spell-check math symbols, consistent notation, no undefined terms, acknowledgments, reproducible appendix (code/checkpoints/dataset links).

## Writing rules

- Claims must be falsifiable and matched by evidence — never overclaim ("outperforms" needs a table and significance note)
- Notation: define everything on first use; keep one symbol per concept
- Passive voice for methods, active voice for contributions ("We propose...")
- Every ablation answers a question: "what if we remove X?"

## Common mistakes

- Contribution buried in Section 4 — restate it in intro and experiments
- Results without analysis — every table row needs a sentence explaining WHY
- Figures that don't match the text numbers — always sync
- Related work as a list instead of positioning
- Missing limitations — reviewers will find them; state them honestly

## Output

LaTeX source + compiled PDF, or a Markdown manuscript for later conversion. Match the venue template exactly.
