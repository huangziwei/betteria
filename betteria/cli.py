from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Sequence

import cv2
import img2pdf
from PIL import Image
from tqdm import tqdm

try:
    __version__ = metadata.version("betteria")
except metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"


def get_page_count(pdf_path: Path | str) -> int:
    """Return the number of pages in *pdf_path* using Poppler's ``pdfinfo``."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Input PDF not found: {path}")

    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            universal_newlines=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Poppler's 'pdfinfo' not found. Install Poppler or add it to PATH."
        ) from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Error running pdfinfo: {e.stderr}")

    output = result.stdout
    for line in output.splitlines():
        if line.lower().startswith("pages:"):
            parts = line.split()
            return int(parts[1])

    raise RuntimeError("Could not determine page count from pdfinfo output.")


def _page_sort_key(path: Path) -> int:
    stem = path.stem
    for part in reversed(stem.split("-")):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            return int(digits)
    return sys.maxsize


def _coerce_jobs(value: str | int | None) -> int:
    if isinstance(value, int):
        parsed = value
    else:
        token = (value or "").strip().lower()
        if token in {"", "auto", "max", "0"}:
            parsed = 0
        else:
            try:
                parsed = int(token)
            except ValueError as exc:  # pragma: no cover - handled by argparse
                raise argparse.ArgumentTypeError(
                    f"Invalid jobs value: {value}"
                ) from exc

    if parsed < 0:
        raise argparse.ArgumentTypeError("jobs must be non-negative")

    return parsed


def _run_pdftoppm_page(
    source: Path, output_prefix: Path, dpi: int, page: int
) -> tuple[int, str]:
    cmd = [
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        "-f",
        str(page),
        "-l",
        str(page),
        str(source),
        str(output_prefix),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Poppler's 'pdftoppm' not found. Install Poppler or add it to PATH."
        ) from exc

    stderr_output = result.stderr.decode().strip() if result.stderr else ""
    return result.returncode, stderr_output


def pdf_to_images(
    pdf_path: Path | str,
    dpi: int = 150,
    out_dir: Path | str | None = None,
    show_progress: bool = True,
    jobs: int = 0,
) -> list[Path]:
    """Render *pdf_path* to PNG files (optionally in parallel via ``pdftoppm``)."""
    source = Path(pdf_path)
    target_dir = (
        Path(out_dir)
        if out_dir is not None
        else Path(tempfile.mkdtemp(prefix="betteria-pages-"))
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    total_pages = get_page_count(source)
    output_prefix = target_dir / "page"

    worker_target = jobs if jobs > 0 else os.cpu_count() or 1
    worker_target = max(1, min(worker_target, total_pages))

    if worker_target <= 1:
        cmd = ["pdftoppm", "-png", "-r", str(dpi), str(source), str(output_prefix)]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Poppler's 'pdftoppm' not found. Install Poppler or add it to PATH."
            ) from exc

        assert process.stderr is not None  # for type checkers

        png_paths: list[Path] = []
        seen_files: set[str] = set()
        process_finished = False

        with tqdm(
            total=total_pages,
            desc="Converting PDF to PNG",
            disable=not show_progress,
            leave=False,
        ) as progress:
            while len(png_paths) < total_pages:
                new_found = 0

                for entry in target_dir.iterdir():
                    if entry.suffix.lower() != ".png":
                        continue
                    name = entry.name
                    if name in seen_files:
                        continue
                    seen_files.add(name)
                    png_paths.append(entry)
                    progress.update(1)
                    new_found += 1

                if len(png_paths) >= total_pages:
                    break

                if process_finished:
                    if new_found == 0:
                        break
                    continue

                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue

                process_finished = True

        if process.returncode is None:
            process.wait()
            process_finished = True

        stderr_output = process.stderr.read().decode().strip()
        process.stderr.close()

        if process.returncode != 0:
            raise RuntimeError(f"Error running pdftoppm: {stderr_output}")

        if len(png_paths) != total_pages:
            raise RuntimeError(
                f"Expected {total_pages} PNG files but found {len(png_paths)} in {target_dir}."
            )

        png_paths.sort(key=_page_sort_key)

        return png_paths

    with tqdm(
        total=total_pages,
        desc="Converting PDF to PNG",
        disable=not show_progress,
        leave=False,
    ) as progress_bar:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_target
        ) as executor:
            futures = {
                executor.submit(
                    _run_pdftoppm_page, source, output_prefix, dpi, page
                ): page
                for page in range(1, total_pages + 1)
            }

            try:
                for future in concurrent.futures.as_completed(futures):
                    page = futures[future]
                    try:
                        returncode, stderr_output = future.result()
                    except Exception:
                        for pending in futures:
                            pending.cancel()
                        raise

                    if returncode != 0:
                        for pending in futures:
                            pending.cancel()
                        raise RuntimeError(
                            f"Error running pdftoppm for page {page}: {stderr_output}"
                        )

                    progress_bar.update(1)
            except KeyboardInterrupt:
                for pending in futures:
                    pending.cancel()
                raise

    png_paths = sorted(target_dir.glob("*.png"), key=_page_sort_key)

    if len(png_paths) != total_pages:
        raise RuntimeError(
            f"Expected {total_pages} PNG files but found {len(png_paths)} in {target_dir}."
        )

    return png_paths


def whiten_and_save_as_tiff(
    input_path: Path | str,
    out_path: Path | str,
    threshold: int = 128,
    use_adaptive: bool = False,
    block_size: int = 31,
    c_val: int = 15,
    invert: bool = False,
) -> None:
    """Threshold *input_path* and write the result as a CCITT Group 4 TIFF."""
    img = cv2.imread(str(input_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError(f"Failed to read image: {input_path}")

    if invert:
        img = 255 - img

    if use_adaptive:
        bw = cv2.adaptiveThreshold(
            img,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size,
            c_val,
        )
    else:
        _, bw = cv2.threshold(img, threshold, 255, cv2.THRESH_BINARY)

    pil_bw = Image.fromarray(bw).convert("1")
    pil_bw.save(str(out_path), format="TIFF", compression="group4")


def convert_tiffs_to_pdf(
    tiff_paths: Sequence[Path | str], output_pdf: Path | str
) -> None:
    """Combine the TIFF pages in *tiff_paths* into *output_pdf* as a PDF."""
    paths: list[str] = [str(Path(path)) for path in tiff_paths]
    if not paths:
        raise ValueError("No TIFF pages supplied; cannot build PDF")

    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("wb") as file:
        file.write(img2pdf.convert(paths))


def _whiten_task(
    png_path: str,
    tiff_path: str,
    threshold: int,
    use_adaptive: bool,
    block_size: int,
    c_val: int,
    invert: bool,
) -> None:
    whiten_and_save_as_tiff(
        png_path,
        tiff_path,
        threshold=threshold,
        use_adaptive=use_adaptive,
        block_size=block_size,
        c_val=c_val,
        invert=invert,
    )


def betteria(
    input_pdf: Path | str,
    output_pdf: Path | str | None,
    dpi: int = 150,
    threshold: int = 128,
    use_adaptive: bool = False,
    block_size: int = 31,
    c_val: int = 15,
    invert: bool = False,
    show_progress: bool = True,
    jobs: int = 0,
) -> None:
    """
    1) Convert each PDF page to PNG via Poppler's 'pdftoppm' (page-by-page).
    2) For each PNG, whiten background -> 1-bit TIFF (CCITT Group 4).
    3) Merge TIFFs into one compressed PDF.
    4) Clean up temp directories (PNG + TIFF) even if interrupted.
    """
    if dpi <= 0:
        raise ValueError("DPI must be a positive integer")
    if not 0 <= threshold <= 255:
        raise ValueError("Threshold must be between 0 and 255")
    if use_adaptive and (block_size < 3 or block_size % 2 == 0):
        raise ValueError(
            "block_size must be an odd integer >= 3 when adaptive thresholding is enabled"
        )

    input_path = Path(input_pdf)
    if output_pdf is None:
        output_path = input_path.with_name(f"{input_path.stem}-enhanced.pdf")
    else:
        output_path = Path(output_pdf)

    if not input_path.exists():
        raise FileNotFoundError(f"Input PDF not found: {input_path}")
    if output_path.exists() and output_path.is_dir():
        raise IsADirectoryError(f"Output path points to a directory: {output_path}")

    with tempfile.TemporaryDirectory(prefix="betteria-pages-") as pages_dir_name:
        with tempfile.TemporaryDirectory(prefix="betteria-tiff-") as tiff_dir_name:
            pages_dir = Path(pages_dir_name)
            tiff_dir = Path(tiff_dir_name)

            png_paths = pdf_to_images(
                input_path,
                dpi=dpi,
                out_dir=pages_dir,
                show_progress=show_progress,
                jobs=jobs,
            )

            tiff_paths = [
                tiff_dir / (png_path.stem + ".tiff") for png_path in png_paths
            ]

            if not png_paths:
                raise RuntimeError("No PNG pages generated from input PDF")

            worker_target = jobs if jobs > 0 else os.cpu_count() or 1
            worker_target = min(worker_target, len(png_paths))

            if worker_target <= 1:
                iterator = tqdm(
                    png_paths,
                    desc="Whitening images",
                    disable=not show_progress,
                    leave=False,
                )
                for png_path, tiff_path in zip(iterator, tiff_paths):
                    whiten_and_save_as_tiff(
                        png_path,
                        tiff_path,
                        threshold=threshold,
                        use_adaptive=use_adaptive,
                        block_size=block_size,
                        c_val=c_val,
                        invert=invert,
                    )
            else:
                with tqdm(
                    total=len(png_paths),
                    desc="Whitening images",
                    disable=not show_progress,
                    leave=False,
                ) as progress_bar:
                    with concurrent.futures.ProcessPoolExecutor(
                        max_workers=worker_target
                    ) as executor:
                        futures = [
                            executor.submit(
                                _whiten_task,
                                str(png_path),
                                str(tiff_path),
                                threshold,
                                use_adaptive,
                                block_size,
                                c_val,
                                invert,
                            )
                            for png_path, tiff_path in zip(png_paths, tiff_paths)
                        ]

                        for future in concurrent.futures.as_completed(futures):
                            future.result()
                            progress_bar.update(1)

            convert_tiffs_to_pdf(tiff_paths, output_path)


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
    parser.add_argument(
        "--output",
        default=None,
        help="Path to output PDF (default: <input-stem>-enhanced.pdf)",
    )
    parser.add_argument(
        "--dpi", type=int, default=150, help="DPI for rasterizing PDF pages"
    )
    parser.add_argument(
        "--threshold", type=int, default=128, help="Global threshold value (0-255)"
    )
    parser.add_argument(
        "--block-size",
        type=int,
        default=31,
        help="Odd-sized neighborhood for adaptive thresholding (default: 31)",
    )
    parser.add_argument(
        "--c-val",
        type=int,
        default=15,
        help="Constant subtracted in adaptive thresholding (default: 15)",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="Use adaptive thresholding instead of a global threshold",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="Invert pixels before thresholding (for light text on dark background)",
    )
    parser.add_argument("--quiet", action="store_true", help="Disable progress bars")
    parser.add_argument(
        "--jobs",
        type=_coerce_jobs,
        default=0,
        help="Parallel workers for whitening ('auto' or an integer; use 1 to disable)",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    betteria(
        input_pdf=args.input,
        output_pdf=args.output,
        dpi=args.dpi,
        threshold=args.threshold,
        use_adaptive=args.adaptive,
        block_size=args.block_size,
        c_val=args.c_val,
        invert=args.invert,
        show_progress=not args.quiet,
        jobs=args.jobs,
    )


if __name__ == "__main__":
    main()
