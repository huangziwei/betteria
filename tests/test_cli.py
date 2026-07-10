from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from betteria import cli


def test_get_page_count_parses(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    def fake_run(cmd, stdout, stderr, check, universal_newlines):
        assert cmd[0] == "pdfinfo"
        assert cmd[1] == str(pdf_path)
        return CompletedProcess(cmd, 0, stdout="Pages: 7\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.get_page_count(pdf_path) == 7


def test_pdf_to_images_generates_paths(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    monkeypatch.setattr(cli, "get_page_count", lambda path: 2)

    class FakePopen:
        def __init__(self, cmd, stdout, stderr):
            assert cmd[:4] == ["pdftocairo", "-png", "-r", "72"]
            self.cmd = cmd
            self.returncode: int | None = None
            self.stderr = io.BytesIO(b"")
            self.output_stub = Path(cmd[-1])
            assert self.output_stub.parent == out_dir
            assert self.output_stub.name == "page"

        def wait(self, timeout=None):
            (self.output_stub.parent / f"{self.output_stub.name}-1.png").write_bytes(
                b"PNG"
            )
            (self.output_stub.parent / f"{self.output_stub.name}-2.png").write_bytes(
                b"PNG"
            )
            self.returncode = 0
            return self.returncode

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)

    out_dir = tmp_path / "pages"
    results = cli.pdf_to_images(
        pdf_path, dpi=72, out_dir=out_dir, show_progress=False, jobs=1
    )

    assert len(results) == 2
    assert results[0].name == "page-1.png"
    assert results[1].name == "page-2.png"
    assert all(path.exists() and path.suffix == ".png" for path in results)


def test_pdf_to_images_parallelizes(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    monkeypatch.setattr(cli, "get_page_count", lambda path: 3)

    out_dir = tmp_path / "pages"
    calls: list[int] = []
    lock = threading.Lock()

    def fake_run(cmd, stdout, stderr, check):
        assert cmd[:4] == ["pdftocairo", "-png", "-r", "90"]
        f_idx = cmd.index("-f")
        l_idx = cmd.index("-l")
        assert cmd[f_idx + 1] == cmd[l_idx + 1]
        page = int(cmd[f_idx + 1])
        output_prefix = Path(cmd[-1])
        assert output_prefix.parent == out_dir
        png_file = output_prefix.parent / f"{output_prefix.name}-{page}.png"
        png_file.write_bytes(b"PNG")
        with lock:
            calls.append(page)
        return CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    results = cli.pdf_to_images(
        pdf_path, dpi=90, out_dir=out_dir, show_progress=False, jobs=2
    )

    assert sorted(calls) == [1, 2, 3]
    assert len(results) == 3
    assert [path.name for path in results] == ["page-1.png", "page-2.png", "page-3.png"]


def test_pdf_to_images_uses_pdftoppm(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    monkeypatch.setattr(cli, "get_page_count", lambda path: 2)

    out_dir = tmp_path / "pages"
    seen: list[str] = []

    def fake_run(cmd, stdout, stderr, check):
        assert cmd[0] == "pdftoppm"
        assert "-png" in cmd
        f_idx = cmd.index("-f")
        page = cmd[f_idx + 1]
        seen.append(page)
        output_prefix = Path(cmd[-1])
        (output_prefix.parent / f"{output_prefix.name}-{page}.png").write_bytes(b"PNG")
        return CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    results = cli.pdf_to_images(
        pdf_path,
        dpi=110,
        out_dir=out_dir,
        show_progress=False,
        jobs=2,
        rasterizer="pdftoppm",
    )

    assert sorted(seen) == ["1", "2"]
    assert [path.name for path in results] == ["page-1.png", "page-2.png"]


def test_cmd_enhance_coordinates_pipeline(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    captured = {}

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        captured["pdf_to_images"] = (
            input_path,
            dpi,
            out_dir,
            show_progress,
            jobs,
            rasterizer,
        )
        pages = [out_dir / "page_1.png", out_dir / "page_2.png"]
        for page in pages:
            page.write_bytes(b"PNG")
        return pages

    whiten_calls: list[tuple[Path, Path, dict]] = []

    def fake_whiten(png_path, out_path, **kwargs):
        out_path = Path(out_path)
        out_path.write_bytes(b"PNG-enhanced")
        whiten_calls.append((Path(png_path), out_path, kwargs))

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    book_dir = cli.cmd_enhance(
        input_pdf=input_pdf,
        dpi=100,
        threshold=120,
        use_adaptive=False,
        block_size=31,
        c_val=10,
        invert=True,
        show_progress=False,
        jobs=1,
    )

    assert book_dir == tmp_path / "doc"
    assert book_dir.is_dir()
    assert (book_dir / "doc.original.pdf").exists()
    artifacts_dir = book_dir / "artifacts"
    assert artifacts_dir.is_dir()
    assert len(whiten_calls) == 2
    assert all(call[2]["threshold"] == 120 for call in whiten_calls)
    assert all(call[2]["invert"] is True for call in whiten_calls)
    # Enhanced PNGs should be in the artifacts dir
    assert all(call[1].parent == artifacts_dir for call in whiten_calls)
    assert all(call[1].suffix == ".png" for call in whiten_calls)

    (
        captured_input,
        captured_dpi,
        captured_out_dir,
        captured_progress,
        captured_jobs,
        captured_rasterizer,
    ) = captured["pdf_to_images"]
    assert captured_input == book_dir / "doc.original.pdf"
    assert captured_dpi == 100
    assert captured_progress is False
    assert captured_out_dir.name.startswith("betteria-pages-")
    assert captured_jobs == 1
    assert captured_rasterizer == "pdftocairo"


def test_cmd_enhance_output_dir_name(monkeypatch, tmp_path):
    input_pdf = tmp_path / "letter.pdf"
    input_pdf.write_bytes(b"%PDF")

    png_paths = [tmp_path / "page-1.png", tmp_path / "page-2.png"]

    def fake_pdf_to_images(*_, **__):
        for p in png_paths:
            p.write_bytes(b"PNG")
        return png_paths

    def fake_whiten(png_path, out_path, **_):
        Path(out_path).write_bytes(b"PNG-enhanced")

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    book_dir = cli.cmd_enhance(
        input_pdf=input_pdf,
        show_progress=False,
        jobs=1,
    )

    expected_dir = tmp_path / "letter"
    assert book_dir == expected_dir
    assert (book_dir / "artifacts").is_dir()
    assert (book_dir / "letter.original.pdf").exists()


def test_cmd_enhance_skips_existing_pages(monkeypatch, tmp_path):
    """Pages with existing enhanced PNGs in artifacts/ are skipped."""
    book_dir = tmp_path / "doc"
    book_dir.mkdir()
    original_pdf = book_dir / "doc.original.pdf"
    original_pdf.write_bytes(b"%PDF")
    artifacts = book_dir / "artifacts"
    artifacts.mkdir()

    # Pre-create page 1 so it should be skipped
    (artifacts / "page-1.png").write_bytes(b"PNG-existing")

    whiten_calls: list[tuple[Path, Path]] = []

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        pages = [out_dir / "page_1.png", out_dir / "page_2.png"]
        for page in pages:
            page.write_bytes(b"PNG")
        return pages

    def fake_whiten(png_path, out_path, **kwargs):
        Path(out_path).write_bytes(b"PNG-enhanced")
        whiten_calls.append((Path(png_path), Path(out_path)))

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    # Pass directory to resume
    result = cli.cmd_enhance(
        input_pdf=book_dir,
        show_progress=False,
        jobs=1,
    )

    assert result == book_dir
    # Only page 2 should have been enhanced (page 1 was skipped)
    assert len(whiten_calls) == 1
    assert whiten_calls[0][1] == artifacts / "page-2.png"
    # Page 1 should still have its original content
    assert (artifacts / "page-1.png").read_bytes() == b"PNG-existing"


def test_cmd_enhance_skips_all_when_complete(monkeypatch, tmp_path):
    """When all pages exist, no rasterization or enhancement happens."""
    book_dir = tmp_path / "doc"
    book_dir.mkdir()
    original_pdf = book_dir / "doc.original.pdf"
    original_pdf.write_bytes(b"%PDF")
    artifacts = book_dir / "artifacts"
    artifacts.mkdir()
    (artifacts / "page-1.png").write_bytes(b"PNG")
    (artifacts / "page-2.png").write_bytes(b"PNG")

    pdf_to_images_called = False

    def fake_pdf_to_images(*args, **kwargs):
        nonlocal pdf_to_images_called
        pdf_to_images_called = True
        return []

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)

    result = cli.cmd_enhance(
        input_pdf=book_dir,
        show_progress=False,
        jobs=1,
    )

    assert result == book_dir
    # pdf_to_images should NOT have been called since all pages exist
    assert not pdf_to_images_called


# ── Page-order reordering (--ppd) ─────────────────────────────────────


def test_spread_reading_order_basic():
    # lead=1: page 1 (cover) kept, pairs (2,3),(4,5)... swapped, tail kept
    assert cli._spread_reading_order(6, lead=1) == [0, 2, 1, 4, 3, 5]
    assert cli._spread_reading_order(5, lead=1) == [0, 2, 1, 4, 3]
    # lead=0: pure pairwise swap from the start
    assert cli._spread_reading_order(6, lead=0) == [1, 0, 3, 2, 5, 4]


def test_spread_reading_order_is_a_permutation():
    for n in (1, 2, 3, 7, 212):
        order = cli._spread_reading_order(n, lead=1)
        assert sorted(order) == list(range(n))


def _fake_sources(out_dir, n):
    pages = [out_dir / f"src-{i}.png" for i in range(n)]
    for page in pages:
        page.write_bytes(b"PNG")
    return pages


def test_cmd_enhance_ppd_rtl_reorders(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        return _fake_sources(out_dir, 4)

    mapping: dict[str, str] = {}  # output slot name -> source name

    def fake_whiten(png_path, out_path, **kwargs):
        Path(out_path).write_bytes(b"PNG-enhanced")
        mapping[Path(out_path).name] = Path(png_path).name

    monkeypatch.setattr(cli, "get_page_count", lambda _: 4)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    book_dir = cli.cmd_enhance(
        input_pdf=input_pdf, show_progress=False, jobs=1, ppd="rtl"
    )

    # lead=1 over 4 pages -> order [0, 2, 1, 3]
    assert mapping["page-1.png"] == "src-0.png"  # cover stays first
    assert mapping["page-2.png"] == "src-2.png"
    assert mapping["page-3.png"] == "src-1.png"
    assert mapping["page-4.png"] == "src-3.png"

    saved = json.loads((book_dir / "page_order.json").read_text())
    assert saved["ppd"] == "rtl"
    assert saved["order"] == [0, 2, 1, 3]


def test_cmd_enhance_ltr_writes_no_order_file(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        return _fake_sources(out_dir, 4)

    def fake_whiten(png_path, out_path, **kwargs):
        Path(out_path).write_bytes(b"PNG-enhanced")

    monkeypatch.setattr(cli, "get_page_count", lambda _: 4)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    book_dir = cli.cmd_enhance(input_pdf=input_pdf, show_progress=False, jobs=1)
    assert not (book_dir / "page_order.json").exists()


def test_cmd_enhance_resume_reuses_saved_order(monkeypatch, tmp_path):
    """A saved page_order.json is honored even when ppd defaults to ltr."""
    book_dir = tmp_path / "doc"
    book_dir.mkdir()
    (book_dir / "doc.original.pdf").write_bytes(b"%PDF")
    (book_dir / "page_order.json").write_text(
        json.dumps(
            {"ppd": "rtl", "lead": 1, "total_pages": 4, "order": [0, 2, 1, 3]}
        )
    )

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        return _fake_sources(out_dir, 4)

    mapping: dict[str, str] = {}

    def fake_whiten(png_path, out_path, **kwargs):
        Path(out_path).write_bytes(b"PNG-enhanced")
        mapping[Path(out_path).name] = Path(png_path).name

    monkeypatch.setattr(cli, "get_page_count", lambda _: 4)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save", fake_whiten)

    # Resume via directory input, no --ppd (defaults to ltr); saved order wins
    cli.cmd_enhance(input_pdf=book_dir, show_progress=False, jobs=1)

    assert mapping["page-2.png"] == "src-2.png"
    assert mapping["page-3.png"] == "src-1.png"


def test_cmd_extract_ppd_rtl_reorders(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        # Poppler-style names in source (PDF) order; content tags the source.
        pages = [out_dir / f"page-{i + 1}.png" for i in range(4)]
        for i, page in enumerate(pages):
            page.write_bytes(f"src{i}".encode())
        return pages

    text_pages: list[int] = []

    def fake_extract_text_page(pdf_path, page, txt_path):
        text_pages.append(page)
        Path(txt_path).write_text(f"page {page}")

    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    book_dir = cli.cmd_extract(
        input_pdf=input_pdf, show_progress=False, jobs=1, ppd="rtl"
    )
    artifacts = book_dir / "artifacts"

    # order [0,2,1,3]: slot1<-src0, slot2<-src2, slot3<-src1, slot4<-src3
    assert (artifacts / "page-1.png").read_bytes() == b"src0"
    assert (artifacts / "page-2.png").read_bytes() == b"src2"
    assert (artifacts / "page-3.png").read_bytes() == b"src1"
    assert (artifacts / "page-4.png").read_bytes() == b"src3"
    # Text extraction reads the matching source PDF page (order[slot] + 1)
    assert text_pages == [1, 3, 2, 4]
    # Two-phase rename leaves no temp files behind
    assert not list(artifacts.glob(".reorder-*.png"))


def test_cmd_extract_produces_pngs_and_txt(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        # Simulate Poppler output (non-zero-padded names)
        pages = [out_dir / "page-1.png", out_dir / "page-2.png"]
        for p in pages:
            p.write_bytes(b"PNG")
        return pages

    extract_calls: list[tuple[int, Path]] = []

    def fake_extract_text_page(pdf_path, page, txt_path):
        extract_calls.append((page, txt_path))
        txt_path.write_text(f"Text from page {page}.", encoding="utf-8")

    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    book_dir = cli.cmd_extract(
        input_pdf=input_pdf,
        dpi=300,
        show_progress=False,
        jobs=1,
    )

    assert book_dir == tmp_path / "doc"
    assert book_dir.is_dir()
    assert (book_dir / "doc.original.pdf").exists()

    artifacts_dir = book_dir / "artifacts"
    assert artifacts_dir.is_dir()

    # PNGs should be renamed to zero-padded names
    assert (artifacts_dir / "page-1.png").exists()
    assert (artifacts_dir / "page-2.png").exists()

    # Text files should exist
    assert (artifacts_dir / "page-1.txt").exists()
    assert (artifacts_dir / "page-2.txt").exists()
    assert (artifacts_dir / "page-1.txt").read_text(encoding="utf-8") == "Text from page 1."

    # Should have called text extraction for both pages
    assert [c[0] for c in extract_calls] == [1, 2]


def test_extract_text_page_skips_pages_without_text(monkeypatch, tmp_path):
    """A page whose pdftotext output is only a form feed writes no .txt file."""
    pdf_path = tmp_path / "doc.original.pdf"
    pdf_path.write_bytes(b"%PDF")
    txt_path = tmp_path / "page-1.txt"

    def fake_run(cmd, stdout, stderr, check):
        assert cmd[0] == "pdftotext"
        # Poppler emits a lone form feed for a page with no embedded text.
        return CompletedProcess(cmd, 0, stdout=b"\x0c", stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    wrote = cli._extract_text_page(pdf_path, 1, txt_path)

    assert wrote is False
    assert not txt_path.exists()


def test_extract_text_page_writes_pages_with_text(monkeypatch, tmp_path):
    """A page with embedded text writes the .txt file verbatim and returns True."""
    pdf_path = tmp_path / "doc.original.pdf"
    pdf_path.write_bytes(b"%PDF")
    txt_path = tmp_path / "page-1.txt"

    def fake_run(cmd, stdout, stderr, check):
        return CompletedProcess(cmd, 0, stdout=b"Real page text.\n\x0c", stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    wrote = cli._extract_text_page(pdf_path, 1, txt_path)

    assert wrote is True
    assert txt_path.read_text(encoding="utf-8") == "Real page text.\n\x0c"


def test_cmd_extract_image_only_pdf_writes_no_txt(monkeypatch, tmp_path):
    """An image-only PDF yields page PNGs but no .txt files at all."""
    input_pdf = tmp_path / "scan.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        pages = [out_dir / "page-1.png", out_dir / "page-2.png"]
        for p in pages:
            p.write_bytes(b"PNG")
        return pages

    def fake_extract_text_page(pdf_path, page, txt_path):
        # Simulate a scanned page with no embedded text: nothing written.
        return False

    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    book_dir = cli.cmd_extract(input_pdf=input_pdf, show_progress=False, jobs=1)

    artifacts_dir = book_dir / "artifacts"
    assert (artifacts_dir / "page-1.png").exists()
    assert (artifacts_dir / "page-2.png").exists()
    assert not list(artifacts_dir.glob("*.txt"))


def test_cmd_extract_caps_long_side(monkeypatch, tmp_path):
    """Effective DPI is reduced so the rendered long side stays <= max_long_side."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    captured: dict[str, object] = {}

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        captured["dpi"] = dpi
        page = out_dir / "page-1.png"
        page.write_bytes(b"PNG")
        return [page]

    # Page is 1776 pt long → DPI cap for 1999 px long side is 1999*72/1776 ≈ 81
    monkeypatch.setattr(cli, "_get_page_long_side_pts", lambda _: 1776.0)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **kw: None)

    cli.cmd_extract(
        input_pdf=input_pdf,
        dpi=300,
        max_long_side=1999,
        show_progress=False,
        jobs=1,
    )

    assert captured["dpi"] == 81


def test_cmd_extract_keeps_user_dpi_for_small_pages(monkeypatch, tmp_path):
    """When page rendered at user DPI is already under the cap, no downscale."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    captured: dict[str, object] = {}

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        captured["dpi"] = dpi
        page = out_dir / "page-1.png"
        page.write_bytes(b"PNG")
        return [page]

    # Tiny page (100 pt long) at any reasonable user DPI stays under 1999 px;
    # effective DPI must remain the user value (no upscale, no needless cap).
    monkeypatch.setattr(cli, "_get_page_long_side_pts", lambda _: 100.0)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **kw: None)

    cli.cmd_extract(
        input_pdf=input_pdf,
        dpi=150,
        max_long_side=1999,
        show_progress=False,
        jobs=1,
    )

    assert captured["dpi"] == 150


def test_cmd_extract_max_long_side_zero_disables_cap(monkeypatch, tmp_path):
    """max_long_side=0 leaves the user-supplied DPI untouched."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    captured: dict[str, object] = {}

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        captured["dpi"] = dpi
        page = out_dir / "page-1.png"
        page.write_bytes(b"PNG")
        return [page]

    # Even with a huge page, max_long_side=0 should disable the cap and pdfinfo
    # should not even be consulted.
    def must_not_call(_):
        raise AssertionError("_get_page_long_side_pts should not be called")

    monkeypatch.setattr(cli, "_get_page_long_side_pts", must_not_call)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", lambda *a, **kw: None)

    cli.cmd_extract(
        input_pdf=input_pdf,
        dpi=300,
        max_long_side=0,
        show_progress=False,
        jobs=1,
    )

    assert captured["dpi"] == 300


def test_get_page_long_side_pts_parses(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    def fake_run(cmd, stdout, stderr, check, text):
        return CompletedProcess(
            cmd, 0,
            stdout="Pages: 1\nPage size:       1059.81 x 1775.99 pts\n",
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._get_page_long_side_pts(pdf_path) == 1775.99


def test_get_page_long_side_pts_handles_named_size(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    def fake_run(cmd, stdout, stderr, check, text):
        return CompletedProcess(
            cmd, 0,
            stdout="Pages: 1\nPage size:       letter (612 x 792 pts)\n",
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._get_page_long_side_pts(pdf_path) == 792.0


def test_get_page_long_side_pts_returns_none_on_variable(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF")

    def fake_run(cmd, stdout, stderr, check, text):
        return CompletedProcess(
            cmd, 0,
            stdout="Pages: 5\nPage size:       variable\n",
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._get_page_long_side_pts(pdf_path) is None


def test_cmd_extract_rerun(monkeypatch, tmp_path):
    """Re-running extract when the PDF has already been moved should work."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(pdf_path, dpi, out_dir, show_progress, jobs, rasterizer):
        pages = [out_dir / "page-1.png"]
        for p in pages:
            p.write_bytes(b"PNG")
        return pages

    def fake_extract_text_page(pdf_path, page, txt_path):
        txt_path.write_text("text", encoding="utf-8")

    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    # First run
    cli.cmd_extract(input_pdf=input_pdf, show_progress=False, jobs=1)
    assert not input_pdf.exists()  # moved to book_dir

    # Second run (PDF already moved)
    book_dir = cli.cmd_extract(input_pdf=input_pdf, show_progress=False, jobs=1)
    assert book_dir == tmp_path / "doc"


def test_main_extract_subcommand(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_extract(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/out")

    monkeypatch.setattr(cli, "cmd_extract", fake_cmd_extract)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "extract",
            "input.pdf",
            "--dpi",
            "300",
            "--jobs",
            "2",
            "--rasterizer",
            "pdftoppm",
            "--ppd",
            "rtl",
        ],
    )

    cli.main()

    assert captured["input_pdf"] == "input.pdf"
    assert captured["dpi"] == 300
    assert captured["jobs"] == 2
    assert captured["rasterizer"] == "pdftoppm"
    assert captured["show_progress"] is True
    assert captured["ppd"] == "rtl"


def test_main_extract_defaults(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_extract(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/out")

    monkeypatch.setattr(cli, "cmd_extract", fake_cmd_extract)
    monkeypatch.setattr(sys, "argv", ["betteria", "extract", "book.pdf"])

    cli.main()

    assert captured["input_pdf"] == "book.pdf"
    assert captured["dpi"] == 300
    assert captured["max_long_side"] == 1999
    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"
    assert captured["show_progress"] is True
    assert captured["ppd"] == "ltr"


def test_main_extract_max_long_side_flag(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_extract(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/out")

    monkeypatch.setattr(cli, "cmd_extract", fake_cmd_extract)
    monkeypatch.setattr(
        sys, "argv",
        ["betteria", "extract", "book.pdf", "--max-long-side", "0"],
    )

    cli.main()

    assert captured["max_long_side"] == 0


def test_cmd_ocr_produces_per_page_txt(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    artifacts_dir = book_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "page-1.png").write_bytes(b"PNG1")
    (artifacts_dir / "page-2.png").write_bytes(b"PNG2")
    (artifacts_dir / "page-3.png").write_bytes(b"PNG3")

    ocr_texts = {
        "page-1.png": "Chapter 1: Introduction\nSome text here.",
        "page-2.png": "More text on page two.",
        "page-3.png": "Chapter 2: Methods\nDetails about methods.",
    }

    def fake_ocr_page(image_path, backend="mlx", model=None, **kwargs):
        return ocr_texts[image_path.name]

    def fake_load_ocr_model_mlx(model_path):
        return None, None, None

    import sys
    import types
    sys.modules.setdefault("mlx_vlm", types.ModuleType("mlx_vlm"))
    monkeypatch.setattr(cli, "_ocr_page", fake_ocr_page)
    monkeypatch.setattr(cli, "_load_ocr_model_mlx", fake_load_ocr_model_mlx)

    result = cli.cmd_ocr(input_dir=book_dir, show_progress=False)

    assert result == book_dir

    # Per-page .txt files should exist next to PNGs in artifacts/
    assert (artifacts_dir / "page-1.txt").exists()
    assert (artifacts_dir / "page-2.txt").exists()
    assert (artifacts_dir / "page-3.txt").exists()


def test_cmd_ocr_skips_existing_txt(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    artifacts_dir = book_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "page-1.png").write_bytes(b"PNG1")
    (artifacts_dir / "page-2.png").write_bytes(b"PNG2")

    # Pre-existing text for page 1
    (artifacts_dir / "page-1.txt").write_text("Existing OCR text.", encoding="utf-8")

    ocr_called_for: list[str] = []

    def fake_ocr_page(image_path, backend="mlx", model=None, **kwargs):
        ocr_called_for.append(image_path.name)
        return "New OCR text for page 2."

    def fake_load_ocr_model_mlx(model_path):
        return None, None, None

    import sys
    import types
    sys.modules.setdefault("mlx_vlm", types.ModuleType("mlx_vlm"))
    monkeypatch.setattr(cli, "_ocr_page", fake_ocr_page)
    monkeypatch.setattr(cli, "_load_ocr_model_mlx", fake_load_ocr_model_mlx)

    result = cli.cmd_ocr(input_dir=book_dir, show_progress=False)

    assert result == book_dir
    # Only page 2 should have been OCR'd
    assert ocr_called_for == ["page-2.png"]
    # Page 1 text should be preserved
    assert (artifacts_dir / "page-1.txt").read_text(encoding="utf-8") == "Existing OCR text."


def test_detect_chapters_heuristic():
    page_texts = [
        "Chapter 1: The Beginning\nSome text.",
        "More text here.",
        "Chapter 2: The Middle\nAnother paragraph.",
        "Even more text.",
        "Chapter 3: The End\nFinal text.",
    ]
    result = cli._detect_chapters(page_texts)
    chapters = result["chapters"]
    assert len(chapters) == 3
    assert chapters[0]["start_page"] == 1
    assert chapters[1]["start_page"] == 3
    assert chapters[2]["start_page"] == 5


def test_detect_chapters_strips_headers_and_footers():
    # Simulate a book where even pages have book title header, odd have chapter
    # title, and every page has a page number footer.
    page_texts = [
        "THE BOOK TITLE\n\nChapter 1: Intro\nSome text.\n\n1",
        "THE BOOK TITLE\n\nMore text continues.\n\n2",
        "THE BOOK TITLE\n\nStill chapter one.\n\n3",
        "THE BOOK TITLE\n\nChapter 2: Middle\nNew stuff.\n\n4",
        "THE BOOK TITLE\n\nFinal page.\n\n5",
    ]
    result = cli._detect_chapters(page_texts)
    chapters = result["chapters"]
    # Should find 2 chapters, not be confused by "THE BOOK TITLE" or page numbers
    assert len(chapters) == 2
    assert chapters[0]["start_page"] == 1
    assert chapters[1]["start_page"] == 4


def test_detect_chapters_no_match():
    page_texts = ["Just some text.", "More text without headings."]
    result = cli._detect_chapters(page_texts)
    assert len(result["chapters"]) == 1
    assert result["chapters"][0]["title"] == "Full Text"


def test_strip_headers_footers():
    pages = [
        "HEADER\n\nContent one.\n\n10",
        "HEADER\n\nContent two.\n\n11",
        "HEADER\n\nContent three.\n\n12",
        "HEADER\n\nContent four.\n\n13",
    ]
    cleaned = cli._strip_headers_footers(pages)
    for c in cleaned:
        assert "HEADER" not in c
        assert not c.strip().endswith(("10", "11", "12", "13"))


def test_cmd_merge_creates_epub_from_chapters(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()

    # Set up chapters directory with proofread text
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir()
    (chapters_dir / "01-intro.txt").write_text(
        "Hello world.\n\nSecond paragraph.", encoding="utf-8"
    )
    (chapters_dir / "02-body.txt").write_text("Main content here.", encoding="utf-8")

    # Metadata file
    metadata_obj = {
        "title": "Test Book",
        "author": "Test Author",
        "chapters": [
            {"number": 1, "title": "Intro", "pages": [1, 5], "file": "01-intro.txt"},
            {"number": 2, "title": "Body", "pages": [6, 10], "file": "02-body.txt"},
        ],
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(metadata_obj), encoding="utf-8"
    )

    epub_out, pdf_out = cli.cmd_merge(
        input_dir=book_dir,
        epub_only=True,
        show_progress=False,
    )

    assert epub_out == book_dir / "book.epub"
    assert epub_out.exists()
    assert pdf_out is None


def test_cmd_merge_untitled_chapter_uses_first_words_in_toc(tmp_path):
    """Chapters with no heading and no metadata title use first words in TOC."""
    book_dir = tmp_path / "book"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)

    (chapters_dir / "01-untitled.txt").write_text(
        "The quick brown fox jumps over the lazy dog.", encoding="utf-8"
    )
    (chapters_dir / "02-titled.txt").write_text(
        "## Real Title\n\nBody text here.", encoding="utf-8"
    )

    metadata_obj = {
        "title": "Test Book",
        "author": "Test Author",
        "chapters": [
            {"number": 1, "title": "", "pages": [1, 5], "file": "01-untitled.txt"},
            {"number": 2, "title": "Real Title", "pages": [6, 10], "file": "02-titled.txt"},
        ],
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(metadata_obj), encoding="utf-8"
    )

    epub_out, _ = cli.cmd_merge(
        input_dir=book_dir,
        epub_only=True,
        show_progress=False,
    )

    import zipfile
    import re

    with zipfile.ZipFile(epub_out) as z:
        toc_titles = []
        for name in z.namelist():
            if "nav" in name.lower() and name.endswith(".xhtml"):
                content = z.read(name).decode("utf-8")
                toc_titles = re.findall(r"<a[^>]*>(.*?)</a>", content)
                break

    # Untitled chapter should use first words with ellipsis
    assert any("The quick brown fox jumps over" in t for t in toc_titles)
    assert any(t.endswith("\u2026") for t in toc_titles if "quick" in t)
    # Titled chapter should use its actual title
    assert "Real Title" in toc_titles
    # Body text of untitled chapter should NOT have the ellipsis
    with zipfile.ZipFile(epub_out) as z:
        for name in z.namelist():
            if "ch_001" in name:
                body = z.read(name).decode("utf-8")
                # The ellipsis appears in <title> (mirrors TOC), but not in <body>
                body_content = body.split("<body>", 1)[1]
                assert "\u2026" not in body_content
                assert "The quick brown fox" in body_content
                break


def test_cmd_merge_skips_epub_without_chapters(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    artifacts_dir = book_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    from PIL import Image as PILImage

    img = PILImage.new("L", (100, 100), 255)
    img.save(str(artifacts_dir / "page-1.png"))

    epub_out, pdf_out = cli.cmd_merge(
        input_dir=book_dir,
        show_progress=False,
    )

    assert epub_out is None
    assert pdf_out == book_dir / "book.pdf"
    assert pdf_out.exists()


def test_cmd_merge_creates_pdf(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    artifacts_dir = book_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    from PIL import Image as PILImage

    img = PILImage.new("L", (100, 100), 255)
    img.save(str(artifacts_dir / "page-1.png"))

    epub_out, pdf_out = cli.cmd_merge(
        input_dir=book_dir,
        show_progress=False,
    )

    assert epub_out is None  # No chapters dir, no EPUB
    assert pdf_out == book_dir / "book.pdf"
    assert pdf_out.exists()


def test_cmd_merge_title_override(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()

    # Create chapters dir so EPUB gets generated
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir()
    (chapters_dir / "01-ch.txt").write_text("Text.", encoding="utf-8")

    epub_out, _ = cli.cmd_merge(
        input_dir=book_dir,
        title="Override Title",
        author="Override Author",
        epub_only=True,
        show_progress=False,
    )

    assert epub_out.exists()


def test_cmd_merge_accepts_chapter_path_with_dir_prefix(tmp_path):
    """metadata.json 'file' may be either 'foo.md' or 'chapters/foo.md'."""
    book_dir = tmp_path / "book"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01-intro.md").write_text("Body.", encoding="utf-8")

    metadata_obj = {
        "title": "Test Book",
        "author": "Test Author",
        "chapters": [
            {"number": 1, "title": "Intro", "file": "chapters/01-intro.md"},
        ],
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(metadata_obj), encoding="utf-8"
    )

    epub_out, _ = cli.cmd_merge(
        input_dir=book_dir,
        epub_only=True,
        show_progress=False,
    )

    import zipfile

    with zipfile.ZipFile(epub_out) as z:
        names = z.namelist()
        # Chapter content xhtml should exist
        assert any("ch_001" in n for n in names)


def test_cmd_merge_raises_on_missing_chapter_files(tmp_path):
    """Missing chapter files referenced in metadata.json must fail loudly."""
    book_dir = tmp_path / "book"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01-present.md").write_text("Body.", encoding="utf-8")

    metadata_obj = {
        "title": "Test Book",
        "author": "Test Author",
        "chapters": [
            {"number": 1, "title": "One", "file": "01-present.md"},
            {"number": 2, "title": "Two", "file": "02-missing.md"},
        ],
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(metadata_obj), encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError, match="02-missing.md"):
        cli.cmd_merge(
            input_dir=book_dir,
            epub_only=True,
            show_progress=False,
        )


def test_text_to_html():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird."
    html = cli._text_to_html(text)
    assert "<p>First paragraph.</p>" in html
    assert "<p>Second paragraph.</p>" in html
    assert "<p>Third.</p>" in html


def test_text_to_html_escapes_entities():
    text = "A < B & C > D"
    html = cli._text_to_html(text)
    assert "&lt;" in html
    assert "&amp;" in html
    assert "&gt;" in html


def test_slugify():
    assert cli._slugify("Chapter 1: Introduction") == "chapter-1-introduction"
    assert cli._slugify("  Hello World!  ") == "hello-world"
    assert cli._slugify("A" * 100) == "a" * 50


def test_main_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["betteria", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "betteria" in captured.out
    assert cli.__version__ in captured.out


def test_main_no_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["betteria"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_main_enhance_subcommand(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_enhance(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/out-artifacts")

    monkeypatch.setattr(cli, "cmd_enhance", fake_cmd_enhance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "enhance",
            "input.pdf",
            "--dpi",
            "200",
            "--jobs",
            "auto",
            "--rasterizer",
            "pdftocairo",
            "--ppd",
            "rtl",
        ],
    )

    cli.main()

    assert captured["input_pdf"] == "input.pdf"
    assert captured["dpi"] == 200
    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"
    assert captured["ppd"] == "rtl"


def test_main_enhance_defaults(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_enhance(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/out-artifacts")

    monkeypatch.setattr(cli, "cmd_enhance", fake_cmd_enhance)
    monkeypatch.setattr(sys, "argv", ["betteria", "enhance", "book.pdf"])

    cli.main()

    assert captured["input_pdf"] == "book.pdf"
    assert captured["dpi"] == 150
    assert captured["threshold"] == 128
    assert captured["use_adaptive"] is True
    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"
    assert captured["show_progress"] is True
    assert captured["ppd"] == "ltr"


def test_main_ocr_subcommand(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_ocr(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/book-artifacts")

    monkeypatch.setattr(cli, "cmd_ocr", fake_cmd_ocr)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "ocr",
            "book-artifacts/",
            "--model",
            "custom-model",
            "--quiet",
        ],
    )

    cli.main()

    assert captured["input_dir"] == "book-artifacts/"
    assert captured["model"] == "custom-model"
    assert captured["show_progress"] is False


def test_main_merge_subcommand(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_merge(**kwargs):
        captured.update(kwargs)
        return Path("/tmp/book.epub"), Path("/tmp/book.pdf")

    monkeypatch.setattr(cli, "cmd_merge", fake_cmd_merge)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "merge",
            "book-chapters/",
            "--title",
            "My Book",
            "--author",
            "Author",
            "--epub-only",
        ],
    )

    cli.main()

    assert captured["input_dir"] == "book-chapters/"
    assert captured["title"] == "My Book"
    assert captured["author"] == "Author"
    assert captured["epub_only"] is True
    assert captured["pdf_only"] is False
    assert captured["embed_text"] is True
    assert captured["horizontal_text"] is False


def test_main_merge_text_layer_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_cmd_merge(**kwargs):
        captured.update(kwargs)
        return None, Path("/tmp/book.pdf")

    monkeypatch.setattr(cli, "cmd_merge", fake_cmd_merge)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria", "merge", "book/", "--pdf-only",
            "--no-pdf-text", "--pdf-text-horizontal",
        ],
    )

    cli.main()

    assert captured["embed_text"] is False
    assert captured["horizontal_text"] is True


# ── Searchable-PDF text layer ────────────────────────────────────────


def test_markdown_to_plaintext_strips_syntax():
    md = (
        "## Heading\n\n*emph* and **bold** with `code`\n\n"
        "> a blockquote\n\n[BLANK PAGE]\n\n---\n\nTail.\n<!--PARA-->"
    )
    out = cli._markdown_to_plaintext(md)
    assert "Heading" in out and "Tail." in out
    assert not any(sym in out for sym in "*#>`")
    assert "BLANK PAGE" not in out
    assert "<!--" not in out


def test_proofread_units_words_vs_chars():
    assert cli._proofread_units("the cat sat", cjk=False) == ["the", "cat", "sat"]
    assert cli._proofread_units("猫が居る", cjk=True) == ["猫", "が", "居", "る"]


def test_align_tokens_corrects_and_drops_ocr_only():
    body = ("1", "1", "2")
    ocr = [
        {"text": "teh", "x": 0, "y": 10, "w": 10, "h": 5, "col": body},
        {"text": "cat", "x": 12, "y": 10, "w": 10, "h": 5, "col": body},
        # a running header Tesseract picked up but proofreading removed
        {"text": "RUNHEAD", "x": 0, "y": 60, "w": 40, "h": 5, "col": ("1", "1", "9")},
        {"text": "sat", "x": 0, "y": 70, "w": 10, "h": 5, "col": ("1", "1", "3")},
        {"text": "down", "x": 12, "y": 70, "w": 10, "h": 5, "col": ("1", "1", "3")},
    ]
    placed = cli._align_tokens(ocr, ["the", "cat", "sat", "down"], cjk=False)
    assert [text for _, text in placed] == ["the", "cat", "sat", "down"]
    # corrected "the" rides on the OCR box of the misspelled "teh"
    assert placed[0][0]["x"] == 0 and placed[0][0]["y"] == 10


def test_group_columns_keeps_columns_contiguous():
    a, b = ("1", "1", "1"), ("1", "1", "2")
    placed = [
        ({"x": 0, "y": 0, "w": 4, "h": 4, "col": a}, "あ"),
        ({"x": 0, "y": 5, "w": 4, "h": 4, "col": a}, "い"),
        ({"x": 9, "y": 0, "w": 4, "h": 4, "col": b}, "う"),
    ]
    runs = cli._group_columns(placed)
    assert [text for _, text in runs] == ["あい", "う"]


def test_text_layer_pdf_is_searchable(monkeypatch, tmp_path):
    import shutil
    from subprocess import run

    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext (poppler) not available")

    from PIL import Image as PILImage

    img_path = tmp_path / "page-1.png"
    PILImage.new("L", (200, 100), 255).save(str(img_path))

    def fake_tokens(image_path, lang, psm, vertical, split_chars):
        return [
            {"text": "Helo", "x": 10, "y": 20, "w": 60, "h": 30,
             "col": ("1", "1", "1")},
            {"text": "world", "x": 80, "y": 20, "w": 70, "h": 30,
             "col": ("1", "1", "1")},
        ]

    monkeypatch.setattr(cli, "_tesseract_tokens", fake_tokens)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)

    out_pdf = tmp_path / "out.pdf"
    cli.convert_images_to_pdf(
        [img_path], out_pdf, page_texts=["Hello world"], lang="en",
    )
    text = run(
        ["pdftotext", str(out_pdf), "-"], capture_output=True, text=True
    ).stdout
    assert "Hello" in text and "world" in text   # corrected from "Helo"
    assert "Helo" not in text


def test_text_layer_vertical_japanese_order(monkeypatch, tmp_path):
    import shutil
    from subprocess import run

    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext (poppler) not available")

    from PIL import Image as PILImage

    img_path = tmp_path / "page-1.png"
    PILImage.new("L", (200, 200), 255).save(str(img_path))

    # Two columns, right-to-left; Tesseract emits them in reading order.
    def fake_tokens(image_path, lang, psm, vertical, split_chars):
        return [
            {"text": "あ", "x": 150, "y": 20, "w": 20, "h": 20, "col": ("1", "1", "1")},
            {"text": "い", "x": 150, "y": 44, "w": 20, "h": 20, "col": ("1", "1", "1")},
            {"text": "う", "x": 110, "y": 20, "w": 20, "h": 20, "col": ("1", "1", "2")},
            {"text": "え", "x": 110, "y": 44, "w": 20, "h": 20, "col": ("1", "1", "2")},
        ]

    monkeypatch.setattr(cli, "_tesseract_tokens", fake_tokens)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)

    out_pdf = tmp_path / "out.pdf"
    cli.convert_images_to_pdf(
        [img_path], out_pdf, page_texts=["あいうえ"], lang="ja", vertical=True,
    )
    text = run(
        ["pdftotext", str(out_pdf), "-"], capture_output=True, text=True
    ).stdout
    flat = "".join(text.split())
    assert "あいうえ" in flat   # correct vertical reading order, geometric mode


def test_cmd_merge_image_only_pdf_has_no_text(monkeypatch, tmp_path):
    import shutil
    from subprocess import run

    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext (poppler) not available")

    book_dir = tmp_path / "book"
    artifacts_dir = book_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    from PIL import Image as PILImage

    PILImage.new("L", (100, 100), 255).save(str(artifacts_dir / "page-1.png"))
    (artifacts_dir / "page-1.proofread.txt").write_text("Hi.", encoding="utf-8")

    _, pdf_out = cli.cmd_merge(
        input_dir=book_dir, embed_text=False, show_progress=False,
    )
    text = run(
        ["pdftotext", str(pdf_out), "-"], capture_output=True, text=True
    ).stdout
    assert text.strip() == ""


# ── enhance: embedded-text extraction (parity with extract) ───────────


def test_cmd_enhance_extracts_embedded_text(monkeypatch, tmp_path):
    """enhance pulls the source PDF's embedded text into per-page .txt files."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        pages = [out_dir / "page_1.png", out_dir / "page_2.png"]
        for page in pages:
            page.write_bytes(b"PNG")
        return pages

    extract_calls: list[int] = []

    def fake_extract_text_page(pdf_path, page, txt_path):
        extract_calls.append(page)
        txt_path.write_text(f"Text {page}.", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(
        cli, "whiten_and_save", lambda p, o, **k: Path(o).write_bytes(b"E")
    )
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    book_dir = cli.cmd_enhance(input_pdf=input_pdf, show_progress=False, jobs=1)

    artifacts = book_dir / "artifacts"
    assert (artifacts / "page-1.txt").read_text(encoding="utf-8") == "Text 1."
    assert (artifacts / "page-2.txt").read_text(encoding="utf-8") == "Text 2."
    assert extract_calls == [1, 2]


def test_cmd_enhance_no_text_skips_extraction(monkeypatch, tmp_path):
    """extract_text=False (--no-text) skips embedded-text extraction entirely."""
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress, jobs, rasterizer):
        page = out_dir / "page_1.png"
        page.write_bytes(b"PNG")
        return [page]

    called = False

    def fake_extract_text_page(*a, **k):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cli, "get_page_count", lambda _: 1)
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(
        cli, "whiten_and_save", lambda p, o, **k: Path(o).write_bytes(b"E")
    )
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    book_dir = cli.cmd_enhance(
        input_pdf=input_pdf, show_progress=False, jobs=1, extract_text=False
    )

    assert called is False
    assert not (book_dir / "artifacts" / "page-1.txt").exists()


def test_cmd_enhance_text_pass_preserves_existing_txt(monkeypatch, tmp_path):
    """The text pass runs even when all PNGs exist, and never clobbers a .txt."""
    book_dir = tmp_path / "doc"
    artifacts = book_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (book_dir / "doc.original.pdf").write_bytes(b"%PDF")
    # Both pages already enhanced; page 1 also has hand-edited text.
    (artifacts / "page-1.png").write_bytes(b"PNG")
    (artifacts / "page-1.txt").write_text("hand edited", encoding="utf-8")
    (artifacts / "page-2.png").write_bytes(b"PNG")

    def fake_extract_text_page(pdf_path, page, txt_path):
        txt_path.write_text(f"embedded {page}", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "get_page_count", lambda _: 2)
    monkeypatch.setattr(cli, "_extract_text_page", fake_extract_text_page)

    cli.cmd_enhance(input_pdf=book_dir, show_progress=False, jobs=1)

    # page 1's existing text is preserved; page 2 gets fresh embedded text.
    assert (artifacts / "page-1.txt").read_text(encoding="utf-8") == "hand edited"
    assert (artifacts / "page-2.txt").read_text(encoding="utf-8") == "embedded 2"


# ── ocr: override, backends, language ─────────────────────────────────


def test_cmd_ocr_override_reocrs_existing(monkeypatch, tmp_path):
    """--override re-OCRs pages that already have text, overwriting it."""
    book_dir = tmp_path / "book"
    artifacts = book_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "page-1.png").write_bytes(b"PNG1")
    (artifacts / "page-1.txt").write_text("embedded text", encoding="utf-8")

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(cli, "_ocr_page", lambda image_path, **kw: "fresh OCR text")

    cli.cmd_ocr(
        input_dir=book_dir, backend="tesseract", override=True, show_progress=False
    )

    assert (artifacts / "page-1.txt").read_text(encoding="utf-8") == "fresh OCR text"


def test_cmd_ocr_tesseract_backend_passes_lang_and_vertical(monkeypatch, tmp_path):
    """The tesseract backend threads lang/vertical into _ocr_page (no mlx needed)."""
    book_dir = tmp_path / "book"
    artifacts = book_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "page-1.png").write_bytes(b"PNG1")

    calls: list[tuple] = []

    def fake_ocr_page(image_path, backend="mlx", model=None, lang="en", vertical=False):
        calls.append((backend, lang, vertical))
        return "text"

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(cli, "_ocr_page", fake_ocr_page)

    cli.cmd_ocr(
        input_dir=book_dir,
        backend="tesseract",
        lang="ja",
        vertical=True,
        show_progress=False,
    )

    assert (artifacts / "page-1.txt").read_text(encoding="utf-8") == "text"
    assert calls == [("tesseract", "ja", True)]


def test_cmd_ocr_tesseract_lang_defaults_from_metadata(monkeypatch, tmp_path):
    """With no --lang, the tesseract backend reads metadata.json's language."""
    book_dir = tmp_path / "book"
    artifacts = book_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "page-1.png").write_bytes(b"PNG1")
    (book_dir / "metadata.json").write_text(
        json.dumps({"language": "de"}), encoding="utf-8"
    )

    calls: list[dict] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(
        cli, "_ocr_page", lambda image_path, **kw: calls.append(kw) or "t"
    )

    cli.cmd_ocr(input_dir=book_dir, backend="tesseract", show_progress=False)

    assert calls[0]["lang"] == "de"


def test_ocr_page_tesseract_builds_command(monkeypatch, tmp_path):
    """_ocr_page_tesseract shells out with the right lang/psm and returns stdout."""
    img = tmp_path / "page-1.png"
    img.write_bytes(b"PNG")

    captured: dict = {}

    def fake_run(cmd, capture_output, text, check):
        captured["cmd"] = cmd
        return CompletedProcess(cmd, 0, stdout="page text", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    out = cli._ocr_page_tesseract(img, lang="ja", vertical=True)

    assert out == "page text"
    cmd = captured["cmd"]
    assert cmd[0] == "tesseract"
    # Vertical Japanese → jpn_vert model with psm 5.
    assert cmd[cmd.index("-l") + 1] == "jpn_vert"
    assert cmd[cmd.index("--psm") + 1] == "5"


def test_ocr_page_dispatches_to_backend(monkeypatch, tmp_path):
    img = tmp_path / "p.png"
    monkeypatch.setattr(
        cli, "_ocr_page_tesseract", lambda image, lang, vertical: f"T:{lang}:{vertical}"
    )
    monkeypatch.setattr(cli, "_ocr_page_mlx", lambda image, model: "M")

    assert cli._ocr_page(img, backend="tesseract", lang="de") == "T:de:False"
    assert cli._ocr_page(img, backend="mlx") == "M"
    with pytest.raises(ValueError):
        cli._ocr_page(img, backend="nope")


def test_resolve_ocr_backend(monkeypatch):
    assert cli._resolve_ocr_backend("mlx") == "mlx"
    assert cli._resolve_ocr_backend("tesseract") == "tesseract"
    monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    assert cli._resolve_ocr_backend("auto") == "mlx"
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    assert cli._resolve_ocr_backend("auto") == "tesseract"


def test_read_book_language(tmp_path):
    assert cli._read_book_language(tmp_path) is None
    (tmp_path / "metadata.json").write_text(
        json.dumps({"language": "ja"}), encoding="utf-8"
    )
    assert cli._read_book_language(tmp_path) == "ja"
    # A metadata.json without a language key falls back to None.
    (tmp_path / "metadata.json").write_text(
        json.dumps({"title": "X"}), encoding="utf-8"
    )
    assert cli._read_book_language(tmp_path) is None


def test_main_ocr_passes_new_flags(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        cli, "cmd_ocr", lambda **kw: captured.update(kw) or Path("/x")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria", "ocr", "book/",
            "--backend", "tesseract",
            "--lang", "ja",
            "--vertical",
            "--override",
        ],
    )
    cli.main()
    assert captured["backend"] == "tesseract"
    assert captured["lang"] == "ja"
    assert captured["vertical"] is True
    assert captured["override"] is True


def test_main_ocr_defaults(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        cli, "cmd_ocr", lambda **kw: captured.update(kw) or Path("/x")
    )
    monkeypatch.setattr(sys, "argv", ["betteria", "ocr", "book/"])
    cli.main()
    assert captured["backend"] == "auto"
    assert captured["lang"] is None
    assert captured["vertical"] is False
    assert captured["override"] is False


def test_main_enhance_no_text(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        cli, "cmd_enhance", lambda **kw: captured.update(kw) or Path("/x")
    )
    monkeypatch.setattr(sys, "argv", ["betteria", "enhance", "book.pdf", "--no-text"])
    cli.main()
    assert captured["extract_text"] is False


def test_main_enhance_extract_text_default(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        cli, "cmd_enhance", lambda **kw: captured.update(kw) or Path("/x")
    )
    monkeypatch.setattr(sys, "argv", ["betteria", "enhance", "book.pdf"])
    cli.main()
    assert captured["extract_text"] is True
