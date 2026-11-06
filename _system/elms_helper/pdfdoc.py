"""PDF text extraction (bundled poppler, pypdf fallback) and answer PDF rendering."""

from __future__ import annotations

import html
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import POPPLER_DIR, TEMPLATE_PATH

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe",
]


def find_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.exists():
            return candidate
    raise RuntimeError("Microsoft Edge was not found. Install Microsoft Edge.")


def extract_pdf(path: Path, max_pages: int = 6) -> tuple[str, list[Path]]:
    """Return (text, page_images). Page images are produced for scanned PDFs."""
    text = ""
    pdftotext = POPPLER_DIR / "pdftotext.exe"
    if pdftotext.exists():
        try:
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
                txt_path = Path(handle.name)
            subprocess.run(
                [str(pdftotext), "-layout", "-enc", "UTF-8", str(path), str(txt_path)],
                check=False, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            text = txt_path.read_text(encoding="utf-8", errors="replace")
            txt_path.unlink(missing_ok=True)
        except Exception:
            text = ""
    if not text.strip():
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            text = ""

    cleaned = re.sub(r"\s+", "", text)
    images: list[Path] = []
    if len(cleaned) < 40:
        pdftoppm = POPPLER_DIR / "pdftoppm.exe"
        if pdftoppm.exists():
            out_dir = Path(tempfile.mkdtemp(prefix="elms-pages-"))
            prefix = out_dir / "page"
            try:
                subprocess.run(
                    [str(pdftoppm), "-r", "110", "-png", "-f", "1", "-l", str(max_pages),
                     str(path), str(prefix)],
                    check=False, timeout=180,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                images = sorted(out_dir.glob("page*.png"))
            except Exception:
                images = []
    return text.strip(), images


def plain_text(answer_html: str) -> str:
    # SVG is rendered in the saved PDF; keep its accompanying section headings
    # and prose in the ELMS text field without dumping XML/shape attributes there.
    text = re.sub(r"<svg\b[^>]*>.*?</svg\s*>", " [diagram included in attached answer.pdf] ",
                  answer_html or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*(p|h2|h3|li|ul|ol)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_html(title: str, meta: str, body_html: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{TITLE}}", html.escape(title))
        .replace("{{META}}", html.escape(meta))
        .replace("{{BODY}}", body_html)
    )


def html_to_pdf(page_html: str, out_pdf: Path, timeout_s: int = 90) -> Path:
    """Render HTML to PDF with Edge headless. Returns the PDF path."""
    edge = find_edge()
    out_pdf = out_pdf.resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    temp_html = Path(tempfile.gettempdir()) / f"elms-answer-{run_id}.html"
    profile = Path(tempfile.gettempdir()) / f"elms-edge-{run_id}"
    try:
        temp_html.write_text(page_html, encoding="utf-8")
        args = [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_pdf}",
            temp_html.as_uri(),
        ]
        subprocess.run(args, check=False, timeout=timeout_s,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        last_size = -1
        while time.time() < deadline:
            if out_pdf.exists():
                size = out_pdf.stat().st_size
                if size > 0 and size == last_size:
                    break
                last_size = size
            time.sleep(0.25)
        if not out_pdf.exists() or out_pdf.stat().st_size == 0:
            raise RuntimeError("PDF rendering failed (Edge produced no file).")
        return out_pdf
    finally:
        temp_html.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
