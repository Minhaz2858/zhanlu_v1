# PPTX Quality Checklist (long form)

This is the long-form version of the **12-point pre-emit self-audit** in `SKILL.md`. The short form is a checklist; this file explains each point with examples, scoring, and what "good" looks like.

**Use it for:** the first time you generate a deck in a new domain, or when a self-audit item keeps failing and you need a deeper rubric.

**Skip it for:** routine decks where the 12-point check has consistently passed. The short form is enough.

---

## How to use this file

1. Run the 12-point audit from `SKILL.md` first.
2. For any item you can't pass confidently, scroll to the matching section below.
3. Each section gives: the failure mode, why it matters, the "good" target, and a copy-paste fix.

If you fail an item 2+ times in a row, stop and re-read the relevant *Design Ideas* subsection in `SKILL.md` — the issue is upstream of the audit, not the audit itself.

---

## 1. No placeholder text

**Failure mode:** `xxxx`, `Lorem ipsum`, `Click to add text`, `TODO`, `TBD`, `Sample text`, `Your title here`, `<insert name>`.

**Why it matters:** these are scaffolded by the tool or the template, not by you. If a user opens the deck and sees one, the whole deck loses credibility — every other claim is now suspect.

**Target:** zero matches. Run:

```bash
python -m markitdown output.pptx | grep -iE "xxxx|lorem|ipsum|click to add|your.*title|sample text|todo|tbd|insert.*name"
```

Exit code must be 1 (no matches).

**Fix:** open the deck in the editor (LibreOffice Impress) and replace each placeholder with the actual content. If you don't have the content, cut the slide.

---

## 2. One idea per slide

**Failure mode:** a slide titled "Q3 Results & Roadmap & Hiring Plan" that tries to do all three. The audience can only absorb one.

**Why it matters:** cognitive load. A 6-word summary is the test for "one idea" — if the slide needs more than 6 words to describe, it's more than one idea.

**Target:** every slide titles itself in ≤ 6 words, and that title is a complete thought, not a topic.

**Fix:** split into multiple slides. Use the "Section divider" slide-type to handle the handoff.

---

## 3. Headline not body

**Failure mode:** slide title is a topic ("Revenue") when it should be a takeaway ("Revenue grew 18% YoY despite churn in EMEA").

**Why it matters:** if the title is just the topic, the audience has to read the body to learn anything. The title should be the slide in one line.

**Target:** the title is a sentence (or sentence-fragment) that contains the conclusion.

**Fix:** rewrite the title as a sentence, then re-derive the bullets to support it.

---

## 4. Source citations on data slides

**Failure mode:** a chart of revenue with no source, or a stat with no date.

**Why it matters:** an un-sourced number is unfalsifiable. A C-suite audience will reject the whole slide, not just the number.

**Target:** every data slide has a small line at the bottom (10pt, muted color) like `Source: Internal finance dashboard, 2026-06-30.`

**Fix:** add the source line as a text box. Do not bury it in the speaker notes.

---

## 5. No accent line under titles

**Failure mode:** a thin horizontal line under every title.

**Why it matters:** this is the single biggest AI-slides tell. Designers rarely use it; default templates always have it. A human reviewer will spot it within 2 seconds.

**Target:** zero horizontal lines as decoration. If you need visual structure under a title, use whitespace, a color block, or a 1pt border on the title text itself.

**Fix:** in the slide master, remove the bottom border on the title placeholder. In individual slides, delete the line shape.

---

## 6. No off-canvas or cropped elements

**Failure mode:** an image that hangs off the right edge, a text box that runs past the bottom margin, a logo clipped on the corner.

**Why it matters:** looks broken, even if it isn't. Viewers will assume the deck was rushed.

**Target:** every element has a bounding box fully inside the 13.33" × 7.5" 16:9 frame. Test by converting to image and looking at the edges.

**Fix:** in the slide editor, select all on the affected slide and look for any shape whose outline touches or crosses the slide edge. Move it in by 0.1-0.2".

---

## 7. ≥ 0.5" margin on all sides

**Failure mode:** text running flush to the slide edge, or images starting at x=0, y=0.

**Why it matters:** projects look cheap. Eye needs air around content to track it.

**Target:** the leftmost content is at x ≥ 0.5", the rightmost at x ≤ 12.83", top at y ≥ 0.5", bottom at y ≤ 7.0".

**Fix:** select all on the slide and visually inspect the gaps. Move content inward as needed.

---

## 8. Contrast is readable

**Failure mode:** light gray body text on a cream background. Yellow accent text on white. Dark blue text on dark navy.

**Why it matters:** WCAG 2.1 AA requires body text to have a contrast ratio of ≥ 4.5:1 and large text (≥ 18pt or ≥ 14pt bold) ≥ 3:1. Failure = unreadable for ~8% of viewers (low-vision, projector wash-out, bright sunlight).

**Target:** pass WCAG AA. Use a contrast checker like https://webaim.org/resources/contrastchecker/ — paste the foreground and background hexes.

**Pre-validated palette picks** (all pass AA for body and AAA for large text):

| Use | Foreground | Background | Ratio |
|---|---|---|---|
| Body on white | `212121` | `FFFFFF` | 16.1:1 |
| Body on white | `36454F` | `FFFFFF` | 10.4:1 |
| Body on cream | `1E2761` | `ECE2D0` | 11.2:1 |
| Body on dark navy | `F2F2F2` | `1E2761` | 14.6:1 |
| Body on charcoal | `F5F5F5` | `36454F` | 12.1:1 |
| Accent only (not body) | `990011` | `FFFFFF` | 9.4:1 |

**Fix:** if your palette fails, swap the offending color for one of the pre-validated picks above. Don't change both colors — keep brand consistency.

---

## 9. Consistent fonts across slides

**Failure mode:** slide 1 in Georgia, slide 2 in Calibri, slide 3 in Arial, slide 4 in Trebuchet. The deck looks like a ransom note.

**Why it matters:** mixed fonts = no design system = no trust. Designers commit to ONE header + ONE body pair and use them across the whole deck.

**Target:** one header font and one body font, both used on every slide. Headers can use weight (bold vs regular) for hierarchy; they should not change family.

**Fix:** open the slide master and set the body font there. Re-apply per-slide overrides (delete them, then re-apply from the master).

---

## 10. Consistent palette across slides

**Failure mode:** each slide picks a different color from the "Color Palettes" table in `SKILL.md`. The deck looks like a mood board.

**Why it matters:** same as fonts. Pick a 3-color palette (dominant + support + accent) and use it.

**Target:** the same 3 hex values appear on every slide, with one dominant (60-70% of visual weight), one supporting (20-30%), and one accent (5-10%).

**Fix:** use the slide master. Lock the dominant color as the background for section dividers; the support color for content backgrounds; the accent for callouts only.

---

## 11. No two consecutive slides use the same layout

**Failure mode:** slides 4, 5, 6 are all "title + 4 bullets", in the same font, with the same column structure.

**Why it matters:** visual monotony = the audience stops paying attention. Slide 4 might as well be slides 4-6 combined.

**Target:** adjacent slides differ in layout. Rotate through: title + 4 bullets → two-column → icon row → data callout → comparison columns → quote → repeat.

**Fix:** before saving, scan the slide sorter view. If two adjacent slides look the same, redesign the second one to a different layout (even if the content is similar).

---

## 12. Has a summary / next-steps slide

**Failure mode:** the deck ends with a content slide. There's no synthesis.

**Why it matters:** a deck without a summary leaves the audience to do the work. They won't. The summary slide is where the intent from the *Pre-Generation Decision Tree* gets repeated as the takeaway.

**Target:** the second-to-last slide is "Summary" or "Next steps" with 3-5 bullets that mirror the deck's one-sentence intent. The last slide is "Thank you" or "Questions" and mirrors the cover.

**Fix:** write the summary first (before any content slide). Each content slide then becomes "evidence for" one of the summary bullets, not the other way around.

---

## Scoring summary

If you ran the 12-point audit and want a single pass/fail, the rule is:

- **12/12** → emit the marker.
- **10-11/12** → fix the failing items, re-verify, then emit.
- **< 10/12** → stop. The deck needs another pass from the *Pre-Generation Decision Tree* — the brief was wrong, not the slides.
