"""
FDA Product Codes Updater
Downloads and processes the latest FDA product code classifications
with a focus on knee joint implants.
"""

import requests
import zipfile
import io
import pandas as pd
import re
import os
from datetime import datetime
from pathlib import Path

# —————— Configuration ——————
DOWNLOAD_URL = "https://www.accessdata.fda.gov/premarket/ftparea/foiclass.zip"
OUTPUT_DIR = "product_codes"
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_ZIP_PATH = f"{OUTPUT_DIR}/foiclass_{TIMESTAMP}.zip"
PRODUCT_CODES_CSV = f"{OUTPUT_DIR}/fda_product_codes_{TIMESTAMP}.csv"

# —————— Functions ——————

def download_fda_data(url, output_path, timeout=30):
    """
    Download FDA product code file with progress indication.
    
    Args:
        url: Download URL
        output_path: Where to save the file
        timeout: Request timeout in seconds
    
    Returns:
        bool: Success status
    """
    print(" Downloading latest FDA product code file...")
    print(f"   URL: {url}")
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, timeout=timeout, stream=True, headers=headers)
        resp.raise_for_status()
        
        total_size = int(resp.headers.get('content-length', 0))
        block_size = 8192
        downloaded = 0
        
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=block_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        percent = (downloaded / total_size) * 100
                        print(f"\r   Progress: {percent:.1f}% ({downloaded // 1024} KB / {total_size // 1024} KB)", end="")
        
        print(f"\n✅ Downloaded successfully: {output_path}")
        print(f"   File size: {downloaded // 1024} KB")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f" Download failed: {e}")
        return False


def extract_zip(zip_path, extract_dir):
    """
    Extract the product code file from ZIP.
    
    Args:
        zip_path: Path to ZIP file
        extract_dir: Directory to extract to
    
    Returns:
        str: Path to extracted file, or None if failed
    """
    print(f"\n📦 Extracting {zip_path}...")
    
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            files = z.namelist()
            print(f"   Files in archive: {files}")
            
            # Extract the first file (typically foiclass.txt or foiclass.lis)
            if not files:
                print(" No files found in archive")
                return None
            
            inner_file = files[0]
            z.extract(inner_file, extract_dir)
            extracted_path = os.path.join(extract_dir, inner_file)
            
            print(f"✅ Extracted: {extracted_path}")
            return extracted_path
            
    except zipfile.BadZipFile as e:
        print(f"Invalid ZIP file: {e}")
        return None
    except Exception as e:
        print(f"Extraction failed: {e}")
        return None


def parse_product_codes(file_path, delimiter="|"):
    """
    Parse FDA product code file into DataFrame.
    
    Args:
        file_path: Path to the extracted text file
        delimiter: Field delimiter (default: pipe)
    
    Returns:
        pd.DataFrame: Parsed product codes
    """
    print(f"\n Parsing product codes from {file_path}...")
    
    try:
        # Try reading with different encodings
        for encoding in ['utf-8', 'latin-1', 'cp1252']:
            try:
                df = pd.read_csv(file_path, sep=delimiter, dtype=str, encoding=encoding)
                print(f"✅ Successfully parsed with {encoding} encoding")
                break
            except UnicodeDecodeError:
                continue
        else:
            print(" Failed to parse with any encoding")
            return None
        
        # Clean column names
        df.columns = [c.strip() for c in df.columns]
        
        # Display available columns
        print(f"   Total records: {len(df):,}")
        print(f"   Columns: {', '.join(df.columns.tolist())}")
        
        return df
        
    except Exception as e:
        print(f"Parsing failed: {e}")
        return None


# Define Filtering Scheme, filter by device name prefix 
def filter_knee_by_device_name(df, device_prefix="Prosthesis, Knee"):
    """
    Filter FDA product codes where Device Name starts with 'Prosthesis, Knee'
    
    Args:
        df: Full product codes DataFrame
        device_prefix: Device name prefix to match
    
    Returns:
        pd.DataFrame: Filtered DataFrame
    """
    print(f"\n Filtering devices where name starts with: '{device_prefix}'")

    # Identify device name column
    device_cols = [c for c in df.columns if "device" in c.lower() and "name" in c.lower()]
    if not device_cols:
        raise ValueError("Could not find Device Name column")

    device_col = device_cols[0]
    print(f"   Using device name column: {device_col}")

    # Identify product code column
    code_cols = [c for c in df.columns if "product" in c.lower() and "code" in c.lower()]
    if not code_cols:
        raise ValueError(" Could not find Product Code column")

    code_col = code_cols[0]
    print(f"   Using product code column: {code_col}")

    # Filter rows
    knee_df = df[
        df[device_col]
        .str.strip()
        .str.lower()
        .str.startswith(device_prefix.lower(), na=False)
    ].copy()

    print(f" Found {len(knee_df)} knee prosthesis records")

    # Show unique product codes
    codes = sorted(knee_df[code_col].unique())
    print(f" Product codes extracted ({len(codes)}): {', '.join(codes)}")

    return knee_df


def save_results(df, output_path):
    """
    Save filtered results to CSV.
    
    Args:
        df: DataFrame to save
        output_path: Output file path
    """
    print(f"\n💾 Saving results to {output_path}...")
    
    try:
        df.to_csv(output_path, index=False)
        file_size = os.path.getsize(output_path) // 1024
        print(f" Saved successfully ({file_size} KB)")
        print(f"   Records: {len(df)}")
        print(f"   Columns: {len(df.columns)}")
        return True
    except Exception as e:
        print(f"Save failed: {e}")
        return False


# —————— Main Execution ——————

def main():
    """Main execution function."""
    print("=" * 60)
    print("FDA Product Codes Updater - Knee Joint Implants")
    print("=" * 60)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Create output directory
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    
    # Step 1: Download
    if not download_fda_data(DOWNLOAD_URL, OUTPUT_ZIP_PATH):
        print("\n Process terminated: Download failed")
        return False
    
    # Step 2: Extract
    extracted_file = extract_zip(OUTPUT_ZIP_PATH, OUTPUT_DIR)
    if not extracted_file:
        print("\n Process terminated: Extraction failed")
        return False
    
    # Step 3: Parse
    df = parse_product_codes(extracted_file)
    if df is None or len(df) == 0:
        print("\n Process terminated: Parsing failed")
        return False
    
    # Step 4: Filter for knee codes
    knee_df = filter_knee_by_device_name(df, device_prefix="Prosthesis, Knee")
    
    # Step 5: Save all product codes
    print("\n" + "=" * 60)
    print("Saving Complete Dataset")
    print("=" * 60)
    all_codes_path = f"{OUTPUT_DIR}/fda_all_product_codes_{TIMESTAMP}.csv"
    save_results(df, all_codes_path)
    
    # Step 6: Save filtered knee codes
    if len(knee_df) > 0:
        print("\n" + "=" * 60)
        print("Saving Filtered Knee Dataset")
        print("=" * 60)
        knee_codes_path = f"{OUTPUT_DIR}/fda_knee_product_codes_{TIMESTAMP}.csv"
        save_results(knee_df, knee_codes_path)
        
        # Display sample
        print("\n Sample of filtered records:")
        display_cols = [col for col in knee_df.columns if any(x in col.lower() for x in ['code', 'name', 'class', 'device'])][:5]
        if display_cols:
            print(knee_df[display_cols].head(10).to_string(index=False))
    
    print("\n" + "=" * 60)
    print("Process completed successfully!")
    print("=" * 60)
    print(f"\nOutput files:")
    print(f"  • All codes: {all_codes_path}")
    if len(knee_df) > 0:
        print(f"  • Knee codes: {knee_codes_path}")
    
    return True


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)