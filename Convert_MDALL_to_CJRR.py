import csv
import re

# Usage: Just edit the input and output file names/pathes in the main function (at bottom) then run :)

MANUFACTURERS_MAP = {
    "Stryker/Osteonics/Howmedica": ["Stryker", "Howmedica", "Mako/Stryker"],
    "Zimmer/Biomet/Sulzer/Centerpulse": ["Zimmer", "Biomet"],
    "DePuy/Finsbury/J & J": ["Depuy"],
    "MicroPort/Wright Medical": ["Microport"],
}


def standardize_manufacturer(manufacturer: str) -> str:
    for standard, raws in MANUFACTURERS_MAP.items():
        if manufacturer in raws:
            return standard
    return manufacturer


def edit_catalogue_number(cat_num: str, manufacturer: str) -> list[str]:
    """
    Returns a list of CJRR-cleaned catalogue numbers.
    Multiple values are joined with ';' in the output CSV.
    """
    cat_num = re.sub(r"[^\w]", "", cat_num.strip())
    manufacturer = manufacturer.strip()

    versions = set()

    # --- Zimmer / Biomet rules ---
    if manufacturer == "Zimmer/Biomet/Sulzer/Centerpulse":
        versions.add(cat_num)

        if cat_num.startswith(("00", "90")):
            base = cat_num[2:]
            versions.add(base)

            # Remove inner 0 if present
            if len(base) > 6:
                versions.add(base[:4] + base[5:])

    # --- Default ---
    else:
        versions.add(cat_num)

    return sorted(v for v in versions if v)


def main():
    input_file = "implant_library_filled.csv"
    output_file = "implant_library_final.csv"

    with open(input_file, "r", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(f, dialect=dialect)

        fieldnames = reader.fieldnames + ["CJRR_Manufacturer", "CJRR_catnum"]
        rows = []

        for row in reader:
            raw_cat = row.get("catalogue_num", "").strip()
            raw_manu = row.get("manufacturer", "").strip()

            cjrr_manu = standardize_manufacturer(raw_manu)
            cjrr_versions = edit_catalogue_number(raw_cat, cjrr_manu)

            row["CJRR_Manufacturer"] = cjrr_manu
            row["CJRR_catnum"] = ";".join(cjrr_versions)

            rows.append(row)

    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done! Updated CSV saved as '{output_file}'")
    print(f"Added columns 'CJRR_Manufacturer' and 'CJRR_catnum' to {len(rows)} rows.")


if __name__ == "__main__":
    main()
