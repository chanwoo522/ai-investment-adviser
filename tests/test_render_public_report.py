from __future__ import annotations

import json
import struct
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import urlopen

from scripts.qa import render_public_report as render_module


def _fake_png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
    )


class PublicReportRendererTests(unittest.TestCase):
    def test_loopback_server_serves_only_on_loopback_origin(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "report.html").write_text("<p>ok</p>", encoding="utf-8")
            with render_module._local_server(root) as origin:
                self.assertTrue(origin.startswith("http://127.0.0.1:"))
                with urlopen(f"{origin}/report.html", timeout=3) as response:
                    self.assertEqual(response.read(), b"<p>ok</p>")

    def test_measurement_document_uses_requested_width_and_encoded_report(self) -> None:
        document = render_module._measurement_document(
            report_name="보고서 public.html", width=390
        )
        self.assertIn("width:390px", document)
        self.assertIn("/%EB%B3%B4%EA%B3%A0%EC%84%9C%20public.html", document)
        self.assertIn("data-height=\"pending\"", document)
        self.assertIn("scrollHeight", document)

    def test_screenshot_document_pins_exact_mobile_viewport(self) -> None:
        document = render_module._screenshot_document(
            report_name="보고서 public.html", width=390, height=9824
        )
        self.assertIn("width:390px", document)
        self.assertIn("height:9824px", document)
        self.assertIn("/%EB%B3%B4%EA%B3%A0%EC%84%9C%20public.html", document)

    def test_transactional_render_publishes_full_screenshots_a4_pages_and_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path = root / "public.html"
            html_path.write_text("<!doctype html><p>report</p>", encoding="utf-8")
            pdf_path = root / "public.pdf"
            desktop_path = root / "desktop.png"
            mobile_path = root / "mobile.png"
            pages_dir = root / "pdf_pages"
            metadata_path = root / "render_metadata.json"

            def fake_browser(*args, **kwargs) -> None:
                output = kwargs["output_path"]
                if kwargs["mode"] == "pdf":
                    output.write_bytes(b"%PDF-fixture")
                else:
                    width, height = kwargs["window_size"]
                    _fake_png(output, width, height)

            def fake_pages(*args, **kwargs):
                output_dir = kwargs["output_dir"]
                output_dir.mkdir(parents=True, exist_ok=False)
                pages = [output_dir / "page-1.png", output_dir / "page-2.png"]
                for page in pages:
                    _fake_png(page, 1191, 1684)
                return pages

            with (
                patch.object(render_module, "_browser_executable", return_value=Path("edge")),
                patch.object(render_module, "_pdftoppm_executable", return_value=Path("pdftoppm")),
                patch.object(
                    render_module,
                    "_measure_document_height",
                    side_effect=[4124, 9824],
                ),
                patch.object(render_module, "_run_browser", side_effect=fake_browser),
                patch.object(
                    render_module,
                    "_pdf_a4_dimensions",
                    return_value=(595.28, 841.89),
                ),
                patch.object(render_module, "_render_pdf_pages", side_effect=fake_pages),
            ):
                render_module.render_public_report(
                    html_path,
                    pdf_path=pdf_path,
                    desktop_path=desktop_path,
                    mobile_path=mobile_path,
                    pdf_pages_dir=pages_dir,
                    render_metadata_path=metadata_path,
                )

            self.assertEqual(render_module._png_dimensions(desktop_path), (1440, 4124))
            self.assertEqual(render_module._png_dimensions(mobile_path), (390, 9824))
            self.assertEqual(len(list(pages_dir.glob("page-*.png"))), 2)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(metadata["loopback_http_used"])
            self.assertTrue(metadata["desktop"]["full_report_single_image"])
            self.assertTrue(metadata["mobile"]["full_report_single_image"])
            self.assertEqual(metadata["pdf"]["page_format"], "A4")
            self.assertEqual(metadata["pdf"]["page_count"], 2)
            self.assertFalse((root / ".render_tmp").exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path = root / "public.html"
            html_path.write_text("report", encoding="utf-8")
            pdf_path = root / "public.pdf"
            pdf_path.write_bytes(b"existing")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                render_module.render_public_report(
                    html_path,
                    pdf_path=pdf_path,
                    desktop_path=root / "desktop.png",
                    mobile_path=root / "mobile.png",
                    pdf_pages_dir=root / "pdf_pages",
                    render_metadata_path=root / "metadata.json",
                )
            self.assertEqual(pdf_path.read_bytes(), b"existing")

    def test_pdf_page_size_contract_accepts_a4_and_rejects_letter(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdftoppm = root / ("pdftoppm.exe" if render_module.os.name == "nt" else "pdftoppm")
            pdfinfo = root / ("pdfinfo.exe" if render_module.os.name == "nt" else "pdfinfo")
            pdf = root / "report.pdf"
            for path in (pdftoppm, pdfinfo, pdf):
                path.write_bytes(b"fixture")

            a4 = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Page size: 595.28 x 841.89 pts (A4)\n", stderr=""
            )
            with patch.object(render_module.subprocess, "run", return_value=a4):
                self.assertEqual(
                    render_module._pdf_a4_dimensions(pdf, pdftoppm),
                    (595.28, 841.89),
                )

            letter = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Page size: 612 x 792 pts (letter)\n", stderr=""
            )
            with patch.object(render_module.subprocess, "run", return_value=letter):
                with self.assertRaisesRegex(RuntimeError, "not portrait A4"):
                    render_module._pdf_a4_dimensions(pdf, pdftoppm)

    def test_render_failure_leaves_no_partial_published_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path = root / "public.html"
            html_path.write_text("report", encoding="utf-8")
            targets = {
                "pdf_path": root / "public.pdf",
                "desktop_path": root / "desktop.png",
                "mobile_path": root / "mobile.png",
                "pdf_pages_dir": root / "pdf_pages",
                "render_metadata_path": root / "metadata.json",
            }

            def fail_browser(*args, **kwargs) -> None:
                raise RuntimeError("fixture render failure")

            with (
                patch.object(render_module, "_browser_executable", return_value=Path("edge")),
                patch.object(render_module, "_pdftoppm_executable", return_value=Path("pdftoppm")),
                patch.object(render_module, "_measure_document_height", return_value=1200),
                patch.object(render_module, "_run_browser", side_effect=fail_browser),
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture render failure"):
                    render_module.render_public_report(html_path, **targets)

            for path in targets.values():
                self.assertFalse(path.exists())
            self.assertFalse((root / ".render_tmp").exists())


if __name__ == "__main__":
    unittest.main()
