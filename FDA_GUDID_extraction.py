# pip install requests pandas pdfplumber pytesseract pillow openpyxl



import requests
import pandas as pd
import time

# --- Configuration ---
GUDID_API_URL = "https://accessgudid.nlm.nih.gov/api/v2/devices"

# --- Functions ---
def query_gudid(catalogue, manufacturer):
    """Query the FDA GUDID API by catalogue and manufacturer name."""
    try:
        search_query = f"{catalogue} {manufacturer}".replace(" ", "+")
        url = f"{GUDID_API_URL}?search={search_query}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get('results'):
            return None

        # Get first result
        d = data['results'][0]['device']
        return {
            "CatalogueNumber": d.get("catalogNumber"),
            "BrandName": d.get("brandName"),
            "CompanyName": d.get("companyName"),
            "DeviceDescription": d.get("deviceDescription"),
            "DeviceStatus": d.get("deviceStatus"),
            "VersionModelNumber": d.get("versionModelNumber"),
            "PrimaryDI": d.get("primaryDI"),
            "FDA_RefLink": f"https://accessgudid.nlm.nih.gov/devices/{d.get('primaryDI')}"
        }

    except Exception as e:
        print(f"Error for {catalogue}: {e}")
        return None

# --- Run Search ---
input_df = pd.read_csv("cjrr_input.csv")
results = []

for _, row in input_df.iterrows():
    cat, mfr = row['CatalogueNumber'], row['Manufacturer']
    print(f"Searching {cat} ({mfr})...")
    result = query_gudid(cat, mfr)
    if result:
        combined = {**row.to_dict(), **result}
        results.append(combined)
    time.sleep(1)  # be polite to API

output_df = pd.DataFrame(results)
output_df.to_csv("cjrr_gudid_enriched.csv", index=False)
print("Saved results to cjrr_gudid_enriched.csv")
