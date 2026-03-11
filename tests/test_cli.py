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
    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)

    result = cli.cmd_enhance(
        input_pdf=book_dir,
        show_progress=False,
        jobs=1,
    )

    assert result == book_dir
    # pdf_to_images should NOT have been called since all pages exist
    assert not pdf_to_images_called


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
        ],
    )

    cli.main()

    assert captured["input_pdf"] == "input.pdf"
    assert captured["dpi"] == 300
    assert captured["jobs"] == 2
    assert captured["rasterizer"] == "pdftoppm"
    assert captured["show_progress"] is True


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
    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"
    assert captured["show_progress"] is True


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

    def fake_ocr_page(image_path, model_path):
        return ocr_texts[image_path.name]

    def fake_load_ocr_model(model_path):
        return None, None

    monkeypatch.setattr(cli, "_ocr_page", fake_ocr_page)
    monkeypatch.setattr(cli, "_load_ocr_model", fake_load_ocr_model)

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

    def fake_ocr_page(image_path, model_path):
        ocr_called_for.append(image_path.name)
        return "New OCR text for page 2."

    def fake_load_ocr_model(model_path):
        return None, None

    monkeypatch.setattr(cli, "_ocr_page", fake_ocr_page)
    monkeypatch.setattr(cli, "_load_ocr_model", fake_load_ocr_model)

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
        ],
    )

    cli.main()

    assert captured["input_pdf"] == "input.pdf"
    assert captured["dpi"] == 200
    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"


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
