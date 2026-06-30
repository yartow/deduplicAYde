"""Local image analysis: blur/edge scoring (OpenCV) + receipt OCR (Tesseract).

Returns a DetectionResult for each image file.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract

_BLUR_THRESHOLD = float(os.environ.get("BLUR_THRESHOLD", "100"))
_EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))
_OCR_DENSITY_THRESHOLD = float(os.environ.get("OCR_DENSITY_THRESHOLD", "0.001"))

_RECEIPT_KEYWORDS = re.compile(
    r"\b(total|subtotal|tax|receipt|invoice|amount|cash|change|payment|"
    r"visa|mastercard|amex|debit|credit|purchase|order|item|qty|price)\b",
    re.IGNORECASE,
)
_PRICE_PATTERN = re.compile(r"\$\s*\d+\.\d{2}|\d+\.\d{2}\s*(?:usd|cad)?", re.IGNORECASE)

_SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_HEIC = {".heic", ".heif"}


@dataclass
class DetectionResult:
    blur_score: float
    edge_density: float
    ocr_text_density: float
    label: str  # 'receipt' | 'vague' | 'ok'
    error: str | None = None


def _open_cv_image(path: Path) -> np.ndarray | None:
    suffix = path.suffix.lower()

    if suffix in _HEIC:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
            from PIL import Image
            import io
            img_pil = Image.open(path).convert("RGB")
            arr = np.array(img_pil)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    img = cv2.imread(str(path))
    return img


def analyze(path: Path) -> DetectionResult:
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED | _HEIC:
        return DetectionResult(0.0, 0.0, 0.0, "ok", error="unsupported_format")

    img = _open_cv_image(path)
    if img is None:
        return DetectionResult(0.0, 0.0, 0.0, "ok", error="open_failed")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Blur: Laplacian variance — low = blurry
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # Edge density: ratio of Canny edge pixels to total pixels
    edges = cv2.Canny(gray, 50, 150)
    edge_density = float(np.count_nonzero(edges)) / (h * w)

    # OCR text density
    ocr_text_density, is_receipt_text = _ocr_score(img, h, w)

    # Classification
    label = _classify(blur_score, edge_density, ocr_text_density, is_receipt_text)

    return DetectionResult(
        blur_score=blur_score,
        edge_density=edge_density,
        ocr_text_density=ocr_text_density,
        label=label,
    )


def _ocr_score(img: np.ndarray, h: int, w: int) -> tuple[float, bool]:
    """Return (text_density, is_receipt_by_keywords)."""
    try:
        # Downscale large images for speed — OCR doesn't need full res
        scale = min(1.0, 1500 / max(h, w))
        if scale < 1.0:
            small = cv2.resize(img, (int(w * scale), int(h * scale)))
        else:
            small = img

        text = pytesseract.image_to_string(small, timeout=15)
        char_count = len(text.strip())
        density = char_count / (h * w)

        keyword_hit = bool(_RECEIPT_KEYWORDS.search(text)) or bool(_PRICE_PATTERN.search(text))
        return density, keyword_hit
    except Exception:
        return 0.0, False


def _classify(
    blur_score: float,
    edge_density: float,
    ocr_text_density: float,
    is_receipt_text: bool,
) -> str:
    # Receipt: high text density OR strong keyword match
    if ocr_text_density > _OCR_DENSITY_THRESHOLD or is_receipt_text:
        return "receipt"

    # Vague: blurry AND low visual content
    if blur_score < _BLUR_THRESHOLD and edge_density < _EDGE_THRESHOLD:
        return "vague"

    return "ok"
