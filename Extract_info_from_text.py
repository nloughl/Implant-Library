import re, os, pandas as pd

TEXT_DIR = "pdf_texts"

records = []

for fname in os.listdir(TEXT_DIR):
    if not fname.endswith(".txt"):
        continue
    with open(os.path.join(TEXT_DIR, fname), "r", encoding="utf-8") as f:
        text = f.read()

    # Example regex patterns:
    catalogue_nums = re.findall(r'(?:Cat(?:alogue)? No\.?|REF|Cat\.|Ref\.?)\s*[:\-]?\s*([A-Z0-9\-]{4,})', text, re.I)
    model_names = re.findall(r'(NexGen|Persona|Triathlon|Attune|Legion|Journey|Vanguard|Oxford|Evolution)', text, re.I)
    fixation = re.findall(r'(Cemented|Cementless|Porous|Press[- ]fit)', text, re.I)
    materials = re.findall(r'(Cobalt Chrome|Titanium|Polyethylene|Oxinium|Tantalum|Vitamin E)', text, re.I)

    records.append({
        "File": fname,
        "CatalogueNumbers": list(set(catalogue_nums)),
        "ModelNames": list(set(model_names)),
        "Fixation": list(set(fixation)),
        "Materials": list(set(materials))
    })

df = pd.DataFrame(records)
df.to_csv("pdf_extracted_data.csv", index=False)
print("Saved extracted info to pdf_extracted_data.csv")
