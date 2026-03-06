from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Literal, Sequence

import cv2
import img2pdf
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from PIL import Image

try:
    __version__ = metadata.version("betteria")
except metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

Rasterizer = Literal["pdftoppm", "pdftocairo"]


# ── Utilities ────────────────────────────────────────────────────────


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


def _available_cpu_count() -> int:
    """Best-effort CPU count (logical when available, affinity-aware on Linux)."""
    try:
        return len(os.sched_getaffinity(0))
    except Exception:
        pass

    if sys.platform.startswith(("darwin", "freebsd")):
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.logicalcpu_max"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                text=True,
            )
            count = int(result.stdout.strip())
            if count > 0:
                return count
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.physicalcpu_max"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                text=True,
            )
            count = int(result.stdout.strip())
            if count > 0:
                return count
        except Exception:
            pass

    count = os.cpu_count() or 1
    return max(1, count)


def _build_rasterizer_cmd(
    backend: Rasterizer,
    dpi: int,
    source: Path,
    output_prefix: Path,
    page: int | None = None,
) -> list[str]:
    if backend not in {"pdftoppm", "pdftocairo"}:
        raise ValueError(f"Unsupported rasterizer backend: {backend}")

    cmd = [backend, "-png", "-r", str(dpi)]
    if page is not None:
        cmd += ["-f", str(page), "-l", str(page)]
    cmd += [str(source), str(output_prefix)]
    return cmd


@contextlib.contextmanager
def _progress(total: int, description: str, enabled: bool):
    """
    Wrap Rich progress so callers can advance regardless of whether it is shown.
    """
    if not enabled:
        # Null object for disabled progress
        class _NullProgress:
            def advance(self, *_args, **_kwargs):
                return None

        yield _NullProgress(), 0
        return

    console = Console(stderr=True)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    with progress:
        task_id = progress.add_task(description, total=total)
        yield progress, task_id


# ── PDF to Images ────────────────────────────────────────────────────


def _run_rasterizer_page(
    backend: Rasterizer, source: Path, output_prefix: Path, dpi: int, page: int
) -> tuple[int, str]:
    cmd = _build_rasterizer_cmd(backend, dpi, source, output_prefix, page)

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Poppler's '{backend}' not found. Install Poppler or add it to PATH."
        ) from exc

    stderr_output = result.stderr.decode().strip() if result.stderr else ""
    return result.returncode, stderr_output


def pdf_to_images(
    pdf_path: Path | str,
    dpi: int = 150,
    out_dir: Path | str | None = None,
    show_progress: bool = True,
    jobs: int = 0,
    rasterizer: Rasterizer = "pdftocairo",
) -> list[Path]:
    """Render *pdf_path* to PNG files (optionally in parallel via Poppler)."""
    source = Path(pdf_path)
    target_dir = (
        Path(out_dir)
        if out_dir is not None
        else Path(tempfile.mkdtemp(prefix="betteria-pages-"))
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    total_pages = get_page_count(source)
    output_prefix = target_dir / "page"

    worker_target = jobs if jobs > 0 else _available_cpu_count()
    worker_target = max(1, min(worker_target, total_pages))

    if worker_target <= 1:
        cmd = _build_rasterizer_cmd(rasterizer, dpi, source, output_prefix)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Poppler's '{rasterizer}' not found. Install Poppler or add it to PATH."
            ) from exc

        assert process.stderr is not None  # for type checkers

        with _progress(total_pages, "Converting PDF to PNG", show_progress) as (
            progress,
            task_id,
        ):
            try:
                process.wait()
            except KeyboardInterrupt:
                process.terminate()
                process.wait()
                stderr_output = process.stderr.read().decode().strip()
                process.stderr.close()
                raise RuntimeError(
                    f"Rasterization interrupted: {stderr_output}"
                ) from None

            for _ in range(total_pages):
                progress.advance(task_id, 1)

        stderr_output = process.stderr.read().decode().strip()
        process.stderr.close()

        if process.returncode != 0:
            raise RuntimeError(f"Error running {rasterizer}: {stderr_output}")

        png_paths = sorted(
            target_dir.glob(f"{output_prefix.name}-*.png"), key=_page_sort_key
        )

        if len(png_paths) != total_pages:
            raise RuntimeError(
                f"Expected {total_pages} PNG files but found {len(png_paths)} in {target_dir}."
            )

        return png_paths

    with _progress(total_pages, "Converting PDF to PNG", show_progress) as (
        progress_bar,
        task_id,
    ):
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_target
        ) as executor:
            futures = {
                executor.submit(
                    _run_rasterizer_page,
                    rasterizer,
                    source,
                    output_prefix,
                    dpi,
                    page,
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
                            f"Error running {rasterizer} for page {page}: {stderr_output}"
                        )

                    progress_bar.advance(task_id, 1)
            except KeyboardInterrupt:
                for pending in futures:
                    pending.cancel()
                raise

    png_paths = sorted(
        target_dir.glob(f"{output_prefix.name}-*.png"), key=_page_sort_key
    )

    if len(png_paths) != total_pages:
        raise RuntimeError(
            f"Expected {total_pages} PNG files but found {len(png_paths)} in {target_dir}."
        )

    return png_paths


# ── Image Processing ─────────────────────────────────────────────────


def whiten_and_save(
    input_path: Path | str,
    out_path: Path | str,
    threshold: int = 128,
    use_adaptive: bool = False,
    block_size: int = 31,
    c_val: int = 15,
    invert: bool = False,
) -> None:
    """Threshold *input_path* and write the result as a grayscale PNG."""
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

    cv2.imwrite(str(out_path), bw)


def _whiten_task(
    png_path: str,
    out_path: str,
    threshold: int,
    use_adaptive: bool,
    block_size: int,
    c_val: int,
    invert: bool,
) -> None:
    whiten_and_save(
        png_path,
        out_path,
        threshold=threshold,
        use_adaptive=use_adaptive,
        block_size=block_size,
        c_val=c_val,
        invert=invert,
    )


def convert_images_to_pdf(
    image_paths: Sequence[Path | str], output_pdf: Path | str
) -> None:
    """Combine images into a single PDF via img2pdf."""
    paths: list[str] = [str(Path(path)) for path in image_paths]
    if not paths:
        raise ValueError("No image files supplied; cannot build PDF")

    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("wb") as file:
        file.write(img2pdf.convert(paths))


# ── Subcommand: enhance ──────────────────────────────────────────────


def cmd_enhance(
    input_pdf: Path | str,
    dpi: int = 150,
    threshold: int = 128,
    use_adaptive: bool = False,
    block_size: int = 31,
    c_val: int = 15,
    invert: bool = False,
    show_progress: bool = True,
    jobs: int = 0,
    rasterizer: Rasterizer = "pdftocairo",
) -> Path:
    """
    Rasterize a PDF and save enhanced (whitened) PNGs to ``<stem>-artifacts/``.
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

    # Derive book directory and original-copy path from the input
    book_dir = input_path.parent / input_path.stem
    original_copy = book_dir / f"{input_path.stem}.original.pdf"

    if not input_path.exists():
        # On re-run the PDF has already been moved into the book dir
        if original_copy.exists():
            input_path = original_copy
        else:
            raise FileNotFoundError(f"Input PDF not found: {input_path}")

    book_dir.mkdir(parents=True, exist_ok=True)

    if not original_copy.exists():
        shutil.move(str(input_path), original_copy)
        input_path = original_copy

    out_dir = book_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="betteria-pages-") as pages_dir_name:
        pages_dir = Path(pages_dir_name)

        png_paths = pdf_to_images(
            original_copy,
            dpi=dpi,
            out_dir=pages_dir,
            show_progress=show_progress,
            jobs=jobs,
            rasterizer=rasterizer,
        )

        if not png_paths:
            raise RuntimeError("No PNG pages generated from input PDF")

        width = len(str(len(png_paths)))
        enhanced_paths = [
            out_dir / f"page-{i + 1:0{width}d}.png"
            for i in range(len(png_paths))
        ]

        worker_target = jobs if jobs > 0 else _available_cpu_count()
        worker_target = min(worker_target, len(png_paths))

        if worker_target <= 1:
            with _progress(
                len(png_paths), "Enhancing images", show_progress
            ) as (progress, task_id):
                for png_path, enhanced_path in zip(png_paths, enhanced_paths):
                    whiten_and_save(
                        png_path,
                        enhanced_path,
                        threshold=threshold,
                        use_adaptive=use_adaptive,
                        block_size=block_size,
                        c_val=c_val,
                        invert=invert,
                    )
                    progress.advance(task_id, 1)
        else:
            with _progress(
                len(png_paths), "Enhancing images", show_progress
            ) as (progress_bar, task_id):
                with concurrent.futures.ProcessPoolExecutor(
                    max_workers=worker_target
                ) as executor:
                    futures = [
                        executor.submit(
                            _whiten_task,
                            str(png_path),
                            str(enhanced_path),
                            threshold,
                            use_adaptive,
                            block_size,
                            c_val,
                            invert,
                        )
                        for png_path, enhanced_path in zip(
                            png_paths, enhanced_paths
                        )
                    ]

                    for future in concurrent.futures.as_completed(futures):
                        future.result()
                        progress_bar.advance(task_id, 1)

    return book_dir


# ── Subcommand: ocr ──────────────────────────────────────────────────

_DEFAULT_OCR_MODEL = "mlx-community/PaddleOCR-VL-1.5-6bit"

# Module-level cache so the model is loaded once per process.
_ocr_model_cache: dict[str, tuple] = {}


def _load_ocr_model(model_path: str) -> tuple:
    """Load (or return cached) mlx-vlm model, processor, and config."""
    if model_path not in _ocr_model_cache:
        import warnings

        import huggingface_hub.utils

        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        import logging

        prev = huggingface_hub.utils.are_progress_bars_disabled()
        huggingface_hub.utils.disable_progress_bars()
        transformers_logger = logging.getLogger("transformers")
        prev_level = transformers_logger.level
        transformers_logger.setLevel(logging.ERROR)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model, processor = load(model_path)
                config = load_config(model_path)
        finally:
            transformers_logger.setLevel(prev_level)
            if not prev:
                huggingface_hub.utils.enable_progress_bars()

        _ocr_model_cache[model_path] = (model, processor, config)
    return _ocr_model_cache[model_path]


def _ocr_page(
    image_path: Path,
    model_path: str = _DEFAULT_OCR_MODEL,
) -> str:
    """OCR a single page image via mlx-vlm with PaddleOCR-VL."""
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor, config = _load_ocr_model(model_path)

    prompt = apply_chat_template(processor, config, "OCR:", num_images=1)
    result = generate(
        model,
        processor,
        prompt,
        [str(image_path)],
        max_tokens=4096,
        verbose=False,
    )
    return result.text


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:50].strip("-")


# Patterns that signal a chapter boundary at the start of a page.
_CHAPTER_PATTERNS = [
    # "Chapter 1", "CHAPTER I", "Chapter One"
    re.compile(
        r"^\s*chapter\s+(\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
        r"eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
        r"seventeen|eighteen|nineteen|twenty)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "Part 1", "PART II"
    re.compile(
        r"^\s*part\s+(\d+|[ivxlcdm]+|one|two|three|four|five)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
]


def _strip_headers_footers(page_texts: list[str]) -> list[str]:
    """Remove repeated running headers and page-number footers.

    Headers are detected by counting which first-lines appear on many
    pages (>20%).  Footers are standalone numbers on the last non-blank
    line.
    """
    if not page_texts:
        return page_texts

    # --- detect repeated headers ---
    first_lines: dict[str, int] = {}
    for text in page_texts:
        line = text.strip().splitlines()[0].strip() if text.strip() else ""
        if line:
            first_lines[line] = first_lines.get(line, 0) + 1

    threshold = max(3, len(page_texts) // 5)  # at least 20% of pages
    header_strings = {line for line, cnt in first_lines.items() if cnt >= threshold}

    # --- strip ---
    cleaned: list[str] = []
    for text in page_texts:
        lines = text.strip().splitlines()

        # Remove header (first non-blank line if it matches a known header)
        while lines:
            top = lines[0].strip()
            if not top:
                lines.pop(0)
                continue
            if top in header_strings:
                lines.pop(0)
            break

        # Remove footer (last non-blank line if it is a standalone number)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and re.fullmatch(r"\d{1,4}", lines[-1].strip()):
            lines.pop()

        cleaned.append("\n".join(lines).strip())

    return cleaned


def _detect_chapters(page_texts: list[str]) -> dict:
    """Detect chapter boundaries from OCR'd text using heuristics.

    Headers and footers are stripped first so that running headers
    (e.g. the book title) and page numbers do not cause false matches.
    """
    stripped = _strip_headers_footers(page_texts)
    chapters: list[dict] = []

    for page_idx, text in enumerate(stripped):
        # Check the first few lines of each page for chapter headings
        first_lines = text[:300]
        for pattern in _CHAPTER_PATTERNS:
            match = pattern.search(first_lines)
            if match:
                # Use the matched line as the title
                line = text.splitlines()[
                    text[: match.start()].count("\n")
                ].strip()
                chapters.append({
                    "number": len(chapters) + 1,
                    "title": line,
                    "start_page": page_idx + 1,  # 1-indexed
                })
                break

    if not chapters:
        chapters = [{"number": 1, "title": "Full Text", "start_page": 1}]

    return {"title": "", "author": "", "chapters": chapters}


def cmd_ocr(
    input_dir: Path | str,
    model: str = _DEFAULT_OCR_MODEL,
    show_progress: bool = True,
) -> Path:
    """OCR enhanced PNGs and save per-page text files.

    Per-page OCR results are saved as ``.txt`` files next to each PNG
    (e.g. ``page-001.txt``).  Pages that already have a ``.txt`` file are
    skipped, so the command is safe to re-run after partial completion or
    after manually editing individual page texts.
    """
    try:
        import mlx_vlm  # noqa: F401
    except ImportError:
        raise SystemExit(
            "The 'ocr' command requires mlx-vlm, which is not installed.\n"
            "Install it with: uv sync --extra ocr"
        )

    input_path = Path(input_dir)
    if not input_path.is_dir():
        raise NotADirectoryError(f"Input must be a directory: {input_path}")

    artifacts_dir = input_path / "artifacts"
    if artifacts_dir.is_dir():
        artifacts_dir = artifacts_dir
    else:
        artifacts_dir = input_path

    png_paths = sorted(artifacts_dir.glob("*.png"), key=_page_sort_key)
    if not png_paths:
        raise RuntimeError(f"No PNG files found in {artifacts_dir}")

    # Determine which pages need OCR (skip those with existing .txt)
    todo: list[tuple[Path, Path]] = []  # (png, txt) pairs needing OCR
    for png_path in png_paths:
        txt_path = png_path.with_suffix(".txt")
        if not txt_path.exists():
            todo.append((png_path, txt_path))

    console = Console(stderr=True)

    if todo:
        skipped = len(png_paths) - len(todo)
        if skipped:
            console.print(
                f"[dim]Skipping {skipped} pages with existing text.[/dim]"
            )

        console.print(f"[dim]Loading OCR model {model}...[/dim]")
        _load_ocr_model(model)

        skipped = len(png_paths) - len(todo)
        with _progress(len(png_paths), "OCR processing", show_progress) as (
            progress,
            task_id,
        ):
            progress.advance(task_id, skipped)
            for png_path, txt_path in todo:
                text = _ocr_page(png_path, model_path=model)
                txt_path.write_text(text, encoding="utf-8")
                progress.advance(task_id, 1)
    else:
        console.print("[dim]All pages already have OCR text.[/dim]")

    return input_path


# ── Subcommand: merge ────────────────────────────────────────────────

# Adapted from Standard Ebooks (standardebooks.org) core.css
_EPUB_CSS = """\
@charset "utf-8";
@namespace epub "http://www.idpf.org/2007/ops";

body{
	hyphens: auto;
	-epub-hyphens: auto;
	font-variant-numeric: oldstyle-nums;
	text-wrap: pretty;
}

p{
	margin: 0;
	text-indent: 1em;
}

h1,
h2,
h3,
h4,
h5,
h6{
	break-after: avoid;
	break-inside: avoid;
	font-variant: small-caps;
	hyphens: none;
	-epub-hyphens: none;
	margin: 3em 0;
	text-align: center;
}

h2 + p,
h3 + p,
h4 + p,
h5 + p,
h6 + p,
hr + p,
header + p,
p:first-child{
	text-indent: 0;
}

hr{
	border: none;
	border-top: 1px solid;
	height: 0;
	margin: 1.5em auto;
	width: 25%;
}

blockquote{
	margin: 1em 2.5em;
}

blockquote cite{
	display: block;
	font-style: italic;
	text-align: right;
}

ol,
ul{
	margin-bottom: 1em;
	margin-top: 1em;
}

abbr{
	border: none;
	white-space: nowrap;
}

cite{
	font-style: normal;
}

i > i,
em > i,
i > em{
	font-style: normal;
}
"""


def _text_to_html(text: str) -> str:
    """Convert Markdown text to HTML."""
    import mistune

    return mistune.html(text)


def cmd_merge(
    input_dir: Path | str,
    title: str | None = None,
    author: str | None = None,
    epub_only: bool = False,
    pdf_only: bool = False,
    show_progress: bool = True,
) -> tuple[Path | None, Path | None]:
    """Build EPUB from proofread chapters and/or enhanced PDF from PNGs.

    Accepts the book directory.  EPUB is only generated if a ``chapters/``
    subdirectory exists containing proofread chapter ``.txt`` files.  PDF is
    generated from PNGs in the ``artifacts/`` subdirectory.
    """
    input_path = Path(input_dir)
    if not input_path.is_dir():
        raise NotADirectoryError(f"Input must be a directory: {input_path}")

    book_dir = input_path
    stem = book_dir.name

    chapters_dir = book_dir / "chapters"
    metadata_path = book_dir / "metadata.json"
    pngs_dir = book_dir / "artifacts"
    epub_path = book_dir / f"{stem}.epub"
    pdf_path = book_dir / f"{stem}.pdf"

    console = Console(stderr=True)

    epub_out = None
    pdf_out = None

    # ── EPUB (only if *-chapters/ exists with proofread text) ──
    if not pdf_only:
        chapter_files = sorted(chapters_dir.glob("*.md")) if chapters_dir.is_dir() else []
        if not chapter_files:
            chapter_files = sorted(chapters_dir.glob("*.txt")) if chapters_dir.is_dir() else []
        if chapter_files:
            from ebooklib import epub

            # Read metadata if available
            if metadata_path.exists():
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            else:
                meta = {"title": "", "author": "", "chapters": []}

            book_title = title or meta.get("title", "") or stem
            book_author = author or meta.get("author", "") or ""

            book = epub.EpubBook()
            book.set_identifier(f"betteria-{stem}")
            book.set_title(book_title)
            book.set_language("en")
            if book_author:
                book.add_author(book_author)

            # Cover image
            cover_path = None
            for ext in ("png", "jpg", "jpeg"):
                candidate = book_dir / f"cover.{ext}"
                if candidate.exists():
                    cover_path = candidate
                    break
            if cover_path:
                book.set_cover(f"cover{cover_path.suffix}", cover_path.read_bytes())

            style = epub.EpubItem(
                uid="style",
                file_name="style/default.css",
                media_type="text/css",
                content=_EPUB_CSS.encode("utf-8"),
            )
            book.add_item(style)

            epub_chapters = []
            chapters_meta = meta.get("chapters", [])

            if chapters_meta:
                for ch in chapters_meta:
                    filepath = chapters_dir / ch["file"]
                    if not filepath.exists():
                        continue
                    text = filepath.read_text(encoding="utf-8")
                    ch_title = (ch.get("title") or "").strip()
                    has_heading = bool(re.match(r"\s*#{1,6}\s+", text))
                    if not ch_title:
                        if has_heading:
                            m = re.match(r"\s*#{1,6}\s+(.*)", text)
                            ch_title = m.group(1).strip() if m else ""
                        if not ch_title:
                            # Use first few words of the chapter text as title
                            plain = re.sub(r"[#*>\[\]`]", "", text).strip()
                            words = plain.split()[:6]
                            ch_title = " ".join(words).rstrip(".,;:!?") + "…"
                    epub_ch = epub.EpubHtml(
                        title=ch_title,
                        file_name=f"ch_{ch.get('number', 0):03d}.xhtml",
                        lang="en",
                    )
                    if has_heading:
                        body = re.sub(r"\A\s*#{1,6}\s+[^\n]*\n*", "", text)
                        html = _text_to_html(body)
                        epub_ch.content = f"<h1>{ch_title}</h1>\n{html}"
                    else:
                        epub_ch.content = _text_to_html(text)
                    epub_ch.add_item(style)
                    book.add_item(epub_ch)
                    epub_chapters.append(epub_ch)
            else:
                for i, ch_file in enumerate(chapter_files, 1):
                    text = ch_file.read_text(encoding="utf-8")
                    has_heading = bool(re.match(r"\s*#{1,6}\s+", text))
                    ch_title = (
                        ch_file.stem.lstrip("0123456789-").replace("-", " ").strip()
                    )
                    ch_title = ch_title.title() if ch_title else f"Chapter {i}"
                    epub_ch = epub.EpubHtml(
                        title=ch_title,
                        file_name=f"ch_{i:03d}.xhtml",
                        lang="en",
                    )
                    if has_heading:
                        body = re.sub(r"\A\s*#{1,6}\s+[^\n]*\n*", "", text)
                        epub_ch.content = f"<h1>{ch_title}</h1>\n{_text_to_html(body)}"
                    else:
                        epub_ch.content = _text_to_html(text)
                    epub_ch.add_item(style)
                    book.add_item(epub_ch)
                    epub_chapters.append(epub_ch)

            book.toc = epub_chapters
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.spine = ["nav"] + epub_chapters

            epub.write_epub(str(epub_path), book)
            epub_out = epub_path
        else:
            console.print(
                f"[yellow]No {chapters_dir.name}/ directory found; skipping EPUB. "
                "Proofread per-page texts and create chapter files first.[/yellow]"
            )

    # ── PDF ──
    if not epub_only:
        if pngs_dir.is_dir():
            png_paths = sorted(pngs_dir.glob("*.png"), key=_page_sort_key)
            if png_paths:
                convert_images_to_pdf(png_paths, pdf_path)
                pdf_out = pdf_path
            else:
                console.print(
                    f"[yellow]No PNG files found in {pngs_dir}; skipping PDF.[/yellow]"
                )
        else:
            console.print(
                f"[yellow]{pngs_dir} not found; skipping PDF.[/yellow]"
            )

    return epub_out, pdf_out


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="betteria",
        description="OCR and EPUB pipeline for scanned PDFs.",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command")

    # ── enhance ──
    p_enhance = subparsers.add_parser(
        "enhance",
        help="Rasterize and enhance a scanned PDF into clean PNGs.",
    )
    p_enhance.add_argument("input", help="Path to input PDF")
    p_enhance.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI for rasterizing PDF pages (default: 150)",
    )
    p_enhance.add_argument(
        "--threshold",
        type=int,
        default=128,
        help="Global threshold value 0-255 (default: 128; ignored when adaptive)",
    )
    p_enhance.add_argument(
        "--block-size",
        type=int,
        default=31,
        help="Neighborhood size for adaptive thresholding (default: 31)",
    )
    p_enhance.add_argument(
        "--c-val",
        type=int,
        default=15,
        help="Constant for adaptive thresholding (default: 15)",
    )
    p_enhance.add_argument(
        "--adaptive",
        action="store_true",
        default=True,
        help="Use adaptive thresholding (default: on)",
    )
    p_enhance.add_argument(
        "--invert",
        action="store_true",
        help="Invert pixels before thresholding",
    )
    p_enhance.add_argument(
        "--quiet",
        action="store_true",
        help="Disable progress bars",
    )
    p_enhance.add_argument(
        "--jobs",
        type=_coerce_jobs,
        default=0,
        help="Parallel workers ('auto'/0 = all cores; 1 = single thread)",
    )
    p_enhance.add_argument(
        "--rasterizer",
        choices=["pdftoppm", "pdftocairo"],
        default="pdftocairo",
        help="Poppler backend (default: pdftocairo)",
    )

    # ── ocr ──
    p_ocr = subparsers.add_parser(
        "ocr",
        help="OCR enhanced PNGs into per-page text files.",
    )
    p_ocr.add_argument("input", help="Path to book directory")
    p_ocr.add_argument(
        "--model",
        default=_DEFAULT_OCR_MODEL,
        help=f"mlx-vlm model for OCR (default: {_DEFAULT_OCR_MODEL})",
    )
    p_ocr.add_argument(
        "--quiet",
        action="store_true",
        help="Disable progress bars",
    )

    # ── merge ──
    p_merge = subparsers.add_parser(
        "merge",
        help="Build EPUB from proofread chapters and/or enhanced PDF from PNGs.",
    )
    p_merge.add_argument("input", help="Path to book directory")
    p_merge.add_argument(
        "--title",
        default=None,
        help="Override book title from metadata",
    )
    p_merge.add_argument(
        "--author",
        default=None,
        help="Override author from metadata",
    )
    p_merge.add_argument(
        "--epub-only",
        action="store_true",
        help="Only generate EPUB (skip PDF)",
    )
    p_merge.add_argument(
        "--pdf-only",
        action="store_true",
        help="Only generate PDF (skip EPUB)",
    )
    p_merge.add_argument(
        "--quiet",
        action="store_true",
        help="Disable progress bars",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    console = Console(stderr=True)

    if args.command == "enhance":
        out_dir = cmd_enhance(
            input_pdf=args.input,
            dpi=args.dpi,
            threshold=args.threshold,
            use_adaptive=args.adaptive,
            block_size=args.block_size,
            c_val=args.c_val,
            invert=args.invert,
            show_progress=not args.quiet,
            jobs=args.jobs,
            rasterizer=args.rasterizer,
        )
        console.print(f"[green]Enhanced PNGs saved to {out_dir}[/green]")

    elif args.command == "ocr":
        out_dir = cmd_ocr(
            input_dir=args.input,
            model=args.model,
            show_progress=not args.quiet,
        )
        console.print(f"[green]OCR text saved to {out_dir}[/green]")

    elif args.command == "merge":
        epub_out, pdf_out = cmd_merge(
            input_dir=args.input,
            title=args.title,
            author=args.author,
            epub_only=args.epub_only,
            pdf_only=args.pdf_only,
            show_progress=not args.quiet,
        )
        if epub_out:
            console.print(f"[green]EPUB saved to {epub_out}[/green]")
        if pdf_out:
            console.print(f"[green]PDF saved to {pdf_out}[/green]")


if __name__ == "__main__":
    main()
