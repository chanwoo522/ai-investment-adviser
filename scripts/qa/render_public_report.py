from __future__ import annotations

import argparse
import contextlib
import html
import http.server
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote


EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
DESKTOP_WIDTH = 1440
MOBILE_WIDTH = 390
MIN_CAPTURE_HEIGHT = 800
MAX_CAPTURE_HEIGHT = 30_000
SCREENSHOT_BOTTOM_PADDING = 24
A4_WIDTH_POINTS = 595.28
A4_HEIGHT_POINTS = 841.89
A4_POINT_TOLERANCE = 4.0


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _browser_executable(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"browser executable not found: {path}")
        return _versioned_edge_binary(path)
    for path in _installed_edge_binaries() + EDGE_CANDIDATES + CHROME_CANDIDATES:
        if path.is_file():
            return path
    raise FileNotFoundError("no supported Chromium browser executable was found")


def _version_key(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in path.parent.name.split("."))
    except ValueError:
        return ()


def _installed_edge_binaries() -> tuple[Path, ...]:
    binaries: list[Path] = []
    for launcher in EDGE_CANDIDATES:
        application_dir = launcher.parent
        if not application_dir.is_dir():
            continue
        binaries.extend(application_dir.glob("*.*.*.*/msedge.exe"))
    return tuple(sorted(set(binaries), key=_version_key, reverse=True))


def _versioned_edge_binary(path: Path) -> Path:
    if path.name.lower() != "msedge.exe" or _version_key(path):
        return path
    siblings = tuple(
        sorted(path.parent.glob("*.*.*.*/msedge.exe"), key=_version_key, reverse=True)
    )
    return siblings[0].resolve() if siblings else path


def _pdftoppm_executable(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"pdftoppm executable not found: {path}")
        return path

    on_path = shutil.which("pdftoppm")
    if on_path:
        return Path(on_path).resolve()

    user_profile = os.environ.get("USERPROFILE")
    candidates: tuple[Path, ...] = ()
    if user_profile:
        candidates = (
            Path(user_profile)
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "native"
            / "poppler"
            / "Library"
            / "bin"
            / "pdftoppm.exe",
        )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(
        "pdftoppm was not found; pass --pdftoppm-executable explicitly"
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def _local_server(directory: Path):
    port = _free_loopback_port()
    handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chromium_base_command(executable: Path, profile_dir: Path) -> list[str]:
    return [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-software-rasterizer",
        "--disable-dev-shm-usage",
        "--disable-features=Vulkan,SkiaGraphite",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile_dir}",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=3000",
    ]


def _run_chromium_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    invoked_command = command
    environment = None
    if os.name == "nt" and Path(command[0]).name.lower() == "msedge.exe":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise FileNotFoundError("PowerShell is required to wait for Microsoft Edge")
        environment = os.environ.copy()
        environment["CODEX_PUBLIC_RENDER_COMMAND_JSON"] = json.dumps(
            {"executable": command[0], "arguments": command[1:]}
        )
        invoked_command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$renderCommand=ConvertFrom-Json $env:CODEX_PUBLIC_RENDER_COMMAND_JSON; "
            "$renderExe=[string]$renderCommand.executable; "
            "$renderArgs=@($renderCommand.arguments); "
            "& $renderExe @renderArgs; exit $LASTEXITCODE",
        ]
    return subprocess.run(
        invoked_command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=environment,
    )


def _measurement_document(*, report_name: str, width: int) -> str:
    report_url = "/" + quote(report_name)
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><style>
html,body{{margin:0;padding:0;width:{width}px;overflow:hidden}}
iframe{{display:block;border:0;width:{width}px;height:800px}}
</style></head><body>
<script>
function measureReport() {{
  const frame = document.getElementById('report');
  const doc = frame.contentDocument;
  const root = doc.documentElement;
  const body = doc.body;
  const height = Math.max(root.scrollHeight, root.offsetHeight,
    body ? body.scrollHeight : 0, body ? body.offsetHeight : 0);
  document.getElementById('result').setAttribute('data-height', String(height));
}};
</script>
<iframe id=\"report\" onload=\"measureReport()\"
 src=\"{html.escape(report_url, quote=True)}\"></iframe>
<output id=\"result\" data-height=\"pending\"></output>
</body></html>"""


def _screenshot_document(*, report_name: str, width: int, height: int) -> str:
    """Pin the report viewport even when Windows Chromium enforces a wider window."""
    report_url = "/" + quote(report_name)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;width:{width}px;height:{height}px;overflow:hidden}}
iframe{{display:block;border:0;width:{width}px;height:{height}px}}
</style></head><body><iframe title="visual QA report" src="{html.escape(report_url, quote=True)}"></iframe></body></html>"""


def _measure_document_height(
    executable: Path,
    *,
    origin: str,
    report_name: str,
    render_root: Path,
    width: int,
) -> int:
    measurement_path = render_root / f"measure_{width}.html"
    measurement_path.write_text(
        _measurement_document(report_name=report_name, width=width), encoding="utf-8"
    )
    url = f"{origin}/{quote(render_root.name)}/{quote(measurement_path.name)}"
    command = _chromium_base_command(executable, render_root / f"measure_profile_{width}")
    command.extend([f"--window-size={width},800", "--dump-dom", url])
    completed = _run_chromium_command(command, timeout=90)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown browser failure").strip()
        raise RuntimeError(f"Chromium document-height measurement failed: {detail[:2000]}")
    match = re.search(r'data-height=["\'](\d+)["\']', completed.stdout)
    if not match:
        raise RuntimeError("Chromium did not return a document height")
    document_height = int(match.group(1))
    capture_height = max(
        MIN_CAPTURE_HEIGHT, math.ceil(document_height) + SCREENSHOT_BOTTOM_PADDING
    )
    if capture_height > MAX_CAPTURE_HEIGHT:
        raise RuntimeError(
            f"report height {capture_height}px exceeds the single-image QA limit "
            f"of {MAX_CAPTURE_HEIGHT}px"
        )
    return capture_height


def _run_browser(
    executable: Path,
    *,
    url: str,
    profile_dir: Path,
    output_path: Path,
    mode: str,
    window_size: tuple[int, int] | None = None,
) -> None:
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite rendered artifact: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = _chromium_base_command(executable, profile_dir)
    if mode == "pdf":
        command.extend(
            [
                f"--print-to-pdf={output_path}",
                "--print-to-pdf-no-header",
                "--no-pdf-header-footer",
            ]
        )
    elif mode == "screenshot":
        if window_size is None:
            raise ValueError("screenshot rendering requires a window size")
        width, height = window_size
        command.extend(
            [
                f"--window-size={width},{height}",
                f"--screenshot={output_path}",
            ]
        )
    else:
        raise ValueError(f"unsupported render mode: {mode}")
    command.append(url)
    completed = _run_chromium_command(command, timeout=90)
    if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "unknown browser failure").strip()
        raise RuntimeError(f"Chromium {mode} render failed: {detail[:2000]}")


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"invalid PNG render: {path.name}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid PNG dimensions: {path.name}")
    return int(width), int(height)


def _pdf_a4_dimensions(pdf_path: Path, pdftoppm: Path) -> tuple[float, float]:
    pdfinfo_name = "pdfinfo.exe" if os.name == "nt" else "pdfinfo"
    pdfinfo = pdftoppm.with_name(pdfinfo_name)
    if not pdfinfo.is_file():
        found = shutil.which("pdfinfo")
        if not found:
            raise FileNotFoundError("pdfinfo is required for A4 validation")
        pdfinfo = Path(found).resolve()
    completed = subprocess.run(
        [str(pdfinfo), str(pdf_path)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown pdfinfo failure").strip()
        raise RuntimeError(f"pdfinfo failed: {detail[:2000]}")
    match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts(?:\s+\(A4\))?",
        completed.stdout,
        flags=re.MULTILINE,
    )
    if not match:
        raise RuntimeError("pdfinfo did not report a page size")
    width, height = float(match.group(1)), float(match.group(2))
    if not (
        abs(width - A4_WIDTH_POINTS) <= A4_POINT_TOLERANCE
        and abs(height - A4_HEIGHT_POINTS) <= A4_POINT_TOLERANCE
    ):
        raise RuntimeError(f"public PDF is not portrait A4: {width:.2f} x {height:.2f} pts")
    return width, height


def _render_pdf_pages(
    pdftoppm: Path, *, pdf_path: Path, output_dir: Path, dpi: int
) -> list[Path]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite PDF page renders: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    prefix = output_dir / "page"
    completed = subprocess.run(
        [str(pdftoppm), "-png", "-r", str(dpi), str(pdf_path), str(prefix)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
    )
    pages = sorted(output_dir.glob("page-*.png"))
    if completed.returncode != 0 or not pages:
        detail = (completed.stderr or completed.stdout or "no page images created").strip()
        raise RuntimeError(f"Poppler PDF page rendering failed: {detail[:2000]}")
    for page in pages:
        _png_dimensions(page)
    return pages


def _publish_file_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            created = True
            shutil.copyfileobj(reader, writer)
    except Exception:
        if created and destination.exists():
            destination.unlink()
        raise


def _publish_directory_exclusive(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
            relative = source_file.relative_to(source)
            _publish_file_exclusive(source_file, destination / relative)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def render_public_report(
    html_path: Path,
    *,
    pdf_path: Path,
    desktop_path: Path,
    mobile_path: Path,
    pdf_pages_dir: Path,
    render_metadata_path: Path,
    browser_executable: str | None = None,
    pdftoppm_executable: str | None = None,
    pdf_page_dpi: int = 144,
) -> None:
    html_path = html_path.resolve()
    if not html_path.is_file():
        raise FileNotFoundError(html_path)
    file_targets = (pdf_path, desktop_path, mobile_path, render_metadata_path)
    for target in file_targets:
        if target.exists():
            raise FileExistsError(f"refusing to overwrite immutable output: {target}")
    if pdf_pages_dir.exists():
        raise FileExistsError(f"refusing to overwrite immutable output: {pdf_pages_dir}")
    if pdf_page_dpi <= 0:
        raise ValueError("pdf_page_dpi must be positive")

    executable = _browser_executable(browser_executable)
    pdftoppm = _pdftoppm_executable(pdftoppm_executable)
    render_root = pdf_path.parent / ".render_tmp"
    if render_root.exists():
        raise FileExistsError(f"temporary render directory already exists: {render_root}")
    render_root.mkdir(parents=True, exist_ok=False)
    published_files: list[Path] = []
    published_directory = False
    try:
        temporary_pdf = render_root / "report.pdf"
        temporary_desktop = render_root / "desktop.png"
        temporary_mobile = render_root / "mobile.png"
        temporary_pages = render_root / "pdf_pages"
        with _local_server(html_path.parent) as origin:
            url = f"{origin}/{quote(html_path.name)}"
            desktop_height = _measure_document_height(
                executable,
                origin=origin,
                report_name=html_path.name,
                render_root=render_root,
                width=DESKTOP_WIDTH,
            )
            mobile_height = _measure_document_height(
                executable,
                origin=origin,
                report_name=html_path.name,
                render_root=render_root,
                width=MOBILE_WIDTH,
            )
            screenshot_urls: dict[int, str] = {}
            for width, height in (
                (DESKTOP_WIDTH, desktop_height),
                (MOBILE_WIDTH, mobile_height),
            ):
                wrapper = render_root / f"screenshot_{width}.html"
                wrapper.write_text(
                    _screenshot_document(
                        report_name=html_path.name,
                        width=width,
                        height=height,
                    ),
                    encoding="utf-8",
                )
                screenshot_urls[width] = (
                    f"{origin}/{quote(render_root.name)}/{quote(wrapper.name)}"
                )
            _run_browser(
                executable,
                url=url,
                profile_dir=render_root / "pdf_profile",
                output_path=temporary_pdf,
                mode="pdf",
            )
            _run_browser(
                executable,
                url=screenshot_urls[DESKTOP_WIDTH],
                profile_dir=render_root / "desktop_profile",
                output_path=temporary_desktop,
                mode="screenshot",
                window_size=(DESKTOP_WIDTH, desktop_height),
            )
            _run_browser(
                executable,
                url=screenshot_urls[MOBILE_WIDTH],
                profile_dir=render_root / "mobile_profile",
                output_path=temporary_mobile,
                mode="screenshot",
                window_size=(MOBILE_WIDTH, mobile_height),
            )

        desktop_dimensions = _png_dimensions(temporary_desktop)
        mobile_dimensions = _png_dimensions(temporary_mobile)
        if desktop_dimensions != (DESKTOP_WIDTH, desktop_height):
            raise RuntimeError(
                f"desktop screenshot dimensions mismatch: {desktop_dimensions}"
            )
        if mobile_dimensions != (MOBILE_WIDTH, mobile_height):
            raise RuntimeError(f"mobile screenshot dimensions mismatch: {mobile_dimensions}")
        pdf_width, pdf_height = _pdf_a4_dimensions(temporary_pdf, pdftoppm)
        pages = _render_pdf_pages(
            pdftoppm,
            pdf_path=temporary_pdf,
            output_dir=temporary_pages,
            dpi=pdf_page_dpi,
        )
        metadata = {
            "schema_version": "public-report-visual-qa/v1",
            "loopback_http_used": True,
            "browser_engine": "CHROMIUM",
            "desktop": {
                "viewport_width_px": DESKTOP_WIDTH,
                "capture_height_px": desktop_height,
                "bottom_padding_px": SCREENSHOT_BOTTOM_PADDING,
                "screenshot_width_px": desktop_dimensions[0],
                "screenshot_height_px": desktop_dimensions[1],
                "full_report_single_image": True,
            },
            "mobile": {
                "viewport_width_px": MOBILE_WIDTH,
                "capture_height_px": mobile_height,
                "bottom_padding_px": SCREENSHOT_BOTTOM_PADDING,
                "screenshot_width_px": mobile_dimensions[0],
                "screenshot_height_px": mobile_dimensions[1],
                "full_report_single_image": True,
            },
            "pdf": {
                "page_format": "A4",
                "page_width_points": pdf_width,
                "page_height_points": pdf_height,
                "page_count": len(pages),
                "rendered_page_count": len(pages),
                "page_render_dpi": pdf_page_dpi,
            },
            "overwrite_protection": True,
        }
        metadata_source = render_root / "render_metadata.json"
        metadata_source.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        for source, destination in (
            (temporary_pdf, pdf_path),
            (temporary_desktop, desktop_path),
            (temporary_mobile, mobile_path),
            (metadata_source, render_metadata_path),
        ):
            _publish_file_exclusive(source, destination)
            published_files.append(destination)
        _publish_directory_exclusive(temporary_pages, pdf_pages_dir)
        published_directory = True
    except Exception:
        for path in reversed(published_files):
            if path.exists():
                path.unlink()
        if published_directory and pdf_pages_dir.exists():
            shutil.rmtree(pdf_pages_dir, ignore_errors=True)
        raise
    finally:
        # The target is an explicitly-created run-local temporary directory.
        shutil.rmtree(render_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a privacy-reviewed public rebalance HTML over loopback HTTP."
    )
    parser.add_argument("--html", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--desktop-screenshot", required=True)
    parser.add_argument("--mobile-screenshot", required=True)
    parser.add_argument("--pdf-pages-dir", required=True)
    parser.add_argument("--render-metadata", required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--pdftoppm-executable")
    parser.add_argument("--pdf-page-dpi", type=int, default=144)
    args = parser.parse_args()

    render_public_report(
        Path(args.html),
        pdf_path=Path(args.pdf),
        desktop_path=Path(args.desktop_screenshot),
        mobile_path=Path(args.mobile_screenshot),
        pdf_pages_dir=Path(args.pdf_pages_dir),
        render_metadata_path=Path(args.render_metadata),
        browser_executable=args.browser_executable,
        pdftoppm_executable=args.pdftoppm_executable,
        pdf_page_dpi=args.pdf_page_dpi,
    )


if __name__ == "__main__":
    main()
