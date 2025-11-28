from __future__ import annotations

import io
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
            self._step = 0
            self.returncode: int | None = None
            self.stderr = io.BytesIO(b"")
            self.output_stub = Path(cmd[-1])
            assert self.output_stub.parent == out_dir
            assert self.output_stub.name == "page"

        def wait(self, timeout=None):
            if self._step == 0:
                (
                    self.output_stub.parent / f"{self.output_stub.name}-1.png"
                ).write_bytes(b"PNG")
                self._step += 1
                raise cli.subprocess.TimeoutExpired(self.cmd, timeout)
            if self._step == 1:
                (
                    self.output_stub.parent / f"{self.output_stub.name}-2.png"
                ).write_bytes(b"PNG")
                self.returncode = 0
                self._step += 1
                return self.returncode
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


def test_betteria_coordinates_pipeline(monkeypatch, tmp_path):
    input_pdf = tmp_path / "doc.pdf"
    input_pdf.write_bytes(b"%PDF")
    output_pdf = tmp_path / "output" / "result.pdf"

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
        jobs=1,
    )

    assert output_pdf.exists()
    assert len(whiten_calls) == 2
    assert all(call[2]["threshold"] == 120 for call in whiten_calls)
    assert all(call[2]["invert"] is True for call in whiten_calls)

    (
        captured_input,
        captured_dpi,
        captured_out_dir,
        captured_progress,
        captured_jobs,
        captured_rasterizer,
    ) = captured["pdf_to_images"]
    assert captured_input == input_pdf
    assert captured_dpi == 100
    assert captured_progress is False
    assert captured_out_dir.name.startswith("betteria-pages-")
    assert captured_jobs == 1
    assert captured_rasterizer == "pdftocairo"

    assert converted["output"] == output_pdf
    assert all(path.suffix == ".tiff" for path in converted["paths"])


def test_betteria_default_output_path(monkeypatch, tmp_path):
    input_pdf = tmp_path / "letter.pdf"
    input_pdf.write_bytes(b"%PDF")

    png_dir = tmp_path / "pngs"
    png_dir.mkdir()
    png_paths = [png_dir / "page-1.png", png_dir / "page-2.png"]
    for path in png_paths:
        path.write_bytes(b"PNG")

    monkeypatch.setattr(cli, "pdf_to_images", lambda *_, **__: png_paths)

    def fake_whiten(png_path, tiff_path, **_):
        Path(tiff_path).write_bytes(b"TIFF")

    monkeypatch.setattr(cli, "whiten_and_save_as_tiff", fake_whiten)

    captured: dict[str, object] = {}

    def fake_convert(tiff_paths, output_pdf_path):
        captured["paths"] = list(map(Path, tiff_paths))
        captured["output"] = Path(output_pdf_path)

    monkeypatch.setattr(cli, "convert_tiffs_to_pdf", fake_convert)

    cli.betteria(
        input_pdf=input_pdf,
        output_pdf=None,
        dpi=150,
        threshold=128,
        use_adaptive=True,
        block_size=31,
        c_val=15,
        invert=False,
        show_progress=False,
        jobs=1,
    )

    expected_output = input_pdf.with_name(f"{input_pdf.stem}-enhanced.pdf")
    assert captured["output"] == expected_output
    assert all(path.suffix == ".tiff" for path in captured["paths"])


def test_main_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["betteria", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "betteria" in captured.out
    assert cli.__version__ in captured.out


def test_main_accepts_auto_jobs(monkeypatch):
    captured: dict[str, object] = {}

    def fake_betteria(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "betteria", fake_betteria)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "--input",
            "input.pdf",
            "--output",
            "output.pdf",
            "--jobs",
            "auto",
        ],
    )

    cli.main()

    assert captured["jobs"] == 0
    assert captured["rasterizer"] == "pdftocairo"


def test_main_accepts_numeric_jobs(monkeypatch):
    captured: dict[str, object] = {}

    def fake_betteria(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "betteria", fake_betteria)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "--input",
            "input.pdf",
            "--jobs",
            "3",
        ],
    )

    cli.main()

    assert captured["jobs"] == 3
    assert captured["rasterizer"] == "pdftocairo"


def test_main_uses_default_output(monkeypatch):
    captured: dict[str, object] = {}

    def fake_betteria(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "betteria", fake_betteria)
    monkeypatch.setattr(sys, "argv", ["betteria", "--input", "docs/invoice.pdf"])

    cli.main()

    assert captured["output_pdf"] is None
    assert captured["rasterizer"] == "pdftocairo"


def test_main_accepts_rasterizer(monkeypatch):
    captured: dict[str, object] = {}

    def fake_betteria(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "betteria", fake_betteria)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "betteria",
            "--input",
            "input.pdf",
            "--rasterizer",
            "pdftocairo",
        ],
    )

    cli.main()

    assert captured["rasterizer"] == "pdftocairo"
