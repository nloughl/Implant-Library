import csv
import requests
import time

# === BASE API ENDPOINTS ===
BASE_DEVICE_URL = "https://health-products.canada.ca/api/medical-devices/device/"
BASE_IDENTIFIER_URL = "https://health-products.canada.ca/api/medical-devices/deviceidentifier/"

def get_device_info_by_identifier(identifier, cjrr_num=None, retries=2):
    """Look up a device name, manufacturer, and licence number (active + archived)."""
    def fetch_from_api(query, state=None):
        """Helper to call MDALL with optional state (e.g. 'archived')."""
        params = {"device_identifier": query}
        if state:
            params["state"] = state

        r = requests.get(BASE_IDENTIFIER_URL, params=params, timeout=10)
        if r.status_code != 200:
            return None
        try:
            data = r.json()
            return data if data else None
        except Exception:
            return None

    for attempt in range(retries + 1):
        try:
            # === Try active listing using edited catalogue number (MDALL identifer)===
            id_data = fetch_from_api(identifier)
            state_used = "active (identifier)"

            # === Try active listing using CJRR cleaned catalogue number if not found ===
            if not id_data and cjrr_num:
                id_data = fetch_from_api(cjrr_num)
                state_used = "active (CJRR)"

            # === Try archived if not found ===
            if not id_data:
                id_data = fetch_from_api(identifier, "archived")
                state_used = "archived (identifier)" if id_data else state_used

            if not id_data and cjrr_num:
                id_data = fetch_from_api(cjrr_num, "archived")
                state_used = "archived (CJRR)" if id_data else "not found"

            # === If still nothing ===
            if not id_data:
                return "Not found", "N/A", "N/A", state_used

            device_id = id_data[0].get("device_id")
            if not device_id:
                return "Not found", "N/A", "N/A", state_used

            # === Fetch full device record ===
            params = {"id": device_id}
            if "archived" in state_used:
                params["state"] = "archived"

            device_response = requests.get(BASE_DEVICE_URL, params=params, timeout=10)
            if device_response.status_code != 200:
                continue

            device_data = device_response.json()
            trade_name = device_data.get("trade_name", "Unknown")
            licence_number = device_data.get("licence_number", "N/A")
            manufacturer_name = device_data.get("company_name", "N/A")

            return trade_name, licence_number, manufacturer_name, state_used

        except Exception as e:
            if attempt == retries:
                return f"Error: {str(e)}", "N/A", "N/A", "error"
            time.sleep(1)  # retry delay

    return "Error: failed after retries", "N/A", "N/A", "error"

def main():
    # input_file = "CJRR_catalogue_list.csv"
    input_file = "output_devices_with_edit.csv"
    output_file = "mdall_results.csv"
    results = []

    # === Detect delimiter and handle UTF-8 BOM ===
    with open(input_file, "r", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel  # fallback to comma
        reader = csv.DictReader(f, dialect=dialect)
        expected_cols = {"device_identifier", "cat_num_cleaned"}
        if not expected_cols.issubset(reader.fieldnames):
            print(f"Detected headers: {reader.fieldnames}")
            raise KeyError(f"CSV must include columns: {expected_cols}")

        # === Loop through identifiers ===
        for row in reader:
            identifier = row['device_identifier'].strip()
            cjrr_num = row['cat_num_cleaned'].strip()
            manufacturer_in = row.get('Manufacturer', '').strip()

            print(f"Searching MDALL for {identifier} (and CJRR {cjrr_num})...")

            trade_name, licence_number, manufacturer_api, state_used = get_device_info_by_identifier(identifier, cjrr_num)
            manufacturer_final = manufacturer_in or manufacturer_api

            results.append({
                "device_identifier": identifier,
                "CJRR_cat_num": cjrr_num,
                "Manufacturer": manufacturer_final,
                "Device Name": trade_name,
                "Licence Number": licence_number,
                "MDALL State": state_used
            })
            time.sleep(0.3)  # polite delay

    # === Write output ===
    with open(output_file, "w", newline='', encoding="utf-8") as csvfile:
        fieldnames = ["device_identifier", "CJRR_cat_num", "Manufacturer", "Device Name", "Licence Number", "MDALL State"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nHecho! Results saved to '{output_file}'")

if __name__ == "__main__":
    main()
