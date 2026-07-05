from __future__ import annotations

from pathlib import Path

from jobradar.config import Settings


def extract_text_from_image(settings: Settings, image_path: str | Path, lang: str = "eng+kor") -> tuple[bool, str, str]:
    """Optional OCR. Works only when Tesseract binary and pytesseract are installed."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image
    except Exception as exc:
        return False, "", f"pytesseract/Pillow 사용 불가: {exc}"

    tesseract_cmd = getattr(settings, "tesseract_cmd", "")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        text = pytesseract.image_to_string(Image.open(image_path), lang=lang)
        return True, text.strip(), "OK"
    except Exception as exc:
        return False, "", str(exc)
