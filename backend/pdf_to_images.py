"""
pdf_to_images.py
Converts each page of a PDF into a base64-encoded PNG for vision model input.
"""

import base64
import fitz  # PyMuPDF


def pdf_to_base64_images(pdf_path: str, dpi: int = 150) -> list[str]:
    """
    Render every page of *pdf_path* at *dpi* and return a list of
    base64-encoded PNG strings (one per page).
    """
    doc = fitz.open(pdf_path)
    images: list[str] = []

    try:
        for page in doc:
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            png_bytes = pix.tobytes("png")
            images.append(base64.b64encode(png_bytes).decode("utf-8"))
    finally:
        doc.close()

    if not images:
        raise ValueError(f"PDF produced no renderable pages: {pdf_path}")

    return images
