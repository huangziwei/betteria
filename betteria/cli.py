import argparse
import os
import shutil
import sys
import subprocess
from tqdm import tqdm

import cv2
import img2pdf
from PIL import Image

def get_page_count(pdf_path):
    """
    Use 'pdfinfo' (part of Poppler) to read the total number of pages in the PDF.
    Returns an integer page count or raises an error if 'pdfinfo' isn't installed.
    """
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            universal_newlines=True
        )
    except FileNotFoundError:
        raise RuntimeError("Poppler's 'pdfinfo' not found. Install Poppler or add it to PATH.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Error running pdfinfo: {e.stderr}")

    output = result.stdout
    for line in output.splitlines():
        if line.lower().startswith("pages:"):
            parts = line.split()
            return int(parts[1])
    
    raise RuntimeError("Could not determine page count from pdfinfo output.")

def pdf_to_images(pdf_path, dpi=150, out_dir="pages_temp"):
    """
    Converts each page of a PDF into temporary PNG images at the specified DPI
    by manually calling 'pdftoppm' page by page, using '-singlefile' so 
    we get consistent filenames (e.g., page_1.png).
    """
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    total_pages = get_page_count(pdf_path)  # your existing function that calls 'pdfinfo'
    image_paths = []

    for page_num in tqdm(range(1, total_pages + 1), desc="Converting PDF to PNG"):
        out_stub = os.path.join(out_dir, f"page_{page_num}")

        cmd = [
            "pdftoppm",
            "-f", str(page_num),       # start page
            "-l", str(page_num),       # end page
            "-r", str(dpi),            # DPI
            "-png",
            "-singlefile",             # produce exactly 1 file: 'out_stub.png'
            pdf_path,
            out_stub
        ]
        subprocess.run(cmd, check=True)

        # Now we know the file is pages_temp/page_{page_num}.png
        final_png = f"{out_stub}.png"
        image_paths.append(final_png)

    return image_paths

def whiten_and_save_as_tiff(
    input_path,
    out_path,
    threshold=128,
    use_adaptive=False,
    block_size=31,
    c_val=15,
    invert=False
):
    """
    Threshold the page to pure B/W, then save as a 1-bit CCITT Group 4 TIFF.
    """
    img = cv2.imread(input_path, cv2.IMREAD_GRAYSCALE)

    if invert:
        img = 255 - img

    if use_adaptive:
        bw = cv2.adaptiveThreshold(
            img, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size,
            c_val
        )
    else:
        _, bw = cv2.threshold(img, threshold, 255, cv2.THRESH_BINARY)

    pil_bw = Image.fromarray(bw).convert("1")  # 1-bit pixels
    pil_bw.save(out_path, format="TIFF", compression="group4")

def convert_tiffs_to_pdf(tiff_paths, output_pdf):
    """
    Combine a list of CCITT Group-4 TIFF images into a single PDF using img2pdf.
    """
    with open(output_pdf, "wb") as f:
        f.write(img2pdf.convert(tiff_paths))

def betteria(
    input_pdf,
    output_pdf,
    dpi=150,
    threshold=128,
    use_adaptive=False,
    block_size=31,
    c_val=15,
    invert=False
):
    """
    1) Convert each PDF page to PNG via Poppler's 'pdftoppm' (page-by-page).
    2) For each PNG, whiten background -> 1-bit TIFF (CCITT Group 4).
    3) Merge TIFFs into one compressed PDF.
    4) Clean up temp directories (PNG + TIFF).
    """
    # Convert PDF to PNGs (page-by-page)
    png_paths = pdf_to_images(input_pdf, dpi=dpi, out_dir="pages_temp")

    # Whiten images and store as 1-bit TIFF
    tiff_dir = "tiff_temp"
    os.makedirs(tiff_dir, exist_ok=True)

    tiff_paths = []
    for png_path in tqdm(png_paths, desc="Whitening images"):
        base = os.path.splitext(os.path.basename(png_path))[0]
        tiff_path = os.path.join(tiff_dir, f"{base}.tiff")

        whiten_and_save_as_tiff(
            png_path,
            tiff_path,
            threshold=threshold,
            use_adaptive=use_adaptive,
            block_size=block_size,
            c_val=c_val,
            invert=invert
        )
        tiff_paths.append(tiff_path)

    # Merge all TIFF pages into final PDF
    convert_tiffs_to_pdf(tiff_paths, output_pdf)

    # Clean up
    shutil.rmtree("pages_temp", ignore_errors=True)
    shutil.rmtree("tiff_temp", ignore_errors=True)

def main():
    # If user only typed one argument (besides the script name) and it doesn't start with '-',
    # treat that argument as --input
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        sys.argv = [sys.argv[0], "--input", sys.argv[1]]

    parser = argparse.ArgumentParser(
        description="Clean and compress a scanned PDF by whitening pages "
                    "and saving as CCITT Group 4 TIFFs (via a manual page-by-page approach)."
    )
    parser.add_argument("--input", required=True, help="Path to input PDF")
    parser.add_argument("--output", default="output.pdf", help="Path to output PDF (default: output.pdf)")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for rasterizing PDF pages")
    parser.add_argument("--threshold", type=int, default=128, help="Global threshold value")
    parser.add_argument("--use_adaptive", type=lambda x: (str(x).lower()=="true"), default=False,
                        help="Set True to use adaptive thresholding instead of global")
    parser.add_argument("--invert", type=lambda x: (str(x).lower()=="true"), default=False,
                        help="Set True if pages are inverted (light text on dark background)")

    args = parser.parse_args()

    betteria(
        input_pdf=args.input,
        output_pdf=args.output,
        dpi=args.dpi,
        threshold=args.threshold,
        use_adaptive=args.use_adaptive,
        invert=args.invert
    )

if __name__ == "__main__":
    main()
