# betteria

[![PyPI version](https://badge.fury.io/py/betteria.svg)](https://badge.fury.io/py/betteria)

A commandline tool to enhance PDFs from Internet Archive.

## Installation

```bash
pip install betteria
```

## Usage

```bash
betteria --help
```

    usage: betteria [-h] --input INPUT [--output OUTPUT] [--dpi DPI] [--threshold THRESHOLD] [--block-size BLOCK_SIZE] [--c-val C_VAL] [--adaptive]
                    [--invert] [--quiet] [--jobs JOBS] [-v]

    Clean and compress a scanned PDF by whitening pages and saving as CCITT Group 4 TIFFs (via a manual page-by-page approach).

    options:
    -h, --help            show this help message and exit
    --input INPUT         Path to input PDF
    --output OUTPUT       Path to output PDF (default: <input-stem>-enhanced.pdf)
    --dpi DPI             DPI for rasterizing PDF pages
    --threshold THRESHOLD
                            Global threshold value (0-255)
    --block-size BLOCK_SIZE
                            Odd-sized neighborhood for adaptive thresholding (default: 31)
    --c-val C_VAL         Constant subtracted in adaptive thresholding (default: 15)
    --adaptive            Use adaptive thresholding instead of a global threshold
    --invert              Invert pixels before thresholding (for light text on dark background)
    --quiet               Disable progress bars
    --jobs JOBS           Parallel workers for whitening ('auto' or an integer; use 1 to disable)
    -v, --version         show program's version number and exit
