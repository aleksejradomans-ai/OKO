import io
import logging
import os
from pypdf import PdfReader

from rag_config import TESSERACT_CMD

logger = logging.getLogger("oko_rag.loader")

try:
    import fitz  # PyMuPDF, только для рендера страниц в OCR-ветке
    import pytesseract
    from PIL import Image
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

OCR_LANG = "rus+eng"
OCR_MIN_CHARS = 20  # если pypdf вытащил меньше символов со страницы - считаем её сканом


# RAG_READABILITY_GUARD: нечитаемые документы (кириллица потеряна при
# генерации PDF, шрифт без кириллицы -> символы-заменители) не должны
# попадать в RAG: они занимают top_k и ничего не отвечают.
GARBAGE_CHARS = "\u25a0\ufffd"  # ■ и �
GARBAGE_MAX_RATIO = 0.20  # доля мусорных символов среди непробельных


class UnreadableDocumentError(ValueError):
    """Документ прочитан, но текст непригоден для индексации."""


def garbage_ratio(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    return sum(1 for c in visible if c in GARBAGE_CHARS) / len(visible)


def check_readable(text: str, name: str = "") -> None:
    ratio = garbage_ratio(text)
    if ratio > GARBAGE_MAX_RATIO:
        logger.warning(
            "[UNREADABLE_DOCUMENT] %s: %.0f%% символов-заменителей (порог %.0f%%), "
            "документ не будет проиндексирован",
            name, ratio * 100, GARBAGE_MAX_RATIO * 100,
        )
        raise UnreadableDocumentError(
            f"документ нечитаем: {ratio:.0%} символов-заменителей "
            f"(вероятно, шрифт без кириллицы при создании файла): {name}"
        )


def _require_ocr() -> None:
    if not OCR_AVAILABLE:
        raise RuntimeError(
            "OCR-зависимости не установлены: pip install pymupdf pytesseract pillow. "
            "Плюс бинарник Tesseract отдельно (не через pip): "
            "https://github.com/UB-Mannheim/tesseract/wiki"
        )


def read_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _ocr_image(img: "Image.Image") -> str:
    return pytesseract.image_to_string(img, lang=OCR_LANG)


def _ocr_pdf(path: str) -> str:
    _require_ocr()
    doc = fitz.open(path)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text = _ocr_image(img)
        if text:
            pages.append(text)
    doc.close()
    return "\n".join(pages)


def read_pdf(path: str) -> str:
    """Сначала обычный текстовый парсинг постранично. Если страница пустая
    (меньше OCR_MIN_CHARS символов) - весь файл прогоняется через OCR."""
    reader = PdfReader(path)
    pages = []
    needs_ocr = False

    for page in reader.pages:
        t = page.extract_text()
        if t and len(t.strip()) >= OCR_MIN_CHARS:
            pages.append(t)
        else:
            needs_ocr = True

    text = "\n".join(pages)

    if needs_ocr and len(text.strip()) < OCR_MIN_CHARS:
        logger.info("File %s: scanned as image (OCR used)", os.path.basename(path))
        return _ocr_pdf(path)

    logger.info("File %s: processed via pypdf", os.path.basename(path))
    return text


def read_image(path: str) -> str:
    _require_ocr()
    logger.info("File %s: image OCR", os.path.basename(path))
    img = Image.open(path)
    return _ocr_image(img)


def load_document(path: str) -> str:
    """RAG_READABILITY_GUARD: читает документ и отбраковывает нечитаемый текст."""
    text = _load_document_raw(path)
    check_readable(text, os.path.basename(path))
    return text


def _load_document_raw(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext in (".txt", ".md"):
        return read_txt(path)
    if ext in (".png", ".jpg", ".jpeg"):
        return read_image(path)
    raise ValueError(f"Неподдерживаемый формат: {ext}")


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Простой чанкинг по символам со скользящим окном. Без зависимостей от langchain."""
    chunks = []
    start = 0
    length = len(text)
    step = max(chunk_size - overlap, 1)

    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += step

    return chunks
