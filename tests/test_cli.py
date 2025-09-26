from __future__ import annotations

import sys
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

    def fake_run(cmd, check, stdout, stderr):
        assert cmd[0] == "pdftoppm"
        output_stub = Path(cmd[-1])
        output_stub.with_suffix(".png").write_bytes(b"PNG")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    out_dir = tmp_path / "pages"
    results = cli.pdf_to_images(pdf_path, dpi=72, out_dir=out_dir, show_progress=False)

    assert len(results) == 2
    assert all(path.exists() and path.suffix == ".png" for path in results)


def test_betteria_coordinates_pipeline(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")
    output_pdf = tmp_path / "output" / "result.pdf"

    captured = {}

    def fake_pdf_to_images(input_path, dpi, out_dir, show_progress):
        captured["pdf_to_images"] = (input_path, dpi, out_dir, show_progress)
        pages = [out_dir / "page_1.png", out_dir / "page_2.png"]
        for page in pages:
            page.write_bytes(b"PNG")
        return pages

    whiten_calls: list[tuple[Path, Path, dict]] = []

    def fake_whiten(png_path, tiff_path, **kwargs):
        tiff_path = Path(tiff_path)
        tiff_path.write_bytes(b"TIFF")
        whiten_calls.append((Path(png_path), tiff_path, kwargs))

    converted = {}

    def fake_convert(tiff_paths, output_pdf_path):
        converted["paths"] = list(map(Path, tiff_paths))
        converted["output"] = Path(output_pdf_path)
        Path(output_pdf_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_pdf_path).write_bytes(b"PDF")

    monkeypatch.setattr(cli, "pdf_to_images", fake_pdf_to_images)
    monkeypatch.setattr(cli, "whiten_and_save_as_tiff", fake_whiten)
    monkeypatch.setattr(cli, "convert_tiffs_to_pdf", fake_convert)

    cli.betteria(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        dpi=100,
        threshold=120,
        use_adaptive=False,
        block_size=31,
        c_val=10,
        invert=True,
        show_progress=False,
    )

    assert output_pdf.exists()
    assert len(whiten_calls) == 2
    assert all(call[2]["threshold"] == 120 for call in whiten_calls)
    assert all(call[2]["invert"] is True for call in whiten_calls)

    captured_input, captured_dpi, captured_out_dir, captured_progress = captured["pdf_to_images"]
    assert captured_input == input_pdf
    assert captured_dpi == 100
    assert captured_progress is False
    assert captured_out_dir.name.startswith("betteria-pages-")

    assert converted["output"] == output_pdf
    assert all(path.suffix == ".tiff" for path in converted["paths"])


def test_main_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["betteria", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "betteria" in captured.out
    assert cli.__version__ in captured.out
