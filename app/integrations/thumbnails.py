"""Worker-only bounded thumbnails; DOCX previews explicitly use parsed text layout."""

from __future__ import annotations

import io
import math
import threading
from pathlib import Path

from PIL import Image

from app.integrations.documents import DocumentAdapter, DocumentError

# PDFium is not thread-safe, including calls made for separate documents.
# Keep document creation, rendering and close within the same process-wide lock.
_PDFIUM_LOCK = threading.Lock()
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_PREVIEW_CHARACTERS = 12000


def _render_first_page(raw: bytes) -> bytes:
    with _PDFIUM_LOCK:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(raw) as document:
            if not 1 <= len(document) <= 200:
                raise DocumentError("thumbnail_page_limit", "缩略图文件页数不符合要求。")
            page = document[0]
            width, height = page.get_size()
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise DocumentError("thumbnail_invalid_page", "文件首页尺寸无效。")
            scale = min(480 / width, 720 / height)
            bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
            try:
                # Detach Pillow storage from PDFium before closing native handles.
                image = bitmap.to_pil().convert("RGB")
                try:
                    image.thumbnail((480, 720), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="PNG", optimize=True)
                    return output.getvalue()
                finally:
                    image.close()
            finally:
                bitmap.close()
                page.close()


def create_thumbnail(filename: str, raw: bytes, *, text: str) -> tuple[bytes, str, list[str]]:
    """Run in the thumbnail worker thread, after successful bounded text extraction.

    PDF retains its original first-page appearance. DOCX, Markdown and plain text
    are explicitly labelled parsed previews, never representations of source layout.
    Scanned PDFs remain unsupported by DocumentAdapter's existing OCR policy.
    """
    if not raw or len(raw) > _MAX_UPLOAD_BYTES:
        raise DocumentError("thumbnail_size_limit", "缩略图源文件为空或超过大小限制。")
    extension = Path(filename).suffix.lower()
    warnings: list[str] = []
    try:
        if extension == ".pdf":
            if not raw.startswith(b"%PDF-"):
                raise DocumentError("thumbnail_signature_mismatch", "PDF 文件签名不匹配。")
            if not text.strip():
                raise DocumentError("document_has_no_text", "扫描 PDF 请先完成文字识别。")
            pdf = raw
            kind = "original_first_page"
        elif extension in {".docx", ".md", ".txt"}:
            if not text.strip():
                raise DocumentError("thumbnail_no_text", "没有可用于生成缩略图的解析正文。")
            adapter = DocumentAdapter()
            # Validate the original signature/container too; a nonempty supplied string
            # must not turn a malformed source document into a successful thumbnail.
            adapter.parse_upload(filename, raw)
            pdf = adapter.export(text[:_MAX_PREVIEW_CHARACTERS], Path(filename).stem, "pdf")
            warnings.extend(adapter.warnings)
            warnings.append("thumbnail_parsed_preview_not_original_layout")
            if len(text) > _MAX_PREVIEW_CHARACTERS:
                warnings.append("thumbnail_preview_text_truncated")
            kind = "parsed_preview"
        else:
            raise DocumentError("thumbnail_unsupported_format", "当前文件格式不支持缩略图。")
        return _render_first_page(pdf), kind, sorted(set(warnings))
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("thumbnail_render_failed", "文件首页缩略图生成失败，请稍后重试。") from None
