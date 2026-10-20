from pathlib import Path
import sys

import pdfplumber


def read_pdf(pdf_path: str, use_ocr_if_empty: bool = True) -> dict:
    
    path = Path(pdf_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {pdf_path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    pages = []
    tables = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
            tables.extend(page.extract_tables() or [])

    method = "text"
    full_text = "\n\n".join(pages).strip()

    # If no text was found, the PDF is probably a scan (an image).
    if not full_text and use_ocr_if_empty:
        pages = _ocr_pdf(path)
        full_text = "\n\n".join(pages).strip()
        method = "ocr"

    return {
        "file": path.name,
        "num_pages": len(pages),
        "pages": pages,
        "text": full_text,
        "tables": tables,
        "method": method,
    }


def _ocr_pdf(path: Path) -> list:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as e:
        raise RuntimeError(
            "This PDF has no text layer (it is probably scanned). "
            "Install OCR support: pip install pytesseract pdf2image"
        ) from e

    images = convert_from_path(str(path))
    return [pytesseract.image_to_string(img) for img in images]


if __name__ == "__main__":

  
    result = read_pdf("D:\\dtc\\cv porject\\dtc_cv.pdf")
    
    print(f"File:   {result['file']}")
    print(f"Pages:  {result['num_pages']}")
    print(f"Method: {result['method']}")
    print(f"Tables: {len(result['tables'])}")
    print("-" * 40)
    print(result["text"])