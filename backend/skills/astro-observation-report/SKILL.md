---
name: astro-observation-report
description: Use for writing gravitational wave observational results papers for compact binary coalescence events (BNS, BBH, NSBH) detected by LIGO, Virgo, or KAGRA. Covers paper architecture, first page layout, parameter estimation figures, uncertainty notations, credible interval plots, statistical conventions, page headers, and Physical Review X two-column journal style.
---

# Astro Observation Report

Write gravitational wave observational results papers for compact binary coalescence events (BNS, BBH, NSBH) detected by LIGO, Virgo, or KAGRA — Physical Review X two-column journal style.

## When to use

- Gravitational wave event papers (GW150914-style detection/observation papers)
- CBC (compact binary coalescence) event parameter estimation writeups
- PRX two-column journal formatting, first-page layout, credible interval plots

## Paper architecture

1. **Title & authors** — event name (e.g. GW230529), collaboration/author block per journal convention
2. **First page layout** — PRX style: title, authors, affiliations, abstract, PACS/date line, then the two-column body; a key figure on the first page
3. **Abstract** — detection statement (signal, significance, time), source classification, key parameters with uncertainties, astrophysical implication
4. **Introduction** — motivation (what the event probes), prior context, what this event adds
5. **Detector & data** — instruments (LIGO Hanford/Livingston, Virgo, KAGRA), observation time, data conditioning
6. **Search & detection** — matched-filter / template-bank search, false-alarm rate, significance (p-value / FAR)
7. **Parameter estimation** — waveform models (IMRPhenom, SEOBNR, etc.), prior choices, posterior sampling, results table
8. **Results** — masses (primary/secondary), spins, luminosity distance, sky localization, redshift; all with credible intervals
9. **Astrophysical interpretation** — source classification (BNS/BBH/NSBH), rates implications, multimessenger context
10. **Conclusions** — what was measured, what it means, future outlook
11. **Supplementary** — full posterior corner plots, detector characterization, injection studies

## Statistical conventions (critical)

- Use credible intervals consistently: `90% credible interval` notation like `M = 2.73^{+0.04}_{-0.01} M_sun`
- Posterior summaries: median + symmetric/equal-tailed or highest-density interval — state which
- Significance: false-alarm rate (FAR) in years, or p-value; state the detection threshold
- Uncertainties: always report the statistical (sampling) vs systematic (waveform/model) split where possible
- Corner plots: 1D marginal posteriors on diagonal, 2D contours (50%/90%) off-diagonal

## Formatting rules

- PRX two-column: 10pt, double column; page headers with short title + author
- Figures: parameter estimation corner plot, sky localization map, waveform+residual panel
- Notation: M_sun for solar mass, c for speed of light, H0 for Hubble constant
- References: numbered, journal-style, cited inline

## Pitfalls

- Never state a detection significance without its FAR/p-value and the threshold used
- Always state the waveform models and priors — results are model-dependent
- Distinguish measured parameters from derived ones (e.g. masses from redshift)
- Keep the two-column layout — the journal gate checks format, not just content
