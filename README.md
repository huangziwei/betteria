# betteria (v0.2)

A command-line pipeline for converting scanned PDFs to EPUB.

```
enhance ──── ocr ──── proofread ──── merge
PDF→PNG     PNG→TXT   TXT→chapters   →EPUB/PDF
```

## Prerequisites

- [Poppler](https://poppler.freedesktop.org/) (`pdftocairo` / `pdftoppm`)
- Apple Silicon Mac (OCR step uses [mlx-vlm](https://github.com/Blaizzy/mlx-vlm))
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (proofread step)

## Installation

```bash
uv sync
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

### `betteria ocr <book-dir>`

Runs a local VLM on the enhanced PNGs to produce per-page `.txt` files.

- `--model` — mlx-vlm model (default: `mlx-community/PaddleOCR-VL-1.5-6bit`)

### `betteria merge <book-dir>`

Combines proofread text and enhanced images into final outputs.

- `--title` / `--author` — override metadata
- `--epub-only` / `--pdf-only` — generate only one format

---

> **Note:** v0.1 only enhanced scanned PDFs for e-ink readability. v0.2 adds the full OCR-to-EPUB pipeline. The old enhance-only behavior still works if you skip `ocr` — just run `enhance` + `merge`. v0.1.2 remains on PyPI (`pip install betteria`); v0.2 will not be published there.
