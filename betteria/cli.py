import argparse
import os
import shutil
import sys

import cv2
import img2pdf
from pdf2image import convert_from_path
from PIL import Image
from tqdm import tqdm


def pdf_to_images(pdf_path, dpi=150, out_dir="pages_temp"):
    """Converts each page of a PDF into temporary PNG images at the specified DPI."""
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    pages = convert_from_path(pdf_path, dpi=dpi)
    image_paths = []
    # Use tqdm to display a progress bar during page conversion
    for i, page in enumerate(tqdm(pages, desc="Converting PDF to PNG")):
        out_path = os.path.join(out_dir, f"page_{i}.png")
        page.save(out_path, "PNG")
        image_paths.append(out_path)
    
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
    """Threshold the page to pure B/W, then save as 1-bit CCITT Group 4 TIFF."""
    # 1) Read grayscale
    img = cv2.imread(input_path, cv2.IMREAD_GRAYSCALE)

    # 2) Invert if needed
    if invert:
        img = 255 - img

    # 3) Threshold
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

    # 4) Convert to PIL image in "1" mode (1-bit pixels)
    pil_bw = Image.fromarray(bw).convert("1")

    # 5) Save as TIFF with Group 4 compression
    # Pillow can do this if we specify "compression='group4'".
    pil_bw.save(out_path, format="TIFF", compression="group4")

def convert_tiffs_to_pdf(tiff_paths, output_pdf):
    """Combine a list of CCITT Group-4 TIFF images into a single PDF using img2pdf."""
    # img2pdf works best when the source images are the correct format (1-bit).
    # It will embed them without recompression.
    with open(output_pdf, "wb") as f:
        f.write(img2pdf.convert(tiff_paths))

def clean_pdf(
    input_pdf,
    output_pdf,
    dpi=150,
    threshold=128,
    use_adaptive=False,
    block_size=31,
    c_val=15,
    invert=False
):
    # 1) Convert each PDF page to PNG
    png_paths = pdf_to_images(input_pdf, dpi=dpi)

    # 2) Whiten each PNG -> 1-bit TIFF
    tiff_dir = "tiff_temp"
    if not os.path.exists(tiff_dir):
        os.makedirs(tiff_dir)

    tiff_paths = []
    for png_path in tqdm(png_paths, desc="Whitening images"):
        base = os.path.splitext(os.path.basename(png_path))[0]
        tiff_path = os.path.join(tiff_dir, f"{base}.tiff")
        whiten_and_save_as_tiff(
            png_path, tiff_path,
            threshold=threshold,
            use_adaptive=use_adaptive,
            block_size=block_size,
            c_val=c_val,
            invert=invert
        )
        tiff_paths.append(tiff_path)

    # 3) Merge all TIFFs into a PDF with CCITT Group 4 compression
    convert_tiffs_to_pdf(tiff_paths, output_pdf)

    # Optional: clean up temporary files
    shutil.rmtree("pages_temp")
    shutil.rmtree("tiff_temp")

def main():

    # If user only typed one argument (besides the script name) and it doesn't start with '-',
    # treat that argument as --input
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        sys.argv = [sys.argv[0], "--input", sys.argv[1]]

    parser = argparse.ArgumentParser(
        description="Clean and compress a scanned PDF by whitening pages "
                    "and saving as CCITT Group 4 TIFFs."
    )
    parser.add_argument("--input", required=True, help="Path to input PDF")
    parser.add_argument("--output", required=False, default="output.pdf", help="Path to output PDF (default: output.pdf)")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for rasterizing PDF pages")
    parser.add_argument("--threshold", type=int, default=128, help="Global threshold value")
    parser.add_argument("--use_adaptive", type=lambda x: (str(x).lower()=="true"), default=False,
                        help="Set True to use adaptive thresholding instead of global")
    parser.add_argument("--invert", type=lambda x: (str(x).lower()=="true"), default=False,
                        help="Set True if pages are inverted (light text on dark background)")

    args = parser.parse_args()

    clean_pdf(
        input_pdf=args.input,
        output_pdf=args.output,
        dpi=args.dpi,
        threshold=args.threshold,
        use_adaptive=args.use_adaptive,
        invert=args.invert
    )

if __name__ == "__main__":
    main()
