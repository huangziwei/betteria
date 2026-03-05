Proofread and chapterize a scanned book. The book directory is: `$ARGUMENTS`

You will work through three phases. Be methodical and thorough.

---

## Phase 1: Survey the book structure

1. Glob `$ARGUMENTS/artifacts/*.png` and `$ARGUMENTS/artifacts/*.txt` to get the total page count.
2. Read a sample of pages using **both** the PNG (vision) and the OCR `.txt` to understand the book's structure. Read at least:
   - The first 10 pages
   - 3-5 pages from the middle
   - The last 5 pages
3. From this sample, determine:
   - **Page layout**: single-page or double-page spread
     - Check the image aspect ratio: if width > height, it is a double-page spread
     - If double-page spread, check whether page numbers appear in pairs (left + right)
   - **Front matter to skip**: title pages, copyright, dedication, blank pages, table of contents — note exact page numbers
   - **Back matter to skip**: author bio, praise quotes, ads, blank pages — note exact page numbers
   - **Content page range**: the first and last pages of actual book content
   - **Section/chapter structure**: identify all chapter or section boundaries and their titles (they may be named sections like "INTRODUCTION", "PARTIES" rather than "Chapter 1", "Chapter 2")
   - **Running headers pattern**: e.g. alternating book title on even pages / section name on odd pages
   - **Page number format and location**: e.g. centered at bottom, top-right corner

4. If **double-page spread**, also note:
   - How image file numbers map to book page numbers (e.g. image `page-06` = book pages 8+9)
   - Whether the left page is even or odd

Write a brief summary of your findings before proceeding.

---

## Phase 2: Page-by-page proofreading

Process every content page (skipping front/back matter identified in Phase 1).

### Resumability

- **Single-page layout**: Before processing a page, check if `$ARGUMENTS/artifacts/page-NNN.proofread.txt` already exists. If it does, **skip that page**.
- **Double-page spread**: Before processing a page, check if **both** `$ARGUMENTS/artifacts/page-NN-L.proofread.txt` and `$ARGUMENTS/artifacts/page-NN-R.proofread.txt` already exist. If both exist, **skip that image**. This makes the command resumable.

### Processing each page

Process **one page at a time**, sequentially. Do NOT use batch processing or subagents — work through each page yourself in order. For each page:

1. Read the PNG via vision and the OCR `.txt` file side by side.
2. Produce corrected text:
   - **Fix OCR errors** by comparing what you see in the PNG against the OCR text. Trust vision over OCR when they disagree.
   - **Strip running headers** (identified in Phase 1) from the top of the page.
   - **Strip page numbers** from wherever they appear.
   - **Strip decorative elements**, figure legends, and table captions.
   - **Strip figures and diagrams**: remove any inline figures, illustrations, diagrams, and their text labels/captions entirely. Do NOT insert any placeholder or description such as `[FIGURE: ...]`, `[Figure: ...]`, `[IMAGE: ...]`, or similar annotations — just omit the figure completely and silently. The surrounding prose that *references* the figure should be kept.
   - **Preserve paragraph structure** — maintain paragraph breaks as they appear in the original.
   - If a page is entirely a figure, table, illustration, or blank, write `[BLANK PAGE]` as its content.

3. **Markdown formatting** (apply to both single-page and double-page layouts):
   - `*italic*` for italic text
   - `**bold**` for bold text
   - `> ` prefix for blockquotes / indented definition blocks (e.g. sidebars, callout boxes)
     - Include bold title lines within blockquotes: `> **against policy (a tiny manifesto):**`
   - `---` for section breaks (instead of bare `*` or other decorative dividers)
   - Paragraph breaks remain as double newlines

4. **Output files**:
   - **Single-page**: Write the corrected text to `$ARGUMENTS/artifacts/page-NNN.proofread.txt`.
   - **Double-page spread**: Write **two** separate files per image:
     - `$ARGUMENTS/artifacts/page-NN-L.proofread.txt` (left book page)
     - `$ARGUMENTS/artifacts/page-NN-R.proofread.txt` (right book page)
     - Each file contains only that half's text, with headers/page numbers stripped.

Do NOT summarize or paraphrase — reproduce the author's exact text with only OCR corrections and header/footer removal.

---

## Phase 2.5: Post-proofread cleanup

After all pages are proofread, scan for any `[FIGURE:` or `[IMAGE:` annotations that may have slipped through despite instructions:

1. Grep all `*.proofread.txt` files for lines matching `^\[FIGURE:` or `^\[IMAGE:`.
2. For each match, read the corresponding PNG to determine whether the line is a figure description or actual book text.
3. Remove any figure description lines. If removing the line leaves the page empty (or only whitespace), replace the content with `[BLANK PAGE]`.

---

## Phase 3: Chapterize

Using the chapter boundaries identified in Phase 1 and the proofread page texts from Phase 2:

1. Create the directory `$ARGUMENTS/chapters/` (if it doesn't already exist).
2. For each chapter/section:
   - **Single-page**: Read all `page-NNN.proofread.txt` files in the chapter's page range.
   - **Double-page spread**: Read `-L` and `-R` files in book-page order (left before right, or right before left, depending on which has the lower book page number).
   - **Stitch page breaks** — this is the most critical step. Pages often break in the middle of a sentence or paragraph. You must reconstruct the correct paragraph structure:
     1. **Remove hyphenation**: if a page ends with a hyphenated word (e.g. `con-`), join it with the first word of the next page (`concept`).
     2. **Join mid-sentence breaks**: if a page ends without sentence-ending punctuation (`.` `?` `!` or a closing quote after such punctuation), the next page is a direct continuation — join with a single space, **no paragraph break**.
     3. **Join mid-paragraph breaks**: even if a page ends with a complete sentence, the next page may continue the *same paragraph*. Check the last paragraph of page N and the first paragraph of page N+1 — if they form one logical paragraph, join them with a single space. Look at the original PNGs at page boundaries if needed: a new paragraph in the original is indicated by indentation or extra vertical space on the printed page. If the next page's text starts flush left with no indentation, it is the same paragraph.
     4. **Preserve real paragraph breaks**: only insert a double newline (`\n\n`) at a page boundary when the original book genuinely starts a new paragraph on the next page.
     5. **Default to joining**: when in doubt, join. A false paragraph break is worse than a missing one, because it fragments the author's prose.
   - Skip any `[BLANK PAGE]` entries.
   - **Preserve Markdown formatting** from the proofread files.
   - Write the merged text to `$ARGUMENTS/chapters/NN-slug.md` where `NN` is a zero-padded chapter number and `slug` is a lowercase-hyphenated version of the chapter title (e.g. `01-introduction.md`, `02-reality.md`).
3. After writing each chapter file, verify it contains no `[FIGURE:` or `[IMAGE:` lines. If any are found, remove them.
4. Write `$ARGUMENTS/metadata.json` with this exact schema:

```json
{
  "title": "<Book Title>",
  "author": "<Author Name>",
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

Where `pages` is `[first_page, last_page]` inclusive.

---

## Important notes

- Always prefer what you **see** in the PNG over what the OCR text says.
- Do not add any text that isn't in the original book — no summaries, commentary, or notes.
- Preserve the author's formatting choices (italics indicated by emphasis, paragraph breaks, section breaks within chapters).
- If you encounter an ambiguous word, use the context of the sentence and the visual appearance to determine the correct reading.
- Work systematically through all pages — do not skip content pages.
- **Paragraph breaks at page boundaries deserve special care.** A page break in the scan is NOT a paragraph break in the book. During chapterization, always check whether text flows continuously across a page boundary. Read the ending of page N and the beginning of page N+1 together as prose — if it reads as one paragraph, merge them. Refer back to the PNGs when unsure.
