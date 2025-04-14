# betteria

A commandline tool to enhance PDFs from Internet Archive.

## Installation

```bash
pip install betteria
```

## Usage

```bash
betteria --help
```

    usage: betteria [-h] --input INPUT [--output OUTPUT] [--dpi DPI] [--threshold THRESHOLD] [--use_adaptive USE_ADAPTIVE] [--invert INVERT]

    Clean and compress a scanned PDF by whitening pages and saving as CCITT Group 4 TIFFs (via a manual page-by-page approach).

    options:
    -h, --help            show this help message and exit
    --input INPUT         Path to input PDF
    --output OUTPUT       Path to output PDF (default: output.pdf)
    --dpi DPI             DPI for rasterizing PDF pages
    --threshold THRESHOLD
                            Global threshold value
    --use_adaptive USE_ADAPTIVE
                            Set True to use adaptive thresholding instead of global
    --invert INVERT       Set True if pages are inverted (light text on dark background)

