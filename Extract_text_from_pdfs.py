import pdfplumber
import pytesseract
from PIL import Image
import io, os

PDF_DIR = "implant_pdfs"
TEXT_DIR = "pdf_texts"
os.makedirs(TEXT_DIR, exist_ok=True)

def extract_text_from_pdf(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Try text layer first
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
            else:
                # Fallback: OCR
                image = page.to_image(resolution=300).original
                ocr_text = pytesseract.image_to_string(image)
                text += ocr_text + "\n"
    return text

for fname in os.listdir(PDF_DIR):
    if fname.endswith(".pdf"):
        fpath = os.path.join(PDF_DIR, fname)
        print(f"Extracting text from {fname}...")
        text = extract_text_from_pdf(fpath)
        outpath = os.path.join(TEXT_DIR, fname.replace(".pdf", ".txt"))
        with open(outpath, "w", encoding="utf-8") as f:
            f.write(text)

print("All PDF text extracted to ./pdf_texts/")
