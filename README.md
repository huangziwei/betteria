# betteria (v2)

A commandline pipeline for converting scanned PDFs to EPUB.

## Installation

```bash
uv sync
```

## Usage

```bash
betteria --help
```

    usage: betteria [-h] [-v] {enhance,ocr,merge} ...

    OCR and EPUB pipeline for scanned PDFs.

    positional arguments:
    {enhance,ocr,merge}
        enhance            Rasterize and enhance a scanned PDF into clean PNGs.
        ocr                OCR enhanced PNGs into per-page text files.
        merge              Build EPUB from proofread chapters and/or enhanced PDF from PNGs.

    options:
    -h, --help           show this help message and exit
    -v, --version        show program's version number and exit

To enhance the scanned PDF into OCR-able PNGs:

```bash
betteria enhance -h
```

    usage: betteria enhance [-h] [--dpi DPI] [--threshold THRESHOLD] [--block-size BLOCK_SIZE] [--c-val C_VAL] [--adaptive] [--invert] [--quiet] [--jobs JOBS] [--rasterizer {pdftoppm,pdftocairo}] input

    positional arguments:
    input                 Path to input PDF

    options:
    -h, --help            show this help message and exit
    --dpi DPI             DPI for rasterizing PDF pages (default: 150)
    --threshold THRESHOLD
                            Global threshold value 0-255 (default: 128; ignored when adaptive)
    --block-size BLOCK_SIZE
                            Neighborhood size for adaptive thresholding (default: 31)
    --c-val C_VAL         Constant for adaptive thresholding (default: 15)
    --adaptive            Use adaptive thresholding (default: on)
    --invert              Invert pixels before thresholding
    --quiet               Disable progress bars
    --jobs JOBS           Parallel workers ('auto'/0 = all cores; 1 = single thread)
    --rasterizer {pdftoppm,pdftocairo}
                            Poppler backend (default: pdftocairo)


To extract text from the images:

```bash
betteria ocr -h    
```

    usage: betteria ocr [-h] [--model MODEL] [--quiet] input

    positional arguments:
    input          Path to book directory

    options:
    -h, --help     show this help message and exit
    --model MODEL  mlx-vlm model for OCR (default: mlx-community/PaddleOCR-VL-1.5-6bit)
    --quiet        Disable progress bars

To proofread and clean up the OCR text, I use a Claude command to do the job (`.claude/commands/proofread.md`). Run `/proofread <path/to/book-artifacts/folder>` 

And finally, to merge the enhanced PNG and proofread TXT into PDF and EPUB:

```bash
betteria merge -h
```
    usage: betteria merge [-h] [--title TITLE] [--author AUTHOR] [--epub-only] [--pdf-only] [--quiet] input

    positional arguments:
    input            Path to book directory

    options:
    -h, --help       show this help message and exit
    --title TITLE    Override book title from metadata
    --author AUTHOR  Override author from metadata
    --epub-only      Only generate EPUB (skip PDF)
    --pdf-only       Only generate PDF (skip EPUB)
    --quiet          Disable progress bars

> ### NOTE
> `betteria` v0.1 was for enhancing scanned PDF readability on E-Ink devices only, while v0.2 focuses on convert the PDF to EPUB. The old behavior is preserved if you skip the `betteria ocr` step, which is now done in two separated steps (`betteria enhanced` + `betteria merge`). v0.1.2 is still on PyPI can can be installed via `pip install betteria`. v0.2 will not be on PyPI. 