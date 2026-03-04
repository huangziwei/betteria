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
   - **Front matter to skip**: title pages, copyright, dedication, blank pages, table of contents — note exact page numbers
   - **Back matter to skip**: author bio, praise quotes, ads, blank pages — note exact page numbers
   - **Content page range**: the first and last pages of actual book content
   - **Section/chapter structure**: identify all chapter or section boundaries and their titles (they may be named sections like "INTRODUCTION", "PARTIES" rather than "Chapter 1", "Chapter 2")
   - **Running headers pattern**: e.g. alternating book title on even pages / section name on odd pages
   - **Page number format and location**: e.g. centered at bottom, top-right corner

Write a brief summary of your findings before proceeding.

---

## Phase 2: Page-by-page proofreading

Process every content page (skipping front/back matter identified in Phase 1).

### Resumability
Before processing a page, check if `$ARGUMENTS/artifacts/page-NNN.proofread.txt` already exists. If it does, **skip that page**. This makes the command resumable.

### Processing each page
Work in batches of 5-10 pages at a time. For each page:

1. Read the PNG via vision and the OCR `.txt` file side by side.
2. Produce corrected text:
   - **Fix OCR errors** by comparing what you see in the PNG against the OCR text. Trust vision over OCR when they disagree.
   - **Strip running headers** (identified in Phase 1) from the top of the page.
   - **Strip page numbers** from wherever they appear.
   - **Strip decorative elements**, figure legends, and table captions.
   - **Preserve paragraph structure** — maintain paragraph breaks as they appear in the original.
   - If a page is entirely a figure, table, illustration, or blank, write `[BLANK PAGE]` as its content.
3. Write the corrected text to `$ARGUMENTS/artifacts/page-NNN.proofread.txt`.

Do NOT summarize or paraphrase — reproduce the author's exact text with only OCR corrections and header/footer removal.

---

## Phase 3: Chapterize

Using the chapter boundaries identified in Phase 1 and the proofread page texts from Phase 2:

1. Create the directory `$ARGUMENTS/chapters/` (if it doesn't already exist).
2. For each chapter/section:
   - Read all `page-NNN.proofread.txt` files in the chapter's page range.
   - **Stitch page breaks**: if a sentence is split across two pages, join them into one flowing sentence. Remove hyphenation at page breaks (e.g. "con-\ncept" → "concept").
   - Skip any `[BLANK PAGE]` entries.
   - Write the merged text to `$ARGUMENTS/chapters/NN-slug.txt` where `NN` is a zero-padded chapter number and `slug` is a lowercase-hyphenated version of the chapter title (e.g. `01-introduction.txt`, `02-reality.txt`).
3. Write `$ARGUMENTS/metadata.json` with this exact schema:

```json
{
  "title": "<Book Title>",
  "author": "<Author Name>",
  "chapters": [
    {
      "number": 1,
      "title": "Introduction",
      "pages": [11, 17],
      "file": "01-introduction.txt"
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
