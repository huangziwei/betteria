Proofread and chapterize a scanned book. The book directory is: `$ARGUMENTS`

You will work through three phases. Be methodical and thorough.

---

## Phase 1: Survey the book structure

1. Glob `$ARGUMENTS/artifacts/*.png` and `$ARGUMENTS/artifacts/*.txt` to get the total page count.
2. Read a sample of pages using **both** the PNG (vision) and the OCR `.txt` to understand the book's structure. Read at least:
   - The first 10 pages
   - 3-5 pages from the middle
   - The last 5 pages
   - BUT NEVER load more than one PNG at a time
3. From this sample, determine:
   - **Page layout**: single-page or double-page spread
     - Check the image aspect ratio: if width > height, it is a double-page spread
     - If double-page spread, check whether page numbers appear in pairs (left + right)
     - for single-page layout, if height is > 2000px, resize all pngs with `sips --resampleHeight 1999 page-*.png 2>&1`
   - **Front matter to skip**: title pages, copyright, dedication, blank pages, table of contents — note exact page numbers
   - **Publication info** (from the copyright/title page): extract the **publisher**, **publication year**, and **ISBN** if visible — these go into `metadata.json`
   - **Back matter to skip**: footnotes/endnotes, references/bibliography, index, appendices, author bio, praise quotes, ads, blank pages — note exact page numbers
   - **Content page range**: the first and last pages of actual book content
   - **Section/chapter structure**: identify all chapter or section boundaries and their titles (they may be named sections like "INTRODUCTION", "PARTIES" rather than "Chapter 1", "Chapter 2")
   - **Running headers pattern**: e.g. alternating book title on even pages / section name on odd pages
   - **Page number format and location**: e.g. centered at bottom, top-right corner

4. If **double-page spread**, also note:
   - How image file numbers map to book page numbers (e.g. image `page-06` = book pages 8+9)
   - Whether the left page is even or odd

Write a brief summary of your findings in the `$ARGUMENTS/structure.md` before proceeding.

---

## Phase 2: Page-by-page proofreading

Process every content page (skipping front/back matter identified in Phase 1). Part/section divider pages (e.g. "PART I", "PART II" title pages) within the content range ARE content pages — do NOT skip them. After the proofreading process is interrupted, always refer to the `$ARGUMENTS/structure.md` for findings of Phase 1.

### Resumability

- **Single-page layout**: Before processing a page, check if `$ARGUMENTS/artifacts/page-NNN.proofread.txt` already exists. If it does, **skip that page** — EXCEPT for the very last proofread page (the highest-numbered `.proofread.txt` file).
- **Double-page spread**: Before processing a page, check if **both** `$ARGUMENTS/artifacts/page-NN-L.proofread.txt` and `$ARGUMENTS/artifacts/page-NN-R.proofread.txt` already exist. If both exist, **skip that image** — EXCEPT for the very last proofread image. This makes the command resumable.
- **Always re-verify the last proofread page**: Read both the PNG and the existing `.proofread.txt`, compare them, and fix any issues (missed OCR errors, unstripped headers, unjoined hyphens, missing markdown formatting). Only after confirming it is correct should you move on to the next un-proofread page. This guards against incomplete work from an interrupted session.

### Processing each page

Process **one page at a time**, sequentially. Do NOT use batch processing or subagents — work through each page yourself in order. Never process pages in parallel. For each page:

1. Read the PNG via vision and the OCR `.txt` file side by side.
2. Produce corrected text:
   - **Fix OCR errors** by comparing what you see in the PNG against the OCR text. Trust vision over OCR when they disagree.
   - **Strip running headers** (identified in Phase 1) from the top of the page.
   - **Strip page numbers** from wherever they appear.
   - **Strip decorative elements**, figure legends, and table captions.
   - **Strip figures and diagrams**: remove any inline figures, illustrations, diagrams, and their text labels/captions entirely. Do NOT insert any placeholder or description such as `[FIGURE: ...]`, `[Figure: ...]`, `[IMAGE: ...]`, or similar annotations — just omit the figure completely and silently. The surrounding prose that *references* the figure should be kept.
   - **Strip footnote anchors**: remove superscript footnote markers (e.g. `¹`, `²`, `³`, `[1]`, `*`) from the body text. Since footnotes/endnotes are excluded from the final output, dangling anchors serve no purpose and won't render correctly in markdown or epub.
   - **Preserve paragraph structure** — maintain paragraph breaks as they appear in the original.
   - **Join lines within paragraphs**: OCR text has hard line breaks at page-width boundaries. Join these into single continuous lines per paragraph. Only paragraph breaks (double newlines) should separate paragraphs. Do NOT keep mid-paragraph line breaks from the OCR — they produce incorrect rendering in markdown and epub.
   - If a page is entirely a figure, table, illustration, or blank, write `[BLANK PAGE]` as its content.

3. **Markdown formatting** (apply to both single-page and double-page layouts):
   - `*italic*` for italic text
   - `**bold**` for bold text
   - **Headings**: Use proper markdown headings (`#` through `######`) for section/chapter titles that appear on the page. **NEVER use bold (`**text**`) for headings** — always use heading syntax (`## text`). If a line contains only bold text and nothing else, it is a heading and must use `#` syntax. Use heading levels to reflect the book's hierarchy:
     - `##` for chapter titles (e.g. `## Chapter 1` or `## Three Ways of Talking about Value`)
     - `###` for major section subheadings within a chapter (e.g. `### I: Clyde Kluckhohn's value project`)
     - `####` for sub-subheadings if the book has a third level (e.g. subsections within a named case or part)
     - When in doubt about the level, use `###` for any named section within a chapter.
     - All headings must be in correct hierarchy — never skip levels (e.g. don't jump from `##` to `####`).
   - `> ` prefix for blockquotes / indented definition blocks (e.g. sidebars, callout boxes)
     - Include bold title lines within blockquotes: `> **against policy (a tiny manifesto):**`
   - `---` for section breaks (instead of bare `*` or other decorative dividers)
   - **Markdown line breaks** (`  ` — two trailing spaces) for consecutive lines that should render on separate lines but are NOT separate paragraphs. Use this for bibliography entries, lists of works/titles, poetry lines, and similar content where each line must stay distinct but belongs to one logical group. Without trailing spaces, consecutive lines merge into a single line in epub rendering. Example:
     ```
     *Grand Guignol* (1929).
     *It Walks by Night* (1930).
     *The Lost Gallows* (1931).
     ```
     (Note: the last line in a group does not need trailing spaces.)
   - Paragraph breaks remain as double newlines

4. **Output files**:
   - **Single-page**: Write the corrected text to `$ARGUMENTS/artifacts/page-NNN.proofread.txt`.
   - **Double-page spread**: Write **two** separate files per image:
     - `$ARGUMENTS/artifacts/page-NN-L.proofread.txt` (left book page)
     - `$ARGUMENTS/artifacts/page-NN-R.proofread.txt` (right book page)
     - Each file contains only that half's text, with headers/page numbers stripped.

5. **Page-boundary annotation**: After writing each proofread file, determine how this page should connect to the **next** page during stitching. Append one of these markers as the very last line of the proofread file:
   - `<!--JOIN-->` — the next page continues the same paragraph (flush left in the next page's PNG). Use this when the current page ends mid-sentence, OR when it ends with sentence-ending punctuation but the next page's first body line (below the running header) starts flush left.
   - `<!--PARA-->` — the next page starts a new paragraph (indented in the next page's PNG). Use this when the current page ends with sentence-ending punctuation and the next page's first body line is indented.
   - No marker needed if the page ends with `word-` (hyphenation) — the hyphen itself tells the stitching script to join.
   - No marker needed on the very last content page.

   To determine JOIN vs PARA: when the current page ends with sentence-ending punctuation (`.?!"')]}—`), read the **next** page's PNG and check whether the first body text line (below the running header) is indented or flush left. You're already reading pages sequentially, so you can peek ahead. This eliminates the need for the separate PB-resolution step in Phase 3.

**CHECKLIST — verify ALL steps before moving to the next page:**
1. Read PNG + OCR ✓
2. Corrected text (OCR fixes, stripped headers/footers/page numbers, joined lines) ✓
3. Markdown formatting (headings, italics, bold, blockquotes) ✓
4. Output file written ✓
5. Page-boundary annotation appended (`<!--JOIN-->`, `<!--PARA-->`, or none only if hyphenation or last page) ✓

Do NOT summarize or paraphrase — reproduce the author's exact text with only OCR corrections and header/footer removal.

### Periodic command refresh

Every **10 pages**, re-read this command file (`cat .claude/commands/proofread.md`) to refresh your understanding of the full instructions. Also re-read at the start of each new chapter. This counteracts context drift that causes shortcut-taking (batch processing, skipping steps, omitting page-boundary annotations) as the conversation grows long. Do not skip this step — it is as mandatory as the per-page checklist above.

**CRITICAL: Content filtering workaround.** Book content (especially passages involving violence, war, religion, or other sensitive topics) will trigger Anthropic's content filtering policy, causing a 400 error. To avoid this:

1. Do NOT reproduce any of the book's content in your conversational text output.
2. **The Only Preferred method**: Copy the OCR `.txt` file to the `.proofread.txt` path using `cp` via Bash, then use the `Edit` tool to make targeted corrections (strip headers, fix OCR errors, join hyphenated words, add markdown formatting). This avoids ever putting the full page text in a tool parameter.
3. **Never fallback** to use the `Write` tool with the corrected text.
4. Never discuss or quote the book's content in your conversational responses.

---

## Phase 2.5: Post-proofread cleanup

After all pages are proofread, scan for any `[FIGURE:` or `[IMAGE:` annotations that may have slipped through despite instructions:

1. Grep all `*.proofread.txt` files for lines matching `^\[FIGURE:` or `^\[IMAGE:`.
2. For each match, read the corresponding PNG to determine whether the line is a figure description or actual book text.
3. Remove any figure description lines. If removing the line leaves the page empty (or only whitespace), replace the content with `[BLANK PAGE]`.

---

## Phase 3: Chapterize

Use a **script-first approach** for efficiency: write a Python stitching script that processes all chapters at once, then verify and fix the output. Do NOT stitch chapters one at a time by reading each page manually.

### Step 1: Write a stitching script

Write a Python script (`$ARGUMENTS/stitch.py`) that:

1. Defines the chapter list with page ranges (from Phase 1 findings).
2. For each chapter, reads all `page-NNN.proofread.txt` files in range.
3. At each page boundary, applies these rules automatically:
   - If the next page starts with a `#` heading → new section (paragraph break).
   - If the current page ends with `word-` (hyphenation) → join the word, no break.
   - If the current page ends **without** sentence-ending punctuation (`.?!"')]}`) → mid-sentence, join with space.
   - If the current page ends **with** sentence-ending punctuation → check the page-boundary annotation at the end of the proofread file:
     - `<!--JOIN-->` → continuation, join with space.
     - `<!--PARA-->` → new paragraph (paragraph break).
     - If no annotation exists (legacy files without annotations) → **ambiguous**. Insert a `<!--PB:NNN-->` marker (where NNN is the page number) with paragraph breaks around it for later review.
4. Merges `## Chapter N` + `## Title` into a single heading `## Chapter N: Title` (preserving chapter numbers from the original).
5. Cleans up triple+ newlines.
6. Adds markdown line breaks (two trailing spaces) to blockquote lines: for every consecutive run of `> ` lines, add `  ` (two spaces) before the newline on every line **except** the last line of the run. Without these trailing spaces, consecutive blockquote lines merge into a single line in epub rendering. This is a safety net — trailing spaces should also be added during Phase 2 proofreading, but are easy to forget.
7. Writes each chapter to `$ARGUMENTS/chapters/NN-slug.md`.
8. Writes `$ARGUMENTS/metadata.json`.
9. Reports how many `<!--PB:-->` markers remain per chapter.

### Step 2: Resolve paragraph boundary markers (if any remain)

If Phase 2 included page-boundary annotations (`<!--JOIN-->` / `<!--PARA-->`), there should be few or no `<!--PB:NNN-->` markers. If some remain (from legacy/unannotated pages), resolve them **in bulk**:

1. Extract all PB page numbers from the chapter files.
2. For each PB at page N, check the PNG of page **N+1** to see if the first body text line (below the running header) is:
   - **Indented** → new paragraph (keep the break, remove marker)
   - **Flush left** → continuation (join with space, remove marker)
3. Check each PB page **yourself, one at a time, sequentially**. For each PB at page N, read the PNG of page N and examine whether the first body text line (below the running header) is indented or flush left. Record the result as `PB:N → INDENTED` or `PB:N → FLUSH`. Do NOT use parallel subagents — thoroughness matters more than speed.
4. Write a second Python script (`$ARGUMENTS/resolve_pb.py`) that applies the results: replace `\n\n<!--PB:N-->\n\n` with a space (for FLUSH) or `\n\n` (for INDENTED).

### Step 3: Verify

After resolving all markers, verify:
- No `<!--PB:` markers remain
- No `[FIGURE:` or `[IMAGE:` lines
- No `[BLANK PAGE]` entries
- No trailing word-hyphens (`\w-$` at line ends)
- Every chapter file starts with `## `
- Word counts look reasonable

### Chapter file requirements

- If the original book page has a chapter/section title, the chapter file must start with a proper markdown heading using `##`. For example: `## PREFACE`, `## I · THE EARLY YEARS`. Use `##` (not bold `**`) so that epub generators can use these as chapter titles without duplication. If the book uses roman numerals or chapter numbers before the title, combine them: `## IV · KITCHEN BIOGRAPHY`. If the original has no title (e.g. an epigraph page, dedication), do NOT invent a heading — just start with the content.
- Write the merged text to `$ARGUMENTS/chapters/NN-slug.md` where `NN` is a zero-padded chapter number and `slug` is a lowercase-hyphenated version of the chapter title (e.g. `01-introduction.md`, `02-reality.md`).
- Skip any `[BLANK PAGE]` entries.
- Preserve Markdown formatting from the proofread files.

### Clean up

Delete the temporary scripts (`stitch.py`, `resolve_pb.py`) after verification.

### metadata.json

Write `$ARGUMENTS/metadata.json` with this exact schema:

```json
{
  "title": "<Book Title>",
  "author": "<Author Name>",
  "publisher": "<Publisher Name>",
  "date": "<Publication Year>",
  "isbn": "<ISBN>",
  "chapters": [
    {
      "number": 1,
      "title": "Introduction",
      "pages": [11, 17],
      "file": "01-introduction.md"
    }
  ]
}
```

Where `pages` is `[first_page, last_page]` inclusive. The `publisher`, `date`, and `isbn` fields should be extracted from the book's copyright page during Phase 1 survey. These are included in the final EPUB metadata and colophon. Omit any field that is not available.

---

## Important notes

- Always prefer what you **see** in the PNG over what the OCR text says.
- Do not add any text that isn't in the original book — no summaries, commentary, or notes.
- Preserve the author's formatting choices (italics indicated by emphasis, paragraph breaks, section breaks within chapters).
- If you encounter an ambiguous word, use the context of the sentence and the visual appearance to determine the correct reading.
- Work systematically through all pages — do not skip content pages.
- **Paragraph breaks at page boundaries deserve special care.** A page break in the scan is NOT a paragraph break in the book. The stitching script handles unambiguous cases (mid-sentence, hyphenation) automatically. For ambiguous boundaries (sentence ends at page break), the script inserts `<!--PB:N-->` markers. These are resolved by checking indentation in the next page's PNG — check each one yourself, sequentially, for maximum accuracy.
- **Prefer thoroughness over speed.** When choosing between a faster approach (parallel subagents, batch processing) and a slower but more careful approach (sequential, one-at-a-time checking), always choose the slower, more thorough way.
