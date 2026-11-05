"""captcha_solver.py — Free, local OCR-based CAPTCHA solver using EasyOCR.

Strategy
--------
1. Detect the CAPTCHA image element on the page (various CSS selector patterns).
2. Screenshot just that element to a bytes buffer.
3. Pre-process with Pillow / OpenCV: grayscale → adaptive threshold → denoise →
   upscale 2× so EasyOCR has more pixels to work with.
4. Run EasyOCR (CPU, English+digits) and pick the highest-confidence word that
   looks like a CAPTCHA code (4–8 alphanumeric chars).
5. Locate the CAPTCHA text input and fill it.
6. Expose a ``fill_captcha`` helper with automatic retry that refreshes the
   CAPTCHA image on each attempt.
7. If all retries are exhausted, fall back to a manual pause (same behaviour as
   the original code) so the user can solve it themselves.
"""
from __future__ import annotations

import io
import logging
import re
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy EasyOCR reader (expensive to initialise — reuse across calls)
# ---------------------------------------------------------------------------
_reader = None  # type: ignore[assignment]


def _get_reader():
    """Return a cached EasyOCR Reader instance (initialised on first call)."""
    global _reader  # noqa: PLW0603
    if _reader is None:
        try:
            import easyocr  # noqa: PLC0415
            logger.info("Initialising EasyOCR reader (first run downloads ~100 MB model)…")
            _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            logger.info("EasyOCR reader ready.")
        except ImportError as exc:
            raise RuntimeError(
                "EasyOCR is not installed. Run: pip install easyocr"
            ) from exc
    return _reader


# ---------------------------------------------------------------------------
# CAPTCHA element detection
# ---------------------------------------------------------------------------

# Ordered list of CSS selectors tried for the CAPTCHA *image* element
_CAPTCHA_IMAGE_SELECTORS = [
    "#captchaImage",
    "img#captchaImage",
    "img.captcha-img",
    "img[src*='captcha' i]",
    "img[id*='captcha' i]",
    "img[alt*='captcha' i]",
    "img[class*='captcha' i]",
    "div[id*='captcha' i] img",
    "div[class*='captcha' i] img",
    "canvas[id*='captcha' i]",
]

# Ordered list of CSS selectors tried for the CAPTCHA *text input*
_CAPTCHA_INPUT_SELECTORS = [
    "#externalCaptcha",
    "input#externalCaptcha",
    "input[placeholder*='Enter CAPTCHA' i]",
    "input[id*='captcha' i]",
    "input[name*='captcha' i]",
    "input[placeholder*='captcha' i]",
    "input[placeholder*='enter' i][type='text']",
    "div[class*='captcha' i] input[type='text']",
    "div[id*='captcha' i] input[type='text']",
]

# Selectors for a "refresh / reload CAPTCHA" clickable element
_CAPTCHA_REFRESH_SELECTORS = [
    "button#captchaImg",
    "#captchaImg",
    "button:has-text('⟳')",
    "img[id*='refresh' i][alt*='captcha' i]",
    "a[id*='captcha' i][id*='refresh' i]",
    "a[href*='captcha' i][title*='refresh' i]",
    "button[id*='captcha' i][id*='refresh' i]",
    "span[id*='captcha' i][id*='refresh' i]",
    "[onclick*='refreshCaptcha' i]",
    "[onclick*='reloadCaptcha' i]",
    "[onclick*='generateCaptcha' i]",
    "img[onclick*='captcha' i]",
    "#refreshBtn",
]


def _find_first(page, selectors: list[str]):
    """Return the first Playwright Locator from ``selectors`` that has exactly
    one matching element, or ``None`` if none match."""
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() >= 1:
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def detect_captcha_image(page):
    """Return a Locator for the CAPTCHA image, or None if not found."""
    return _find_first(page, _CAPTCHA_IMAGE_SELECTORS)


def detect_captcha_input(page):
    """Return a Locator for the CAPTCHA text input, or None if not found."""
    return _find_first(page, _CAPTCHA_INPUT_SELECTORS)


def is_captcha_present(page) -> bool:
    """Quick check — returns True if any CAPTCHA-like element is visible."""
    try:
        result = page.evaluate("""() => {
          const visible = el => {
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' &&
                   r.width > 0 && r.height > 0;
          };
          const selectors = [
            'iframe[src*="captcha" i]',
            'iframe[src*="recaptcha" i]',
            'iframe[src*="hcaptcha" i]',
            'img[src*="captcha" i]',
            'img[id*="captcha" i]',
            'input[name*="captcha" i]',
            'input[id*="captcha" i]',
            '[class*="captcha" i]',
            '[id*="captcha" i]'
          ];
          if (selectors.some(s => [...document.querySelectorAll(s)].some(visible)))
            return true;
          const text = (document.body?.innerText || '').toLowerCase();
          return /verify you are human|enter the characters|enter captcha|captcha verification|security challenge/.test(text);
        }""")
        return bool(result)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Image pre-processing for OCR
# ---------------------------------------------------------------------------

def _pil_to_cv2_gray(img: Image.Image) -> np.ndarray:
    """Convert a PIL image to a uint8 grayscale NumPy array (for OpenCV)."""
    import cv2  # noqa: PLC0415
    rgb = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return gray


def preprocess_captcha_image(img: Image.Image) -> Image.Image:
    """Apply a preprocessing pipeline to improve OCR accuracy on CAPTCHA images.

    Steps:
    1. Convert to grayscale.
    2. Upscale 2× (more pixels = more OCR accuracy).
    3. Adaptive threshold (binarise) using OpenCV.
    4. Slight median-blur to remove noise.
    5. Return as PIL image.
    """
    try:
        import cv2  # noqa: PLC0415
        gray = _pil_to_cv2_gray(img)
        # Upscale
        h, w = gray.shape
        big = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        # Adaptive threshold
        thresh = cv2.adaptiveThreshold(
            big, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=15,
            C=8,
        )
        # Denoise
        denoised = cv2.medianBlur(thresh, 3)
        return Image.fromarray(denoised)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenCV preprocessing failed (%s); using basic Pillow fallback.", exc)
        # Pillow fallback: grayscale + contrast enhance
        gray_img = ImageOps.grayscale(img)
        large = gray_img.resize((gray_img.width * 2, gray_img.height * 2), Image.LANCZOS)
        sharpened = large.filter(ImageFilter.SHARPEN)
        return sharpened


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_ALPHANUM = re.compile(r"[^A-Za-z0-9]")


def ocr_captcha(img: Image.Image, reader=None) -> str:
    """Run EasyOCR on *img* and return the best CAPTCHA text candidate.

    Returns an empty string if nothing plausible is found.
    """
    if reader is None:
        reader = _get_reader()

    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    try:
        results = reader.readtext(img_bytes.read(), detail=1, paragraph=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EasyOCR failed: %s", exc)
        return ""

    # results: list of ([bbox], text, confidence)
    # Pick the candidate with the highest confidence that looks like a CAPTCHA code
    candidates = []
    for _, text, confidence in results:
        cleaned = _ALPHANUM.sub("", text).strip()
        if 3 <= len(cleaned) <= 10:
            candidates.append((confidence, cleaned))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_text = candidates[0][1]
    logger.debug("OCR candidates: %s → chose %r", candidates, best_text)
    return best_text


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

def screenshot_element(locator) -> Optional[Image.Image]:
    """Screenshot a single Playwright Locator and return a PIL Image."""
    try:
        raw = locator.screenshot()
        return Image.open(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not screenshot CAPTCHA element: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Refresh CAPTCHA
# ---------------------------------------------------------------------------

def refresh_captcha(page) -> bool:
    """Click the CAPTCHA refresh/reload control if present.

    Returns True if a refresh element was found and clicked.
    """
    loc = _find_first(page, _CAPTCHA_REFRESH_SELECTORS)
    if loc is None:
        # Try reloading the CAPTCHA image src by appending a cache-buster
        img_loc = detect_captcha_image(page)
        if img_loc:
            try:
                page.evaluate("""(imgEl) => {
                    const src = imgEl.src;
                    const sep = src.includes('?') ? '&' : '?';
                    imgEl.src = src.replace(/([?&]_cache=\\d+)/, '') + sep + '_cache=' + Date.now();
                }""", img_loc.element_handle())
                time.sleep(0.5)
                return True
            except Exception:  # noqa: BLE001
                pass
        return False
    try:
        loc.click()
        time.sleep(0.5)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not click CAPTCHA refresh: %s", exc)
        return False


# ---------------------------------------------------------------------------
# High-level: fill_captcha with retry
# ---------------------------------------------------------------------------

def fill_captcha(
    page,
    max_retries: int = 5,
    retry_delay: float = 1.0,
    reader=None,
    progress_callback=None,
) -> bool:
    """Detect, solve, and fill the CAPTCHA on *page*.

    Parameters
    ----------
    page:
        A Playwright ``Page`` object.
    max_retries:
        How many OCR attempts before giving up and falling back to manual entry.
    retry_delay:
        Seconds to wait between retries.
    reader:
        An EasyOCR Reader instance (or None to use the cached singleton).
    progress_callback:
        Optional ``callable(msg: str)`` for status messages.

    Returns
    -------
    bool
        ``True`` if the CAPTCHA was filled successfully (auto or manual),
        ``False`` if all retries failed and the user did not provide input.
    """

    def _log(msg: str) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    for attempt in range(1, max_retries + 1):
        if not is_captcha_present(page):
            _log("No CAPTCHA detected on page — skipping.")
            return True

        if reader is None:
            try:
                reader = _get_reader()
            except RuntimeError as exc:
                _log(f"EasyOCR unavailable ({exc}); falling back to manual CAPTCHA.")
                return _manual_fallback(page)

        _log(f"CAPTCHA solve attempt {attempt}/{max_retries}…")

        img_loc = detect_captcha_image(page)
        if img_loc is None:
            _log("Could not locate CAPTCHA image element.")
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            break

        # Screenshot the CAPTCHA image
        raw_img = screenshot_element(img_loc)
        if raw_img is None:
            _log("Could not screenshot CAPTCHA image.")
            if attempt < max_retries:
                refresh_captcha(page)
                time.sleep(retry_delay)
                continue
            break

        # Pre-process + OCR
        processed = preprocess_captcha_image(raw_img)
        code = ocr_captcha(processed, reader)
        if not code:
            _log("OCR returned empty result — refreshing CAPTCHA.")
            refresh_captcha(page)
            time.sleep(retry_delay)
            continue

        _log(f"OCR result: '{code}' — filling input…")

        # Find and fill the input
        input_loc = detect_captcha_input(page)
        if input_loc is None:
            _log("Could not locate CAPTCHA input field.")
            time.sleep(retry_delay)
            continue

        try:
            input_loc.click()
            input_loc.fill("")
            input_loc.type(code, delay=50)
        except Exception as exc:  # noqa: BLE001
            _log(f"Error filling CAPTCHA input: {exc}")
            time.sleep(retry_delay)
            continue

        _log(f"Filled CAPTCHA with: '{code}'")
        return True

    # All retries exhausted — fall back to manual
    _log(
        f"\n{'='*60}\n"
        "⚠  Auto CAPTCHA solving failed after all retries.\n"
        "   Solve the CAPTCHA manually in the browser window,\n"
        "   then press Enter here to continue.\n"
        f"{'='*60}"
    )
    return _manual_fallback(page)


def _manual_fallback(page, timeout_sec: int = 5) -> bool:
    """In web / automated mode, do NOT block on stdin input().
    Instead, wait briefly and allow the crawler loop to continue."""
    logger.info("Auto CAPTCHA exhausted; continuing crawl loop.")
    return False
