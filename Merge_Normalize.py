def normalize_manufacturer(name):
    mapping = {
        "Zimmer Biomet": "Zimmer_Biomet",
        "Zimmer": "Zimmer_Biomet",
        "Biomet": "Zimmer_Biomet",
        "Stryker": "Stryker",
        "DePuy": "DePuy_Synthes"
    }
    for k,v in mapping.items():
        if k.lower() in str(name).lower():
            return v
    return name

output_df['ManufacturerNorm'] = output_df['CompanyName'].apply(normalize_manufacturer)
output_df.to_csv("cjrr_implant_library_v1.csv", index=False)

## Updated merge?? 
cjrr_df = pd.read_csv("cjrr_gudid_enriched.csv")
pdf_df = pd.read_csv("pdf_extracted_data.csv")

# Simple fuzzy merge
combined = pd.merge(cjrr_df, pdf_df, how="outer", on="CatalogueNumber")
combined.to_csv("implant_library_combined.csv", index=False)
