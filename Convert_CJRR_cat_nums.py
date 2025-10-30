import csv
import re


def edit_catalogue_number(cat_num: str, manufacturer: str) -> str:
    """
    Edit CJRR cleaned catalogue number to match formats accepted by MDALL according to Manufacturer 
    Manufacturers in CJRR: 
        DePuy/Finsbury/J & J
        Zimmer/Biomet?Sulzer/Centerpulse
        Stryker/Osteonics/Howmedica
        Smith & Nephew -- as is, no editing needed 
        MicroPort/Wright Medical -- no editing necessary 
    """
    cat_num = cat_num.strip()
    manufacturer = manufacturer.strip()

    # --- Manufacturer-specific rules ---
    if manufacturer == "Stryker/Osteonics/Howmedica":
        # letter between digits → surround with dashes
        if re.search(r"\d[A-Z]\d", cat_num):
            cat_num = re.sub(r"(\d)([A-Z])(\d)", r"\1-\2-\3", cat_num)
        
        # letter at end → dash after 2nd digit
        elif re.search(r"\d{2}[A-Z0-9]*[A-Z]$", cat_num):
            cat_num = cat_num[:2] + "-" + cat_num[2:]
        
        # length 7 → dash around 3rd digit
        elif len(cat_num) == 7:
            cat_num = cat_num[:2] + "-" + cat_num[2:3] + "-" + cat_num[3:] 
        else: 
            cat_num = cat_num 

    elif manufacturer == "Zimmer/Biomet/Sulzer/Centerpulse":
        if len(cat_num) == 11:
            cat_num = cat_num[:2] + "-" + cat_num[2:6] + "-" + cat_num[6:9] + "-" + cat_num[9:]
        elif len(cat_num) == 9 and cat_num[4] == "0":
            cat_num = "00-" + cat_num[:4] + "-" + cat_num[4:7] + "-" + cat_num[7:] 
        elif len(cat_num) == 9:
            cat_num = cat_num[:4] + "-" + cat_num[4:6] + "-" + cat_num[6:] 
        else: 
            cat_num = cat_num  

    elif manufacturer == "DePuy/Finsbury/J & J":
        if cat_num[:3] == "178":
            cat_num = cat_num
        elif len(cat_num) == 9:
            cat_num = cat_num[:4] + "-" + cat_num[4:6] + "-" + cat_num[6:] 
        else:
            cat_num= cat_num

    else:
        # Default: as is 
        cat_num = cat_num

    return cat_num


def main():
    input_file = "catalogue_list.csv"
    output_file = "output_devices_with_edit.csv"

    with open(input_file, "r", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(f, dialect=dialect)

        required_cols = {"cat_num_cleaned", "Manufacturer"}
        if not required_cols.issubset(reader.fieldnames):
            raise KeyError(f"CSV must include {required_cols}. Found: {reader.fieldnames}")

        results = []
        for row in reader:
            cat_num = row["cat_num_cleaned"].strip()
            manufacturer = row["Manufacturer"].strip()

            edited = edit_catalogue_number(cat_num, manufacturer)
            results.append({
                "cat_num_cleaned": cat_num,
                "Manufacturer": manufacturer,
                "device_identifier": edited
            })

    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["cat_num_cleaned", "Manufacturer", "device_identifier"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Done! New file saved as '{output_file}'")
    print(f"Added 'device_identifier' column for {len(results)} rows.")


if __name__ == "__main__":
    main()

