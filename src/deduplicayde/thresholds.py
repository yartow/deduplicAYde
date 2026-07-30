"""Detection threshold env vars, split out from detection.py so modules that
only need the cutoff values (e.g. review_app.py) don't have to import cv2/
pytesseract along with them — the review service's image doesn't have those
installed, it only ever needed sqlite/FastAPI before."""
import os

BLUR_THRESHOLD = float(os.environ.get("BLUR_THRESHOLD", "100"))
EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))
OCR_DENSITY_THRESHOLD = float(os.environ.get("OCR_DENSITY_THRESHOLD", "0.001"))
