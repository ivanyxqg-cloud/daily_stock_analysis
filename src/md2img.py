# -*- coding: utf-8 -*-
"""
===================================
Markdown 转图片工具模块
===================================

将 Markdown 转为 PNG 图片（用于不支持 Markdown 的通知渠道）。
支持 wkhtmltoimage (imgkit) 与 markdown-to-file (m2f)，后者对 emoji 支持更好 (Issue #455)。

Security note: imgkit passes HTML to wkhtmltoimage via stdin, not argv, so
command injection from content is not applicable. Output is rasterized to PNG
(no script execution). Input is from system-generated reports, not raw user
input. Risk is considered low for the current use case.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional

from src.formatters import markdown_to_html_document

logger = logging.getLogger(__name__)


def _detect_chrome_executable() -> Optional[str]:
    """Find a local Chrome/Chromium executable for markdown-to-file."""
    configured = os.getenv("MD2IMG_CHROME_PATH", "").strip()
    candidates = [
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _trim_png_whitespace(image_bytes: bytes, *, padding: int = 24) -> bytes:
    try:
        from PIL import Image, ImageChops
    except Exception:
        return image_bytes

    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        background = Image.new("RGB", image.size, image.getpixel((0, 0)))
        diff = ImageChops.difference(image, background)
        bbox = diff.getbbox()
        if not bbox:
            return image_bytes
        left = max(bbox[0] - padding, 0)
        top = max(bbox[1] - padding, 0)
        right = min(bbox[2] + padding, image.size[0])
        bottom = min(bbox[3] + padding, image.size[1])
        cropped = image.crop((left, top, right, bottom))
        output = BytesIO()
        cropped.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return image_bytes


def _markdown_to_image_m2f(markdown_text: str) -> Optional[bytes]:
    """Convert Markdown to PNG via markdown-to-file (m2f) CLI. Better emoji support (Issue #455)."""
    if shutil.which("m2f") is None:
        logger.warning(
            "m2f (markdown-to-file) not found in PATH. "
            "Install with: npm i -g markdown-to-file. Fallback to text."
        )
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        command = ["m2f", md_path, "png", f"outputDirectory={temp_dir}"]
        chrome_path = _detect_chrome_executable()
        if chrome_path:
            command.append(f"executablePath={chrome_path}")

        result = subprocess.run(
            command,
            capture_output=True,
            timeout=60,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning(
                "m2f conversion failed: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            return None

        with open(png_path, "rb") as f:
            return _trim_png_whitespace(f.read())
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s)")
        return None
    except Exception as e:
        logger.warning("markdown_to_image (m2f) failed: %s", e)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, e)


def _markdown_to_image_chrome(markdown_text: str) -> Optional[bytes]:
    """Convert Markdown to PNG via an installed Chrome/Chromium binary."""
    chrome_path = _detect_chrome_executable()
    if not chrome_path:
        logger.warning("Chrome/Chromium executable not found, markdown_to_image unavailable")
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        html_path = os.path.join(temp_dir, "report.html")
        png_path = os.path.join(temp_dir, "report.png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(markdown_to_html_document(markdown_text))

        result = subprocess.run(
            [
                chrome_path,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--disable-dev-shm-usage",
                "--window-size=900,1800",
                f"--screenshot={png_path}",
                Path(html_path).as_uri(),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning(
                "Chrome screenshot conversion failed: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            return None

        with open(png_path, "rb") as f:
            return _trim_png_whitespace(f.read())
    except subprocess.TimeoutExpired:
        logger.warning("Chrome screenshot conversion timed out (60s)")
        return None
    except Exception as e:
        logger.warning("markdown_to_image (Chrome) failed: %s", e)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, e)


def _markdown_to_image_wkhtml(markdown_text: str) -> Optional[bytes]:
    """Convert Markdown to PNG via imgkit/wkhtmltoimage."""
    try:
        import imgkit
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    html = markdown_to_html_document(markdown_text)
    try:
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "quiet": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as e:
        if "wkhtmltoimage" in str(e).lower() or "wkhtmltopdf" in str(e).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", e)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", e)
        return None
    except Exception as e:
        logger.warning("markdown_to_image conversion failed: %s", e)
        return None


def markdown_to_image(markdown_text: str, max_chars: int = 15000) -> Optional[bytes]:
    """
    Convert Markdown to PNG image bytes.

    Engine is read from config.md2img_engine: wkhtmltoimage (default) or
    markdown-to-file (better emoji support, Issue #455).

    When conversion fails or dependencies unavailable, returns None so caller
    can fall back to text sending.

    Args:
        markdown_text: Raw Markdown content.
        max_chars: Skip conversion and return None if content exceeds this length
            (avoids huge images). Default 15000.

    Returns:
        PNG bytes, or None if conversion fails or dependencies unavailable.
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text),
            max_chars,
        )
        return None

    try:
        from src.config import get_config

        engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
    except Exception:
        engine = "wkhtmltoimage"

    if engine == "markdown-to-file":
        return _markdown_to_image_chrome(markdown_text) or _markdown_to_image_m2f(markdown_text)
    return _markdown_to_image_wkhtml(markdown_text) or _markdown_to_image_chrome(markdown_text)
