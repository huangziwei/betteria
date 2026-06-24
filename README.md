# betteria (v0.2)

A command-line pipeline for converting scanned PDFs to EPUB.

```
enhance ──── ocr ──── proofread ──── merge
PDF→PNG     PNG→TXT   TXT→chapters   →EPUB + searchable PDF
```

## Prerequisites

Apple Silicon only (the OCR step runs locally via [mlx-vlm](https://github.com/Blaizzy/mlx-vlm)). The proofreading is done with [Claude Code](https://code.claude.com/docs).

```bash
brew install poppler    # provides pdftocairo / pdftoppm for rasterizing
brew install tesseract  # word positions for the searchable PDF text layer
brew install uv         # Python package manager
```

For the searchable PDF, install the Tesseract language packs you need
(`tesseract-lang` covers all; it includes `jpn`/`jpn_vert` for vertical
Japanese). Without Tesseract, `merge` still builds a plain image-only PDF.

## Installation

```bash
uv sync --extra ocr
```

## Quick start

```bash
# 1. Rasterize and binarize scanned pages into clean PNGs
betteria enhance book.pdf

# 2. OCR the PNGs into per-page text files
betteria ocr book-artifacts/

# 3. Proofread and chapterize with Claude Code
#    (run inside this repo in a Claude Code session)
/proofread book-artifacts/

# 4. Build EPUB and/or enhanced PDF
betteria merge book-artifacts/
```

## Commands

### `betteria enhance <input.pdf>`

Rasterizes each page and applies adaptive thresholding to produce clean black-and-white PNGs. Key options:

- `--dpi` — resolution for rasterizing (default: 150)
- `--adaptive` / `--threshold` — adaptive vs. global binarization
- `--invert` — invert pixels before thresholding
- `--jobs` — parallel workers (default: all cores)
- `--rasterizer` — `pdftocairo` (default) or `pdftoppm`
- `--ppd` — page progression direction: `ltr` (default) or `rtl`

### `betteria ocr <book-dir>`

Runs a local VLM on the enhanced PNGs to produce per-page `.txt` files.

- `--model` — mlx-vlm model (default: `mlx-community/PaddleOCR-VL-1.5-6bit`)

### `betteria merge <book-dir>`

Combines proofread text and enhanced images into final outputs. The PDF is a
*searchable* sandwich: the enhanced image stays the visible layer, with the
proofread (corrected) text added as an invisible, selectable layer behind it.
Positions come from a Tesseract pass on each image, aligned to the proofread
text so the *corrected* words are what get embedded — Latin scripts align word
by word, CJK character by character (vertical Japanese included, via a
vertical-CMap font so readers extract columns in the right order).

- `--title` / `--author` — override metadata
- `--epub-only` / `--pdf-only` — generate only one format
- `--no-pdf-text` — skip the text layer; build an image-only PDF
- `--pdf-text-horizontal` — treat CJK as horizontal (default: vertical for Japanese)

The text layer's language comes from `metadata.json` (`"language"`), falling
back to English. If a page has no proofread text yet, its raw OCR `.txt` is
used so the PDF is still fully searchable.

---

> **Note:** v0.1 only enhanced scanned PDFs for e-ink readability. v0.2 adds the full OCR-to-EPUB pipeline. The old enhance-only behavior still works if you skip `ocr` — just run `enhance` + `merge`.
