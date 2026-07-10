from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import html as html_mod
import json
import os
import platform
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


def _get_page_long_side_pts(pdf_path: Path | str) -> float | None:
    """Return the long side of the (uniform) page in points, or ``None``.

    Returns ``None`` if pdfinfo is unavailable, the size cannot be parsed,
    or the PDF reports variable page sizes.
    """
    try:
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    for line in result.stdout.splitlines():
        if line.lower().startswith("page size:"):
            value = line.split(":", 1)[1]
            match = re.search(r"([\d.]+)\s*x\s*([\d.]+)\s*pts", value)
            if match:
                return max(float(match.group(1)), float(match.group(2)))
            return None
    return None


def _page_sort_key(path: Path) -> int:
    stem = path.stem
    for part in reversed(stem.split("-")):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            return int(digits)
    return sys.maxsize


def _spread_reading_order(total_pages: int, lead: int = 1) -> list[int]:
    """Map each output slot (0-based) to its source PDF page index (0-based).

    Some right-to-left books (e.g. Japanese) are distributed as PDFs whose
    page pairs are swapped, so that a left-to-right two-up viewer renders the
    spreads correctly when read right-to-left.  Read as single pages the
    printed folios then run 11, 10, 13, 12, 15, 14, ...  This undoes the
    swap: ``lead`` leading single pages (typically a standalone cover) are
    kept in place, every following adjacent pair is swapped, and a leftover
    trailing page (e.g. a back cover) is kept in place.  ``lead=1`` matches
    the common cover + spreads + back-cover layout.
    """
    lead = max(0, min(lead, total_pages))
    order = list(range(lead))
    i = lead
    while i + 1 < total_pages:
        order.append(i + 1)
        order.append(i)
        i += 2
    if i < total_pages:
        order.append(i)
    return order


def _resolve_page_order(
    book_dir: Path,
    total_pages: int,
    ppd: str,
    console: Console | None = None,
) -> list[int]:
    """Return the output-slot -> source-page permutation for a book.

    The resolved order is persisted to ``<book_dir>/page_order.json`` when it
    is non-trivial, so an interrupted run resumed later reuses the same order
    instead of silently mixing reading directions.  A previously saved order
    always wins (and is validated against the current page count).
    """
    order_path = book_dir / "page_order.json"
    if order_path.exists():
        try:
            data = json.loads(order_path.read_text(encoding="utf-8"))
            saved = data.get("order", [])
        except (json.JSONDecodeError, OSError):
            saved = []
        if sorted(saved) == list(range(total_pages)):
            if (
                console is not None
                and ppd == "ltr"
                and data.get("ppd") == "rtl"
            ):
                console.print(
                    "[yellow]Reusing saved RTL page order from "
                    "page_order.json (ignoring --ppd ltr).[/yellow]"
                )
            return saved
        # Stale marker (page count changed); fall through and recompute.

    if ppd == "rtl":
        order = _spread_reading_order(total_pages, lead=1)
        order_path.write_text(
            json.dumps(
                {
                    "ppd": "rtl",
                    "lead": 1,
                    "total_pages": total_pages,
                    "order": order,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return order

    return list(range(total_pages))


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


# ── Searchable-PDF text layer ────────────────────────────────────────
#
# The enhanced PDF is a stack of page images with no text.  To make it
# searchable we slip an *invisible* text layer (PDF text-rendering mode 3)
# over each image, carrying the proofread (corrected) words.  The proofread
# files are reflowed and have no coordinates, so positions come from a fresh
# Tesseract pass on the same image: align the raw OCR tokens to the proofread
# tokens, then place each *corrected* token at the matching OCR box.  Latin
# scripts align word-by-word; CJK has no word spaces, so it aligns character
# by character — Tesseract's per-glyph boxes already make that 1:1, and each
# glyph is positioned absolutely, so vertical text needs no layout engine.

# BCP-47 (metadata.json "language") → Tesseract language code.
_TESSERACT_LANG = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
    "pt": "por", "nl": "nld", "la": "lat", "ru": "rus",
    "ja": "jpn", "zh": "chi_sim", "ko": "kor",
}

# Scripts written without spaces between words → align per character.
_CJK_LANGS = frozenset({"ja", "zh", "ko"})

# BCP-47 → a built-in reportlab CID font covering the script, so the invisible
# layer extracts/searches correctly without shipping a TTF.
_CID_FONT = {
    "ja": "HeiseiMin-W3",
    "zh": "STSong-Light",
    "ko": "HYSMyeongJo-Medium",
}

# Vertical (V) CMaps: setting one marks the text as vertical writing mode
# (WMode 1), so even geometric extractors read columns top-to-bottom,
# right-to-left.  Input must be UCS-2 (handled by ``_encode_for_font``).
_CID_VCMAP = {
    "ja": "UniJIS-UCS2-V",
    "zh": "UniGB-UCS2-V",
    "ko": "UniKS-UCS2-V",
}


def _base_lang(lang: str) -> str:
    """Primary subtag of a BCP-47 code: ``ja-JP`` → ``ja``."""
    return (lang or "en").split("-")[0].lower()


def _tesseract_config(lang: str, vertical: bool) -> tuple[str, int, bool]:
    """Return ``(tesseract_lang, psm, is_cjk)`` for a BCP-47 language code."""
    base = _base_lang(lang)
    cjk = base in _CJK_LANGS
    tess = _TESSERACT_LANG.get(base, "eng")
    if cjk and vertical:
        return f"{tess}_vert", 5, True   # single uniform block, vertical text
    if cjk:
        return tess, 6, True             # single uniform block, horizontal text
    return tess, 3, False                # fully automatic page segmentation


def _overlay_font(lang: str, vertical: bool) -> tuple[str, bool]:
    """Register (once) a reportlab font for ``lang`` and return it.

    Returns ``(font_name, needs_ucs2)``.  ``needs_ucs2`` is True for the
    vertical CID fonts, whose CMaps consume UCS-2 rather than Python ``str``.
    """
    from reportlab.pdfbase import pdfmetrics

    base = _base_lang(lang)
    face = _CID_FONT.get(base)
    if face is None:
        return "Helvetica", False
    if vertical:
        name = f"{face}-{_CID_VCMAP[base]}"
        try:
            pdfmetrics.getFont(name)
        except KeyError:
            from reportlab.pdfbase.cidfonts import CIDFont
            pdfmetrics.registerFont(CIDFont(face, _CID_VCMAP[base]))
        return name, True
    try:
        pdfmetrics.getFont(face)
    except KeyError:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont(face))
    return face, False


_MD_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_INLINE_RE = re.compile(r"[*_`#>]")


def _markdown_to_plaintext(text: str) -> str:
    """Strip Markdown, boundary markers, and sentinels from a proofread page.

    Proofread files carry Markdown (headings, emphasis, blockquotes),
    ``<!--JOIN-->``/``<!--PARA-->`` boundary markers, and the occasional
    ``[BLANK PAGE]`` sentinel.  None of that belongs in a layer whose only
    job is to make the printed words searchable and copyable.
    """
    text = _MD_COMMENT_RE.sub(" ", text)
    kept = [
        line for line in text.splitlines()
        if line.strip() not in {"[BLANK PAGE]", "---"}
    ]
    return _MD_INLINE_RE.sub("", "\n".join(kept))


def _proofread_units(text: str, cjk: bool) -> list[str]:
    """Split plaintext into alignment units: words (Latin) or chars (CJK)."""
    plain = _markdown_to_plaintext(text)
    if cjk:
        return [ch for ch in plain if not ch.isspace()]
    return plain.split()


def _normalize_unit(unit: str, cjk: bool) -> str:
    """Fold a unit for fuzzy matching without altering what gets placed."""
    if cjk:
        return unit
    folded = unit.lower().replace("’", "'").replace("‘", "'")
    return folded.strip("“”\"'.,;:!?()[]{}—–-…")


def _tesseract_tokens(
    image_path: Path, lang: str, psm: int, vertical: bool, split_chars: bool
) -> list[dict]:
    """Run Tesseract and return word/char boxes in image-pixel coordinates.

    Each token is ``{"text", "x", "y", "w", "h"}``.  For CJK most tokens are
    already single glyphs; any multi-glyph token is split into equal cells
    along the reading axis so alignment stays 1:1 with proofread characters.
    """
    try:
        proc = subprocess.run(
            ["tesseract", str(image_path), "stdout",
             "-l", lang, "--psm", str(psm), "tsv"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    tokens: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":   # level 5 == word
            continue
        word = parts[11].strip()
        if not word:
            continue
        col = (parts[2], parts[3], parts[4])      # block, paragraph, line
        x, y, w, h = int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9])
        chars = list(word)
        if not split_chars or len(chars) == 1:
            tokens.append({"text": word, "x": x, "y": y, "w": w, "h": h,
                           "col": col})
            continue
        n = len(chars)
        if vertical:
            cell = h / n
            for k, ch in enumerate(chars):
                tokens.append({"text": ch, "x": x, "y": y + round(k * cell),
                               "w": w, "h": max(1, round(cell)), "col": col})
        else:
            cell = w / n
            for k, ch in enumerate(chars):
                tokens.append({"text": ch, "x": x + round(k * cell), "y": y,
                               "w": max(1, round(cell)), "h": h, "col": col})
    return tokens


def _distribute_units(
    boxes: list[dict], units: list[str]
) -> list[tuple[dict, str]]:
    """Spread ``units`` across the span of ``boxes`` (for replace blocks)."""
    if not boxes:
        return []
    if len(boxes) == len(units):
        return list(zip(boxes, units))
    x0 = min(b["x"] for b in boxes)
    x1 = max(b["x"] + b["w"] for b in boxes)
    y0 = min(b["y"] for b in boxes)
    y1 = max(b["y"] + b["h"] for b in boxes)
    col = boxes[0].get("col")
    n = max(1, len(units))
    placed: list[tuple[dict, str]] = []
    if (y1 - y0) >= (x1 - x0):          # vertical run of glyphs
        cell = (y1 - y0) / n
        for k, u in enumerate(units):
            placed.append(({"x": x0, "y": y0 + round(k * cell),
                            "w": x1 - x0, "h": max(1, round(cell)),
                            "col": col}, u))
    else:                                # horizontal run
        cell = (x1 - x0) / n
        for k, u in enumerate(units):
            placed.append(({"x": x0 + round(k * cell), "y": y0,
                            "w": max(1, round(cell)), "h": y1 - y0,
                            "col": col}, u))
    return placed


def _align_tokens(
    ocr_tokens: list[dict], units: list[str], cjk: bool
) -> list[tuple[dict, str]]:
    """Align OCR boxes to proofread units, carrying corrected text onto boxes.

    OCR-only runs (running headers, page numbers, furigana, noise) are
    dropped; proofread-only runs (text Tesseract missed) are anchored to the
    neighbouring box so they stay searchable.
    """
    import difflib

    ocr_norm = [_normalize_unit(t["text"], cjk) for t in ocr_tokens]
    unit_norm = [_normalize_unit(u, cjk) for u in units]
    matcher = difflib.SequenceMatcher(None, ocr_norm, unit_norm, autojunk=False)
    placed: list[tuple[dict, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                placed.append((ocr_tokens[i1 + k], units[j1 + k]))
        elif tag == "replace":
            placed.extend(_distribute_units(ocr_tokens[i1:i2], units[j1:j2]))
        elif tag == "insert":
            anchor = placed[-1][0] if placed else (
                ocr_tokens[i1] if i1 < len(ocr_tokens) else None)
            if anchor is not None:
                placed.extend((anchor, u) for u in units[j1:j2])
        # tag == "delete": OCR-only → drop
    return placed


def _render_text_layer(
    page_w_pt: float, page_h_pt: float,
    img_w_px: int, img_h_px: int,
    placed: list[tuple[dict, str]],
    font_name: str, needs_ucs2: bool, vertical: bool,
) -> bytes:
    """Render placed units as an invisible single-page overlay PDF.

    Glyphs use text-rendering mode 3 (invisible but selectable/searchable).
    Horizontal text is width-fitted to its box via horizontal scaling; vertical
    text is placed glyph-by-glyph down each column using a vertical-CMap font.
    """
    import statistics
    from io import BytesIO

    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    sx = page_w_pt / img_w_px
    sy = page_h_pt / img_h_px
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(page_w_pt, page_h_pt))

    def emit(tobj: object, text: str) -> None:
        tobj.textOut(text.encode("utf-16-be").decode("latin-1") if needs_ucs2 else text)

    if vertical:
        for boxes, text in _group_columns(placed):
            size = max(statistics.median(b["h"] for b in boxes) * sy, 1.0)
            cx = statistics.median(b["x"] + b["w"] / 2.0 for b in boxes) * sx
            y_top = page_h_pt - min(b["y"] for b in boxes) * sy
            tobj = pdf.beginText()
            tobj.setTextRenderMode(3)
            tobj.setFont(font_name, size)
            tobj.setTextOrigin(cx, y_top)
            emit(tobj, text)
            pdf.drawText(tobj)
    else:
        for box, text in placed:
            if not text:
                continue
            size = max(box["h"] * sy, 1.0)
            natural = stringWidth(text, font_name, size) or size
            target = max(box["w"] * sx, 1.0)
            tobj = pdf.beginText()
            tobj.setTextRenderMode(3)
            tobj.setFont(font_name, size)
            tobj.setHorizScale(max(10.0, min((target / natural) * 100.0, 1000.0)))
            tobj.setTextOrigin(
                box["x"] * sx, page_h_pt - (box["y"] + box["h"]) * sy
            )
            emit(tobj, text)
            pdf.drawText(tobj)

    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _group_columns(
    placed: list[tuple[dict, str]],
) -> list[tuple[list[dict], str]]:
    """Group consecutive placed glyphs into columns (by Tesseract line id).

    ``placed`` is already in reading order, so each column's glyphs are
    contiguous.  Emitting one text run per column keeps geometric extractors
    from interleaving adjacent columns of vertical text.
    """
    runs: list[tuple[list[dict], list[str]]] = []
    prev = object()
    for box, ch in placed:
        if not ch:
            continue
        col = box.get("col")
        if not runs or col is None or col != prev:
            runs.append(([box], [ch]))
        else:
            runs[-1][0].append(box)
            runs[-1][1].append(ch)
        prev = col
    return [(boxes, "".join(chars)) for boxes, chars in runs]


def _page_text_for(png_path: Path, artifacts_dir: Path) -> str:
    """Best proofread text for a page image (proofread > raw OCR; spreads joined)."""
    stem = png_path.stem
    halves: list[str] = []
    for side in ("L", "R"):
        proof = artifacts_dir / f"{stem}-{side}.proofread.txt"
        raw = artifacts_dir / f"{stem}-{side}.txt"
        if proof.exists():
            halves.append(proof.read_text(encoding="utf-8"))
        elif raw.exists():
            halves.append(raw.read_text(encoding="utf-8"))
    if halves:
        return "\n\n".join(halves)
    proof = artifacts_dir / f"{stem}.proofread.txt"
    if proof.exists():
        return proof.read_text(encoding="utf-8")
    raw = artifacts_dir / f"{stem}.txt"
    if raw.exists():
        return raw.read_text(encoding="utf-8")
    return ""


def convert_images_to_pdf(
    image_paths: Sequence[Path | str],
    output_pdf: Path | str,
    page_texts: Sequence[str] | None = None,
    lang: str = "en",
    vertical: bool = False,
    jobs: int = 0,
    show_progress: bool = False,
) -> None:
    """Combine images into a single PDF via img2pdf.

    When ``page_texts`` is given (one entry per image, in order), an invisible
    searchable text layer is built for each page by aligning the proofread
    text to a fresh Tesseract pass on the image.  Without it, or if Tesseract
    is unavailable, the result is the plain image-only PDF as before.
    """
    paths: list[str] = [str(Path(path)) for path in image_paths]
    if not paths:
        raise ValueError("No image files supplied; cannot build PDF")

    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)

    pdf_bytes = img2pdf.convert(paths)

    texts = list(page_texts or [])
    want_text = any((t or "").strip() for t in texts)
    if not want_text or shutil.which("tesseract") is None:
        output.write_bytes(pdf_bytes)
        return

    from io import BytesIO

    import pikepdf

    tess_lang, psm, cjk = _tesseract_config(lang, vertical)
    font_name, needs_ucs2 = _overlay_font(lang, vertical)

    pdf = pikepdf.open(BytesIO(pdf_bytes))
    n_pages = len(pdf.pages)

    # Page geometry: points from the PDF, pixels from the source image.
    geom: list[tuple[float, float, int, int]] = []
    for idx, page in enumerate(pdf.pages):
        mbox = page.mediabox
        w_pt = float(mbox[2]) - float(mbox[0])
        h_pt = float(mbox[3]) - float(mbox[1])
        with Image.open(paths[idx]) as img:
            img_w, img_h = img.size
        geom.append((w_pt, h_pt, img_w, img_h))

    def _overlay_for(idx: int) -> tuple[int, bytes | None]:
        text = texts[idx] if idx < len(texts) else ""
        if not text or not text.strip():
            return idx, None
        ocr = _tesseract_tokens(Path(paths[idx]), tess_lang, psm, vertical, cjk)
        units = _proofread_units(text, cjk)
        if not ocr or not units:
            return idx, None
        placed = _align_tokens(ocr, units, cjk)
        if not placed:
            return idx, None
        w_pt, h_pt, img_w, img_h = geom[idx]
        return idx, _render_text_layer(
            w_pt, h_pt, img_w, img_h, placed, font_name, needs_ucs2, vertical
        )

    worker_target = jobs or _available_cpu_count()
    overlays: dict[int, bytes] = {}
    with _progress(n_pages, "Embedding text layer", show_progress) as (
        bar,
        task_id,
    ):
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_target
        ) as executor:
            futures = {
                executor.submit(_overlay_for, idx): idx for idx in range(n_pages)
            }
            for future in concurrent.futures.as_completed(futures):
                idx, data = future.result()
                if data is not None:
                    overlays[idx] = data
                bar.advance(task_id, 1)

    for idx, data in overlays.items():
        overlay = pikepdf.open(BytesIO(data))
        pdf.pages[idx].add_overlay(overlay.pages[0])

    pdf.save(str(output))


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
    ppd: str = "ltr",
    extract_text: bool = True,
) -> Path:
    """Rasterize a PDF and save enhanced (whitened) PNGs to ``<stem>/artifacts/``.

    Accepts either a PDF file or an existing book directory.  Pages that
    already have an enhanced PNG in ``artifacts/`` are skipped, so the
    command is safe to re-run after interruption.

    When ``extract_text`` is set (the default), any embedded text the source
    PDF carries is written to per-page ``.txt`` files too, exactly as
    ``extract`` does — a scanned PDF with no text layer simply yields none.
    Existing ``.txt`` files are never overwritten; replace poor embedded text
    with a fresh OCR pass via ``betteria ocr --override``.
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
    console = Console(stderr=True)

    if input_path.is_dir():
        # Directory input: resume from an existing book directory
        book_dir = input_path
        originals = list(book_dir.glob("*.original.pdf"))
        if not originals:
            raise FileNotFoundError(
                f"No .original.pdf found in {book_dir}. "
                "Pass a PDF file for first-time enhancement."
            )
        if len(originals) > 1:
            raise ValueError(
                f"Multiple .original.pdf files in {book_dir}: {originals}"
            )
        original_copy = originals[0]
    else:
        # PDF file input
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

    out_dir = book_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine total page count and which pages still need enhancement
    total_pages = get_page_count(original_copy)
    width = len(str(total_pages))
    enhanced_paths = [
        out_dir / f"page-{i + 1:0{width}d}.png"
        for i in range(total_pages)
    ]
    # Output slot i is filled from source PDF page ``order[i]`` (identity for
    # ``ppd == "ltr"``; a spread de-interleave for ``ppd == "rtl"``).
    order = _resolve_page_order(book_dir, total_pages, ppd, console)

    # Parity with `extract`: pull any text the source PDF already carries into
    # per-page .txt files.  Runs independently of the PNG work (and before the
    # "all enhanced" early return) and skips pages that already have a .txt, so
    # OCR output or hand edits survive a re-run.  A scan with no text layer
    # yields none; replace poor embedded text with `betteria ocr --override`.
    if extract_text:
        pages_with_text = _extract_text_layer(
            original_copy,
            enhanced_paths,
            order,
            show_progress=show_progress,
            skip_existing=True,
        )
        if pages_with_text and show_progress:
            console.print(
                f"[dim]Pulled embedded text from {pages_with_text} of "
                f"{total_pages} pages; run `ocr --override` to replace it.[/dim]"
            )

    pages_todo = [i for i, ep in enumerate(enhanced_paths) if not ep.exists()]

    if not pages_todo:
        console.print("[dim]All pages already enhanced.[/dim]")
        return book_dir

    skipped = total_pages - len(pages_todo)
    if skipped and show_progress:
        console.print(
            f"[dim]Skipping {skipped} pages with existing enhanced PNGs.[/dim]"
        )

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

        # Pair rasterized pages with their enhanced output paths, keep only
        # TODO.  ``order[i]`` selects which source page lands in slot ``i``.
        todo: list[tuple[Path, Path]] = [
            (png_paths[order[i]], enhanced_paths[i]) for i in pages_todo
        ]

        worker_target = jobs if jobs > 0 else _available_cpu_count()
        worker_target = min(worker_target, len(todo))

        if worker_target <= 1:
            with _progress(
                total_pages, "Enhancing images", show_progress
            ) as (progress, task_id):
                progress.advance(task_id, skipped)
                for png_path, enhanced_path in todo:
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
                total_pages, "Enhancing images", show_progress
            ) as (progress_bar, task_id):
                progress_bar.advance(task_id, skipped)
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
                        for png_path, enhanced_path in todo
                    ]

                    for future in concurrent.futures.as_completed(futures):
                        future.result()
                        progress_bar.advance(task_id, 1)

    return book_dir


# ── Subcommand: extract ───────────────────────────────────────────────


def _extract_text_page(
    pdf_path: Path, page: int, txt_path: Path
) -> bool:
    """Extract embedded text from a single PDF page via ``pdftotext``.

    Returns ``True`` when the page carried embedded text and a ``.txt`` file
    was written, ``False`` when it had none.  A page with no text layer (e.g.
    a scanned/image-only PDF) yields only a form-feed from ``pdftotext``; in
    that case no file is written, so the artifacts dir isn't littered with
    empty ``.txt`` files and a later ``ocr`` run isn't blocked (``ocr`` skips
    pages that already have a ``.txt``).
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-f", str(page), "-l", str(page), str(pdf_path), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Poppler's 'pdftotext' not found. Install Poppler or add it to PATH."
        ) from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Error running pdftotext for page {page}: {e.stderr.decode().strip()}"
        )

    text = result.stdout.decode("utf-8")
    if not text.strip():  # form-feed / whitespace only → no embedded text
        return False

    txt_path.write_text(text, encoding="utf-8")
    return True


def _extract_text_layer(
    source_pdf: Path,
    page_paths: Sequence[Path],
    order: Sequence[int],
    show_progress: bool = True,
    skip_existing: bool = False,
) -> int:
    """Write the source PDF's embedded text next to each page image.

    ``page_paths[slot]`` is the image rendered from source PDF page
    ``order[slot] + 1``; its text is written to the sibling ``.txt``.  Pages
    with no embedded text produce no file (see :func:`_extract_text_page`).
    With ``skip_existing`` a page that already has a ``.txt`` is left
    untouched, so OCR output or hand edits survive a re-run.  Returns the
    number of pages that carried embedded text.
    """
    pages_with_text = 0
    with _progress(len(page_paths), "Extracting text", show_progress) as (
        progress,
        task_id,
    ):
        for slot, png_path in enumerate(page_paths):
            txt_path = png_path.with_suffix(".txt")
            if skip_existing and txt_path.exists():
                progress.advance(task_id, 1)
                continue
            if _extract_text_page(source_pdf, order[slot] + 1, txt_path):
                pages_with_text += 1
            progress.advance(task_id, 1)
    return pages_with_text


def cmd_extract(
    input_pdf: Path | str,
    dpi: int = 300,
    max_long_side: int = 1999,
    show_progress: bool = True,
    jobs: int = 0,
    rasterizer: Rasterizer = "pdftocairo",
    ppd: str = "ltr",
) -> Path:
    """Extract per-page PNGs and embedded text from a digital (non-scanned) PDF.

    The rendered long side is capped at ``max_long_side`` pixels (default
    1999) by lowering the effective DPI when needed.  The user-supplied
    ``dpi`` is the upper bound, so smaller pages are never upscaled past
    it.  Pass ``max_long_side=0`` to disable the cap.
    """
    if dpi <= 0:
        raise ValueError("DPI must be a positive integer")
    if max_long_side < 0:
        raise ValueError("max_long_side must be non-negative")

    input_path = Path(input_pdf)

    book_dir = input_path.parent / input_path.stem
    original_copy = book_dir / f"{input_path.stem}.original.pdf"

    if not input_path.exists():
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

    # Cap the effective DPI so the rendered long side stays within
    # max_long_side.  Falls back to the user dpi if pdfinfo can't report
    # a uniform page size.
    effective_dpi = dpi
    if max_long_side > 0:
        page_long_pts = _get_page_long_side_pts(original_copy)
        if page_long_pts:
            dpi_cap = max(1, int(max_long_side * 72 / page_long_pts))
            effective_dpi = min(dpi, dpi_cap)

    # Rasterize directly into artifacts/ (no thresholding needed)
    png_paths = pdf_to_images(
        original_copy,
        dpi=effective_dpi,
        out_dir=out_dir,
        show_progress=show_progress,
        jobs=jobs,
        rasterizer=rasterizer,
    )

    if not png_paths:
        raise RuntimeError("No PNG pages generated from input PDF")

    # Map output slot -> source page (identity for ltr, spread de-interleave
    # for rtl).  Poppler already zero-pads its output, so source and final
    # names share one namespace; rename in two phases (source -> temp ->
    # final) so a reordering can't clobber a not-yet-moved source.
    total_pages = len(png_paths)
    width = len(str(total_pages))
    order = _resolve_page_order(book_dir, total_pages, ppd)

    temp_paths: list[Path] = []
    for slot, src in enumerate(order):
        tmp = out_dir / f".reorder-{slot}.png"
        png_paths[src].rename(tmp)
        temp_paths.append(tmp)
    final_paths: list[Path] = []
    for slot, tmp in enumerate(temp_paths):
        final = out_dir / f"page-{slot + 1:0{width}d}.png"
        tmp.rename(final)
        final_paths.append(final)

    # Extract embedded text per page (slot's source PDF page is order[slot] + 1).
    # Pages with no embedded text produce no .txt file (see _extract_text_page).
    pages_with_text = _extract_text_layer(
        original_copy, final_paths, order, show_progress=show_progress
    )

    if show_progress:
        console = Console(stderr=True)
        if pages_with_text == 0:
            console.print(
                "[yellow]No embedded text found; wrote page images only. "
                "Run `betteria ocr` to generate text.[/yellow]"
            )
        elif pages_with_text < total_pages:
            console.print(
                f"[dim]{total_pages - pages_with_text} of {total_pages} pages had "
                "no embedded text (left for a later `ocr` pass).[/dim]"
            )

    return book_dir


# ── Subcommand: ocr ──────────────────────────────────────────────────

_DEFAULT_OCR_MODEL_MLX = "mlx-community/PaddleOCR-VL-1.5-6bit"

# Kept for backward compatibility with any external callers.
_DEFAULT_OCR_MODEL = _DEFAULT_OCR_MODEL_MLX

# Module-level cache so the model is loaded once per process.
_ocr_model_cache: dict[str, object] = {}


def _resolve_ocr_backend(backend: str) -> str:
    """Resolve the ``auto`` OCR backend to a concrete engine for this machine.

    ``mlx`` runs only on Apple Silicon; everywhere else (e.g. Intel Macs)
    ``auto`` falls back to Tesseract so ``ocr`` still works out of the box.
    """
    if backend != "auto":
        return backend
    return "mlx" if platform.machine() == "arm64" else "tesseract"


def _read_book_language(book_dir: Path) -> str | None:
    """Return the BCP-47 language from ``metadata.json`` if present, else None.

    ``metadata.json`` is usually written later in the pipeline (proofread), so
    at OCR time it often does not exist yet; callers then fall back to English.
    """
    meta_path = book_dir / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return meta.get("language") or None


def _load_ocr_model_mlx(model_path: str) -> tuple:
    """Load (or return cached) mlx-vlm model, processor, and config."""
    cache_key = f"mlx::{model_path}"
    if cache_key not in _ocr_model_cache:
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

        _ocr_model_cache[cache_key] = (model, processor, config)
    return _ocr_model_cache[cache_key]  # type: ignore[return-value]


def _ocr_page_mlx(image_path: Path, model_path: str) -> str:
    """OCR a single page image via mlx-vlm."""
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor, config = _load_ocr_model_mlx(model_path)
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


def _ocr_page_tesseract(
    image_path: Path, lang: str = "en", vertical: bool = False
) -> str:
    """OCR a single page image via Tesseract (runs anywhere, incl. Intel Macs).

    ``lang`` is a BCP-47 code (e.g. ``ja``); :func:`_tesseract_config` maps it
    to a Tesseract language and page-segmentation mode, selecting the vertical
    CJK model (``*_vert``, psm 5) when ``vertical`` is set.
    """
    tess_lang, psm, _ = _tesseract_config(lang, vertical)
    try:
        proc = subprocess.run(
            ["tesseract", str(image_path), "stdout",
             "-l", tess_lang, "--psm", str(psm)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Tesseract not found. Install it (e.g. `brew install tesseract`)."
        ) from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Tesseract failed on {image_path.name}: {e.stderr.strip()}"
        )
    return proc.stdout


def _ocr_page(
    image_path: Path,
    backend: str = "mlx",
    model: str = _DEFAULT_OCR_MODEL_MLX,
    lang: str = "en",
    vertical: bool = False,
) -> str:
    """Dispatch a single-page OCR call to the chosen backend.

    ``model`` is used only by the ``mlx`` backend; ``lang`` and ``vertical``
    only by ``tesseract``.
    """
    if backend == "mlx":
        return _ocr_page_mlx(image_path, model)
    if backend == "tesseract":
        return _ocr_page_tesseract(image_path, lang, vertical)
    raise ValueError(f"Unknown OCR backend: {backend!r}")


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
    backend: str = "mlx",
    model: str | None = None,
    lang: str | None = None,
    vertical: bool = False,
    override: bool = False,
    show_progress: bool = True,
) -> Path:
    """OCR enhanced PNGs and save per-page text files.

    Per-page OCR results are saved as ``.txt`` files next to each PNG
    (e.g. ``page-001.txt``).  Pages that already have a ``.txt`` file are
    skipped, so the command is safe to re-run after partial completion or
    after manually editing individual page texts.  Pass ``override`` to re-OCR
    every page and overwrite existing text — for instance to replace the
    embedded text that ``enhance``/``extract`` pulled from the PDF with fresh
    OCR when the embedded text is poor.

    ``backend`` selects the inference engine: ``mlx`` uses ``mlx-vlm`` (Apple
    Silicon only), ``tesseract`` shells out to Tesseract (runs anywhere,
    including Intel Macs), and ``auto`` picks ``mlx`` on Apple Silicon and
    ``tesseract`` elsewhere.  The Tesseract backend honours ``lang`` (a BCP-47
    code, defaulting to the book's ``metadata.json`` language then English) and
    ``vertical`` for vertical CJK text.
    """
    input_path = Path(input_dir)
    if not input_path.is_dir():
        raise NotADirectoryError(f"Input must be a directory: {input_path}")

    backend = _resolve_ocr_backend(backend)
    if backend == "mlx":
        if model is None:
            model = _DEFAULT_OCR_MODEL_MLX
        try:
            import mlx_vlm  # noqa: F401
        except ImportError:
            raise SystemExit(
                "The 'ocr' command with --backend mlx requires mlx-vlm.\n"
                "Install it with: uv sync --extra ocr (Apple Silicon only)"
            )
    elif backend == "tesseract":
        if shutil.which("tesseract") is None:
            raise SystemExit(
                "The 'ocr' command with --backend tesseract requires "
                "Tesseract.\nInstall it (e.g. `brew install tesseract`)."
            )
        if lang is None:
            lang = _read_book_language(input_path) or "en"
    else:
        raise SystemExit(f"Unknown OCR backend: {backend!r}")

    artifacts_dir = input_path / "artifacts"
    if not artifacts_dir.is_dir():
        artifacts_dir = input_path

    png_paths = sorted(artifacts_dir.glob("*.png"), key=_page_sort_key)
    if not png_paths:
        raise RuntimeError(f"No PNG files found in {artifacts_dir}")

    # Determine which pages need OCR.  Normally skip pages that already have a
    # .txt; with override, re-OCR every page and overwrite it.
    todo: list[tuple[Path, Path]] = []  # (png, txt) pairs needing OCR
    for png_path in png_paths:
        txt_path = png_path.with_suffix(".txt")
        if override or not txt_path.exists():
            todo.append((png_path, txt_path))

    console = Console(stderr=True)

    if todo:
        skipped = len(png_paths) - len(todo)
        if skipped:
            console.print(
                f"[dim]Skipping {skipped} pages with existing text.[/dim]"
            )

        if backend == "mlx":
            console.print(
                f"[dim]Loading OCR model {model} (backend={backend})...[/dim]"
            )
            _load_ocr_model_mlx(model)
        else:
            tess_lang, psm, _ = _tesseract_config(lang, vertical)
            console.print(
                f"[dim]Running OCR (backend={backend}, lang={tess_lang}, "
                f"psm={psm})...[/dim]"
            )

        with _progress(len(png_paths), "OCR processing", show_progress) as (
            progress,
            task_id,
        ):
            progress.advance(task_id, skipped)
            for png_path, txt_path in todo:
                text = _ocr_page(
                    png_path,
                    backend=backend,
                    model=model,
                    lang=lang or "en",
                    vertical=vertical,
                )
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
hgroup + p,
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

b,
strong{
	font-variant: small-caps;
	font-weight: normal;
}

cite{
	font-style: normal;
}

i > i,
em > i,
i > em{
	font-style: normal;
}

p:has(br){
	text-indent: 0;
}

/* Title page */
section[epub|type~="titlepage"]{
	break-after: always;
	text-align: center;
}

section[epub|type~="titlepage"] h1{
	font-size: 2em;
	margin-top: 5em;
}

section[epub|type~="titlepage"] p{
	margin: .5em 0;
	text-indent: 0;
}

section[epub|type~="titlepage"] p.author{
	font-variant: small-caps;
	margin-top: 2em;
}

/* Colophon */
section[epub|type~="colophon"]{
	break-before: always;
	margin-top: 5em;
	text-align: center;
}

section[epub|type~="colophon"] header{
	margin-bottom: 2em;
}

section[epub|type~="colophon"] p{
	margin: .5em 0;
	text-indent: 0;
}
"""

# Additional CSS appended for vertical CJK layouts (e.g. Japanese novels).
# Based on conventions from commercial Japanese EPUB files.
_EPUB_CSS_VERTICAL = """\

html{
	writing-mode: vertical-rl;
	-webkit-writing-mode: vertical-rl;
	-epub-writing-mode: vertical-rl;
	line-break: normal;
	-webkit-line-break: normal;
}

body{
	font-family: serif;
	hyphens: none;
	-epub-hyphens: none;
	font-variant-numeric: normal;
	letter-spacing: 0;
	word-spacing: 0;
}

h1,
h2,
h3,
h4,
h5,
h6{
	font-variant: normal;
}

b,
strong{
	font-variant: normal;
	font-weight: bold;
}

section[epub|type~="titlepage"] p.author{
	font-variant: normal;
}
"""


def _text_to_html(text: str) -> str:
    """Convert Markdown text to HTML."""
    import mistune

    return mistune.html(text)


# ── EPUB structure helpers (following Standard Ebooks conventions) ────

_FRONTMATTER_TITLES = frozenset({
    "foreword", "preface", "introduction", "prologue",
    "author's note", "editor's note",
})

_BACKMATTER_TITLES = frozenset({
    "epilogue", "afterword", "acknowledgments", "acknowledgements",
    "appendix", "bibliography", "glossary", "index",
    "about the author", "notes",
})

_EPUB_TYPE_MAP = {
    "foreword": "foreword",
    "preface": "preface",
    "introduction": "introduction",
    "prologue": "prologue",
    "epilogue": "epilogue",
    "afterword": "afterword",
    "acknowledgments": "acknowledgments",
    "acknowledgements": "acknowledgments",
    "appendix": "appendix",
}


def _infer_section_type(ch_meta: dict) -> str:
    """Return 'frontmatter', 'bodymatter', or 'backmatter'."""
    if ch_meta.get("number") is not None:
        return "bodymatter"
    title_lower = (ch_meta.get("title") or "").lower().strip()
    if title_lower in _FRONTMATTER_TITLES:
        return "frontmatter"
    if title_lower in _BACKMATTER_TITLES:
        return "backmatter"
    return "bodymatter"


def _infer_epub_type(ch_meta: dict) -> str:
    """Return epub:type value for a chapter section."""
    title_lower = (ch_meta.get("title") or "").lower().strip()
    return _EPUB_TYPE_MAP.get(title_lower, "chapter")


def cmd_merge(
    input_dir: Path | str,
    title: str | None = None,
    author: str | None = None,
    epub_only: bool = False,
    pdf_only: bool = False,
    embed_text: bool = True,
    horizontal_text: bool = False,
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
            book_lang = meta.get("language", "en")

            # Extract source PDF metadata if available
            source_pdf = book_dir / f"{stem}.original.pdf"
            source_meta: dict[str, str] = {}
            if source_pdf.exists():
                try:
                    result = subprocess.run(
                        ["pdfinfo", str(source_pdf)],
                        capture_output=True, text=True, check=True,
                    )
                    for line in result.stdout.splitlines():
                        if ":" in line:
                            key, _, val = line.partition(":")
                            source_meta[key.strip()] = val.strip()
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass

            book = epub.EpubBook()
            book.set_identifier(f"betteria-{stem}")
            book.set_title(book_title)
            book.set_language(book_lang)
            if book_author:
                book.add_author(book_author)

            # Optional metadata from metadata.json
            for field in ("description", "date"):
                val = meta.get(field, "")
                if val:
                    book.add_metadata("DC", field, val)
            if meta.get("publisher"):
                book.add_metadata("DC", "publisher", meta["publisher"])
            if meta.get("isbn"):
                book.add_metadata(
                    "DC", "identifier", meta["isbn"],
                    {"id": "isbn", "opf:scheme": "ISBN"},
                )
            # Subjects: prefer metadata.json, fall back to PDF keywords
            subjects = meta.get("subjects", [])
            if not subjects and source_meta.get("Keywords"):
                subjects = [
                    kw.strip() for kw in source_meta["Keywords"].split(",")
                    if kw.strip()
                ]
            for subj in subjects:
                book.add_metadata("DC", "subject", subj)
            # dc:source — record the source PDF
            if source_meta:
                source_desc = source_meta.get("Title", book_title)
                book.add_metadata("DC", "source", source_desc)

            # Cover image
            cover_path = None
            for ext in ("png", "jpg", "jpeg"):
                candidate = book_dir / f"cover.{ext}"
                if candidate.exists():
                    cover_path = candidate
                    break
            if cover_path:
                book.set_cover(f"cover{cover_path.suffix}", cover_path.read_bytes())

            # Vertical layout for CJK languages
            is_vertical = book_lang == "ja"
            css = _EPUB_CSS + _EPUB_CSS_VERTICAL if is_vertical else _EPUB_CSS
            if is_vertical:
                book.set_direction("rtl")
                book.add_metadata(
                    None, "meta", "",
                    {"name": "primary-writing-mode", "content": "vertical-rl"},
                )

            style = epub.EpubItem(
                uid="style",
                file_name="style/default.css",
                media_type="text/css",
                content=css.encode("utf-8"),
            )
            book.add_item(style)

            # ── Title page ──
            esc = html_mod.escape
            titlepage = epub.EpubHtml(
                title="Title Page",
                file_name="titlepage.xhtml",
                lang=book_lang,
            )
            tp_lines = [
                '<section id="titlepage" epub:type="titlepage">',
                f"\t<h1>{esc(book_title)}</h1>",
            ]
            if book_author:
                tp_lines.append(f'\t<p class="author">{esc(book_author)}</p>')
            tp_lines.append("</section>")
            titlepage.content = "\n".join(tp_lines)
            titlepage.add_item(style)
            book.add_item(titlepage)

            # ── Process chapters ──
            epub_chapters = []  # list of (EpubHtml, section_type)
            _added_images: dict[str, epub.EpubItem] = {}  # track added images
            chapters_meta = meta.get("chapters", [])

            def _process_chapter(text, ch_meta, i):
                """Return (EpubHtml, section_type) for one chapter."""
                heading_match = re.match(r"\s*#{1,6}\s+(.*)", text)
                if heading_match:
                    ch_title = heading_match.group(1).strip()
                else:
                    ch_title = (
                        ch_meta.get("title") or ""
                    ) if ch_meta else ""

                if not ch_title:
                    # Use first few words of body text as TOC title
                    plain = re.sub(r"[#*>\[\]`]", "", text).strip()
                    words = plain.split()[:6]
                    ch_title = " ".join(words).rstrip(".,;:!?") + "\u2026" if words else f"Chapter {i}"

                # Convert full markdown (including heading) → HTML
                body_html = _text_to_html(text)

                # Embed images referenced in the HTML
                img_pattern = re.compile(r'<img\s+[^>]*src="([^"]+)"')
                for img_match in img_pattern.finditer(body_html):
                    src = img_match.group(1)
                    img_path = (chapters_dir / src).resolve()
                    if not img_path.exists():
                        continue
                    suffix = img_path.suffix.lower()
                    media_types = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".gif": "image/gif",
                        ".svg": "image/svg+xml",
                    }
                    media_type = media_types.get(suffix, "image/png")
                    epub_img_name = f"images/{img_path.name}"
                    # Avoid adding the same image twice
                    if epub_img_name not in _added_images:
                        img_item = epub.EpubItem(
                            uid=f"img-{img_path.stem}",
                            file_name=epub_img_name,
                            media_type=media_type,
                            content=img_path.read_bytes(),
                        )
                        book.add_item(img_item)
                        _added_images[epub_img_name] = img_item
                    body_html = body_html.replace(
                        f'src="{src}"',
                        f'src="{epub_img_name}"',
                    )

                # Infer structure
                section_type = _infer_section_type(ch_meta) if ch_meta else "bodymatter"
                epub_type = _infer_epub_type(ch_meta) if ch_meta else "chapter"
                if ch_meta:
                    section_id = re.sub(
                        r"[^\w-]", "",
                        ch_meta["file"].rsplit(".", 1)[0],
                    )
                else:
                    section_id = f"chapter-{i}"

                content = (
                    f'<section id="{section_id}" epub:type="{epub_type}">\n'
                    f"{body_html}\n"
                    f"</section>"
                )

                epub_ch = epub.EpubHtml(
                    title=ch_title,
                    file_name=f"ch_{i:03d}.xhtml",
                    lang=book_lang,
                )
                epub_ch.content = content
                epub_ch.add_item(style)
                book.add_item(epub_ch)
                return epub_ch, section_type

            if chapters_meta:
                missing: list[str] = []
                for i, ch in enumerate(chapters_meta, 1):
                    # Accept both "foo.md" and "chapters/foo.md" — strip any
                    # directory prefix and resolve under chapters_dir.
                    filepath = chapters_dir / Path(ch["file"]).name
                    if not filepath.exists():
                        missing.append(ch["file"])
                        continue
                    text = filepath.read_text(encoding="utf-8")
                    epub_chapters.append(_process_chapter(text, ch, i))
                if missing:
                    raise FileNotFoundError(
                        f"Chapter files listed in {metadata_path} were not "
                        f"found in {chapters_dir}: {missing}"
                    )
            else:
                for i, ch_file in enumerate(chapter_files, 1):
                    text = ch_file.read_text(encoding="utf-8")
                    epub_chapters.append(_process_chapter(text, None, i))

            if not epub_chapters:
                raise RuntimeError(
                    f"No chapters were added to the EPUB. Check {chapters_dir} "
                    f"and {metadata_path}."
                )

            # ── Colophon ──
            colophon = epub.EpubHtml(
                title="Colophon",
                file_name="colophon.xhtml",
                lang=book_lang,
            )
            col_lines = [
                '<section id="colophon" epub:type="colophon">',
                "\t<header>",
                '\t\t<h2 epub:type="title">Colophon</h2>',
                "\t</header>",
                f"\t<p><i>{esc(book_title)}</i></p>",
            ]
            if book_author:
                col_lines.append(f"\t<p>by {esc(book_author)}</p>")
            # Publication info
            pub_parts: list[str] = []
            if meta.get("publisher"):
                pub_parts.append(esc(meta["publisher"]))
            if meta.get("date"):
                pub_parts.append(esc(meta["date"]))
            if pub_parts:
                col_lines.append(f"\t<p>{', '.join(pub_parts)}.</p>")
            if meta.get("isbn"):
                col_lines.append(
                    f'\t<p><abbr>ISBN</abbr>: {esc(meta["isbn"])}</p>'
                )
            # Betteria credit
            col_lines.append(
                "\t<p>The text content of this ebook was extracted"
                " and produced using <b>betteria</b>.</p>"
            )
            if not _added_images:
                col_lines.append(
                    "\t<p>Illustrations, photographs, and other non-text"
                    " elements from the original edition are not included.</p>"
                )
            col_lines.append("</section>")
            colophon.content = "\n".join(col_lines)
            colophon.add_item(style)
            book.add_item(colophon)

            # ── TOC, spine, and navigation ──
            toc_chapters = [ec for ec, _ in epub_chapters]
            book.toc = [titlepage] + toc_chapters + [colophon]
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.spine = ["nav", titlepage] + toc_chapters + [colophon]

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
                page_texts = None
                pdf_lang = "en"
                pdf_vertical = False
                if embed_text:
                    if metadata_path.exists():
                        try:
                            meta_pdf = json.loads(
                                metadata_path.read_text(encoding="utf-8")
                            )
                            pdf_lang = meta_pdf.get("language") or "en"
                        except (json.JSONDecodeError, OSError):
                            pass
                    pdf_vertical = (
                        _base_lang(pdf_lang) == "ja" and not horizontal_text
                    )
                    page_texts = [_page_text_for(p, pngs_dir) for p in png_paths]
                    if shutil.which("tesseract") is None:
                        console.print(
                            "[yellow]tesseract not found; building image-only PDF "
                            "without a searchable text layer.[/yellow]"
                        )
                convert_images_to_pdf(
                    png_paths,
                    pdf_path,
                    page_texts=page_texts,
                    lang=pdf_lang,
                    vertical=pdf_vertical,
                    show_progress=show_progress,
                )
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
    p_enhance.add_argument(
        "input", help="Path to input PDF or existing book directory (to resume)"
    )
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
    p_enhance.add_argument(
        "--ppd",
        choices=["ltr", "rtl"],
        default="ltr",
        help=(
            "Page progression direction (default: ltr). Use 'rtl' for "
            "right-to-left books (e.g. Japanese) whose PDF stores spreads as "
            "swapped page pairs; it de-interleaves them into correct "
            "single-page order, keeping page 1 as the cover."
        ),
    )
    p_enhance.add_argument(
        "--no-text",
        dest="extract_text",
        action="store_false",
        help=(
            "Skip pulling the source PDF's embedded text into per-page .txt "
            "files (on by default; a scan with no text layer yields none)"
        ),
    )

    # ── extract ──
    p_extract = subparsers.add_parser(
        "extract",
        help="Extract per-page PNGs and embedded text from a digital PDF.",
    )
    p_extract.add_argument("input", help="Path to input PDF")
    p_extract.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="DPI for rasterizing PDF pages (default: 300)",
    )
    p_extract.add_argument(
        "--max-long-side",
        type=int,
        default=1999,
        help=(
            "Cap the long side of rendered PNGs in pixels by lowering the "
            "effective DPI as needed (default: 1999; pass 0 to disable)"
        ),
    )
    p_extract.add_argument(
        "--quiet",
        action="store_true",
        help="Disable progress bars",
    )
    p_extract.add_argument(
        "--jobs",
        type=_coerce_jobs,
        default=0,
        help="Parallel workers ('auto'/0 = all cores; 1 = single thread)",
    )
    p_extract.add_argument(
        "--rasterizer",
        choices=["pdftoppm", "pdftocairo"],
        default="pdftocairo",
        help="Poppler backend (default: pdftocairo)",
    )
    p_extract.add_argument(
        "--ppd",
        choices=["ltr", "rtl"],
        default="ltr",
        help=(
            "Page progression direction (default: ltr). Use 'rtl' for "
            "right-to-left books whose PDF stores spreads as swapped page "
            "pairs; it de-interleaves them into correct single-page order, "
            "keeping page 1 as the cover."
        ),
    )

    # ── ocr ──
    p_ocr = subparsers.add_parser(
        "ocr",
        help="OCR enhanced PNGs into per-page text files.",
    )
    p_ocr.add_argument("input", help="Path to book directory")
    p_ocr.add_argument(
        "--backend",
        choices=["auto", "mlx", "tesseract"],
        default="auto",
        help=(
            "Inference backend (default: auto — mlx on Apple Silicon, "
            "tesseract elsewhere). 'mlx' is a local VLM (Apple Silicon only); "
            "'tesseract' shells out to Tesseract and runs anywhere."
        ),
    )
    p_ocr.add_argument(
        "--model",
        default=None,
        help=f"mlx OCR model (default: {_DEFAULT_OCR_MODEL_MLX})",
    )
    p_ocr.add_argument(
        "--lang",
        default=None,
        help=(
            "BCP-47 language for the tesseract backend (e.g. 'ja', 'de'); "
            "ignored by mlx. Defaults to metadata.json's language, then English."
        ),
    )
    p_ocr.add_argument(
        "--vertical",
        action="store_true",
        help=(
            "Tesseract: treat CJK text as vertical (uses the *_vert model, "
            "psm 5). Ignored by mlx."
        ),
    )
    p_ocr.add_argument(
        "--override",
        action="store_true",
        help=(
            "Re-OCR every page and overwrite existing .txt files (e.g. to "
            "replace embedded text pulled by enhance/extract with fresh OCR)"
        ),
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
        "--no-pdf-text",
        dest="embed_text",
        action="store_false",
        help="Skip the searchable text layer; build an image-only PDF",
    )
    p_merge.add_argument(
        "--pdf-text-horizontal",
        dest="horizontal_text",
        action="store_true",
        help="Treat CJK text as horizontal (default: vertical for Japanese)",
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
            ppd=args.ppd,
            extract_text=args.extract_text,
        )
        console.print(f"[green]Enhanced PNGs saved to {out_dir}[/green]")

    elif args.command == "extract":
        out_dir = cmd_extract(
            input_pdf=args.input,
            dpi=args.dpi,
            max_long_side=args.max_long_side,
            show_progress=not args.quiet,
            jobs=args.jobs,
            rasterizer=args.rasterizer,
            ppd=args.ppd,
        )
        console.print(f"[green]Extracted PNGs and text to {out_dir}[/green]")

    elif args.command == "ocr":
        out_dir = cmd_ocr(
            input_dir=args.input,
            backend=args.backend,
            model=args.model,
            lang=args.lang,
            vertical=args.vertical,
            override=args.override,
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
            embed_text=args.embed_text,
            horizontal_text=args.horizontal_text,
            show_progress=not args.quiet,
        )
        if epub_out:
            console.print(f"[green]EPUB saved to {epub_out}[/green]")
        if pdf_out:
            console.print(f"[green]PDF saved to {pdf_out}[/green]")


if __name__ == "__main__":
    main()
