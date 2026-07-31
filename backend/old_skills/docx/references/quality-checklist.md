# DOCX Quality Checklist (long form)

This is the long-form version of the **10-point pre-emit self-audit** in `SKILL.md`. The short form is a checklist; this file explains each point with examples, scoring, and what "good" looks like.

**Use it for:** the first time you generate a document in a new type, or when a self-audit item keeps failing and you need a deeper rubric.

**Skip it for:** routine documents where the 10-point check has consistently passed. The short form is enough.

---

## How to use this file

1. Run the 10-point audit from `SKILL.md` first.
2. For any item you can't pass confidently, scroll to the matching section below.
3. Each section gives: the failure mode, why it matters, the "good" target, and a copy-paste fix.

If you fail an item 2+ times in a row, stop and re-read *Choosing a Document Type* in `SKILL.md` — the issue is upstream of the audit (wrong template, wrong type), not the audit itself.

---

## 1. No placeholder text

**Failure mode:** `xxxx`, `Lorem ipsum`, `Click to add text`, `TODO`, `TBD`, `Sample text`, `Your title here`, `<insert name>`.

**Why it matters:** these are scaffolded by the source file or the template, not by you. If the user opens the DOCX and sees one, the whole document loses credibility — every other claim is now suspect.

**Target:** zero matches. Run on the source MD/HTML before conversion:

```bash
grep -iE "xxxx|lorem|ipsum|todo|tbd|click to add|your.*title|sample text|insert.*name" source.md
```

Exit code must be 1 (no matches).

**Fix:** open the source file and replace each placeholder with the actual content. If you don't have the content, remove the affected section.

---

## 2. Document type is appropriate

**Failure mode:** a memo with a table of contents. A letter with H2 sections. A proposal that's actually a memo.

**Why it matters:** the document type is a contract with the reader. Memos say "this is short, internal, action-oriented"; reports say "this is long, multi-section, reference material." Mismatching the type confuses the reader about how to use the document.

**Target:** the document's structure matches one of the 5 types in *Choosing a Document Type*. Each type has a structural template; the document either matches the template or has a documented reason to deviate.

**Fix:** pick the right type. If the content doesn't fit any of the 5 types, ask the user to confirm before generating.

---

## 3. Headings are not paragraphs

**Failure mode:** body text styled as a heading (large, bold, but still in the body paragraph), or headings that skip levels (H1 → H3 with no H2).

**Why it matters:** the Word navigation pane is built from heading levels. If your "headings" are styled body text, the navigation pane shows nothing useful. If you skip levels, the outline reads as broken.

**Target:** every H1/H2/H3 is a real heading element (in markdown, that's `#`/`##`/`###`). Levels are sequential: H1 → H2 → H3, no skipping.

**Fix:** in the source markdown, change paragraph-styled headings to actual `#`/`##`/`###` markers. Add missing intermediate levels (e.g., if you jumped from H1 to H3, add an H2).

---

## 4. All H1s/H2s/H3s use sentence case

**Failure mode:** "Findings From The Q3 Review" instead of "Findings from the Q3 review". "Action Items" instead of "Action items".

**Why it matters:** Title Case is for books and articles. Sentence case is for documents. Mixing them in one document looks inconsistent and amateurish.

**Target:** every heading is sentence case. First word capitalized; the rest lowercase except proper nouns and acronyms.

**Fix:** rewrite the heading. The exception is the document's H1 (cover page title), which can be Title Case if the user explicitly requests it.

---

## 5. Page numbers in the footer for multi-page documents

**Failure mode:** a 10-page report with no page numbers. A 5-page proposal where the reader has to scroll to know they're on page 3.

**Why it matters:** page numbers let readers cite specific content ("see page 4") and find their place after a break. Multi-page documents without page numbers are disorienting.

**Target:** page numbers in the footer for any document > 2 pages. The cover page may be page-numbered separately (often as page 0 or unnumbered) or not at all.

**Fix in HTML pipeline:** add to the document footer HTML:

```html
<footer>
  <p style="text-align: center;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></p>
</footer>
```

Pandoc converts this to a working page-number field.

---

## 6. Headers and footers are present

**Failure mode:** a 10-page report with no header (so the reader doesn't know what document they're in) and no footer (no page numbers, no author/date).

**Why it matters:** in a stack of printed pages, the header tells the reader which document they're in. The footer carries page numbers and authorship.

**Target:** for multi-page documents, both header and footer are populated. The header should have the document title (and optionally a date or section). The footer should have page numbers and/or the author/date.

**Fix:** add header and footer HTML to the document. See the *Headers & footers* subsection in *Typography & Page Layout* for the exact fields.

---

## 7. No tables that overflow the page width

**Failure mode:** a 5-column table where the last column is cut off at the right margin. A table that requires the user to scroll horizontally to read.

**Why it matters:** tables that overflow the page are unreadable when printed. They look broken on screen too.

**Target:** every table fits within the text area (6.5" wide inside 1" margins). If a table needs more columns, consider splitting it (e.g., a wide quarterly table becomes 4 quarterly sub-tables) or using a smaller font.

**Fix:** reduce the number of columns, reduce the per-column content (shorten labels), or split the table. If the table genuinely needs 6+ columns, consider a different format (a list of items, an appendix).

---

## 8. Source citations present

**Failure mode:** a claim like "Q3 revenue grew 18%" with no source. A statistic with no date or attribution.

**Why it matters:** un-sourced claims are unfalsifiable. A reader can't verify them and won't trust the rest of the document either.

**Target:** every data point, quote, or external claim has a source citation. Format: `Source: [name], [date].` in the caption or footnote.

**Fix:** add the source as a small italic line at the end of the section, or as a footnote. Do not bury it in the speaker notes (DOCX doesn't have speaker notes for documents).

---

## 9. XSD validation passed

**Failure mode:** `pack.py` (when editing existing DOCX) outputs `[VALIDATE] word/document.xml: FAILED` with a list of schema violations.

**Why it matters:** schema violations are the most common cause of "DOCX opens in Word but won't open in LibreOffice" or "the document looks fine but the macros are broken." Validation catches them before they reach the user.

**Target:** `pack.py` exits 0 with no `[VALIDATE] ... FAILED` lines. The validator runs by default; no need to enable it.

**Fix:** read the validator output. Each line points to a specific element. Common fixes:
- Re-add the missing attribute (e.g., `xml:space="preserve"` on a `<w:t>` with leading whitespace)
- Fix a nested element order (e.g., `<w:pStyle>` must come before `<w:rPr>` in `<w:pPr>`)
- Correct a namespace prefix (e.g., `w:` vs `wp:` vs `a:`)

If you're generating from MD/HTML, validation isn't directly relevant — but the conversion pipeline should not produce violations. If it does, that's a pipeline bug; report it rather than working around it.

---

## 10. Converted file opens cleanly in LibreOffice

**Failure mode:** `soffice --headless --convert-to pdf file.docx` produces a PDF with warnings, or fails entirely.

**Why it matters:** if LibreOffice can't convert the file, neither can most other downstream tools. A conversion failure is a strong signal that the file is malformed in a way that will eventually fail in some user's Word installation.

**Target:** `soffice` exits 0 and produces a PDF with no warnings in stderr. Run:

```bash
python scripts/office/soffice.py --headless --convert-to pdf file.docx 2>&1 | grep -iE "warning|error|fail"
```

Exit code must be 0 with no matches.

**Fix:** read the warning. Common ones:
- "Image with bad aspect ratio" — re-export the image at the correct aspect ratio
- "Missing alt text" — add alt text to the image (use the python-pptx snippet from the pptx skill's *Accessibility Quick Pass*)
- "Unknown element" — usually a custom XML namespace; remove the custom element or namespace it correctly

---

## Scoring summary

If you ran the 10-point audit and want a single pass/fail, the rule is:

- **10/10** → emit the marker.
- **8-9/10** → fix the failing items, re-verify, then emit.
- **< 8/10** → stop. The document needs another pass from the *Choosing a Document Type* section — the type was wrong, not the content.
