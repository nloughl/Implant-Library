"""
Optimized FDA GUDID Product Code Downloader
Uses the GUDID custom query download to get all devices for specific product codes.

Download endpoint (GET, returns ZIP containing pipe-delimited .txt):
  https://accessgudid.nlm.nih.gov/download/query.zip
  Parameters:
    option=device.productCodes.fdaProductCode.productCode
    value=JWH

One request per product code — no pagination needed.
"""

import io
import zipfile
import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'gudid_bulk_download_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class ProductCodeBulkDownloader:
    """
    Download devices from FDA GUDID using product code bulk download feature
    
    Much faster than individual device lookups:
    - Single request per product code
    - Gets ALL devices for that code
    - No pagination needed
    """
    
    DOWNLOAD_URL = "https://accessgudid.nlm.nih.gov/download/query.zip"
    QUERY_PAGE_URL = "https://accessgudid.nlm.nih.gov/download/query"
    
    # Knee implant product codes
    KNEE_PRODUCT_CODES = {
        'HRY': 'Femorotibial, Semi-Constrained, Cemented',
        'KRR': 'Patello/Femoral, Semi-Constrained, Cemented',
        'HSX': 'Femorotibial, Non-Constrained, Cemented',
        'JWH': 'Patellofemorotibial, Semi-Constrained, Cemented',
        'MBH': 'Patello/Femorotibial, Semi-Constrained, Uncemented',
        'NPJ': 'Patellofemorotibial, Partial, Semi-Constrained',
        'NJL': 'Patellofemorotibial, Semi-Constrained, Metal/Polymer',
        'KWN': 'Femorotibial, Non-Constrained, Uncemented',
        'KRO': 'Femorotibial, Constrained, Cemented',
        'KRP': 'Patello/Femorotibial, Constrained, Cemented',
        'KRQ': 'Patello/Femoral, Constrained, Cemented',
    }
    
    def __init__(self, output_dir: str = "gudid_downloads"):
        """Initialize downloader"""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        
    def download_product_code(self, product_code: str) -> pd.DataFrame:
        """
        Download all devices for a specific product code.

        Issues one GET request to the GUDID query download endpoint which returns
        a ZIP containing a pipe-delimited text file with all matching devices.

        Args:
            product_code: FDA product code (e.g., 'JWH')

        Returns:
            DataFrame with all devices for that code
        """
        logger.info(f"\nDownloading product code: {product_code}")
        logger.info(f"  Description: {self.KNEE_PRODUCT_CODES.get(product_code, 'Unknown')}")

        params = {
            'option': 'device.productCodes.fdaProductCode.productCode',
            'value': product_code,
        }

        try:
            response = self.session.get(
                self.DOWNLOAD_URL,
                params=params,
                headers={'Referer': self.QUERY_PAGE_URL},
                timeout=120,
            )
            response.raise_for_status()

            # Response is a ZIP containing a pipe-delimited .txt file
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                txt_name = zf.namelist()[0]
                with zf.open(txt_name) as f:
                    content = f.read().decode('utf-8-sig')  # strip BOM if present

        except requests.RequestException as e:
            logger.error(f"Failed to download {product_code}: {e}")
            return pd.DataFrame()
        except (zipfile.BadZipFile, KeyError) as e:
            logger.error(f"Failed to parse ZIP for {product_code}: {e}")
            return pd.DataFrame()

        lines = content.strip().splitlines()
        if len(lines) < 2:
            logger.warning(f"No data returned for {product_code}")
            return pd.DataFrame()

        header = [h.strip('"').strip() for h in lines[0].split('|')]
        records = []
        for line in lines[1:]:
            if not line.strip():
                continue
            values = [v.strip().strip('"') for v in line.split('|')]
            if len(values) == len(header):
                records.append(dict(zip(header, values)))

        df = pd.DataFrame(records)
        logger.info(f"[OK] Downloaded {len(df):,} devices for {product_code}")

        # Save raw pipe-delimited file
        output_file = self.output_dir / f"product_code_{product_code}.txt"
        output_file.write_text(content, encoding='utf-8')
        logger.info(f"  Saved to: {output_file}")

        return df
    
    def download_all_knee_codes(self) -> pd.DataFrame:
        """
        Download all knee implant product codes and combine
        
        Returns:
            Combined DataFrame with all knee devices
        """
        logger.info("\n" + "=" * 80)
        logger.info("FDA GUDID BULK DOWNLOAD - KNEE IMPLANT PRODUCT CODES")
        logger.info("=" * 80)
        logger.info(f"\nDownloading {len(self.KNEE_PRODUCT_CODES)} product codes...")
        
        all_devices = []
        stats = {}
        
        for product_code in self.KNEE_PRODUCT_CODES.keys():
            df = self.download_product_code(product_code)
            
            if len(df) > 0:
                # Add product code to each record
                df['product_code'] = product_code
                df['product_code_description'] = self.KNEE_PRODUCT_CODES[product_code]
                all_devices.append(df)
                stats[product_code] = len(df)
            
            # Be respectful - small delay between requests
            time.sleep(1)
        
        # Combine all
        if all_devices:
            combined_df = pd.concat(all_devices, ignore_index=True)
        else:
            logger.error("No devices downloaded!")
            return pd.DataFrame()
        
        # Save combined file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = self.output_dir / f"knee_devices_bulk_{timestamp}.csv"
        combined_df.to_csv(output_file, index=False)
        
        logger.info("\n" + "=" * 80)
        logger.info("DOWNLOAD COMPLETE")
        logger.info("=" * 80)
        logger.info(f"\n[OK] Total devices: {len(combined_df):,}")
        logger.info(f"[OK] Output file: {output_file}")
        
        # Statistics
        logger.info("\nDevices per product code:")
        for code, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            desc = self.KNEE_PRODUCT_CODES[code]
            logger.info(f"  {code}: {count:6,} - {desc}")
        
        return combined_df
    
    def apply_gmdn_filtering(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply GMDN filtering to remove trials, instruments, etc.
        
        Args:
            df: DataFrame with GMDN_TERMS column
        
        Returns:
            Filtered DataFrame
        """
        logger.info("\n" + "=" * 80)
        logger.info("GMDN FILTERING")
        logger.info("=" * 80)
        
        initial_count = len(df)
        
        # Exclusion keywords (trials, instruments, etc.)
        EXCLUDE_KEYWORDS = [
            'trial', 'instrument', 'screw', 'pin', 'wire', 'drill', 
            'saw', 'rasp', 'reamer', 'guide', 'template', 'impactor', 
            'extractor', 'inserter', 'positioning', 'alignment',
            'cutting', 'removal', 'extraction'
        ]
        
        # Create exclusion mask
        exclude_mask = pd.Series([False] * len(df))
        
        for keyword in EXCLUDE_KEYWORDS:
            keyword_mask = df['GMDN_TERMS'].str.contains(
                keyword, 
                case=False, 
                na=False
            )
            exclude_mask |= keyword_mask
            
            excluded_count = keyword_mask.sum()
            if excluded_count > 0:
                logger.info(f"  Excluding '{keyword}': {excluded_count:,} devices")
        
        # Filter out excluded devices
        filtered_df = df[~exclude_mask].copy()
        
        excluded_total = initial_count - len(filtered_df)
        logger.info(f"\n[OK] Filtered out {excluded_total:,} non-implant devices")
        logger.info(f"[OK] Remaining: {len(filtered_df):,} actual implants")
        
        return filtered_df


def compare_methods():
    """
    Compare old vs new download methods
    """
    print("\n" + "=" * 80)
    print("DOWNLOAD METHOD COMPARISON")
    print("=" * 80)

    print("\nOLD METHOD (Individual API Calls):")
    print("  - Download implant list: 1,086,157 devices")
    print("  - GMDN filter: -> 75,967 knee devices")
    print("  - Product code filter: 75,967 API calls")
    print("  - Rate limit: 2 requests/sec")
    print("  TIME: ~10.5 hours")
    print("  ISSUE: Must process ALL 75K devices to get 25K matches")

    print("\nNEW METHOD (GUDID query.zip bulk download):")
    print("  - URL: /download/query.zip?option=...productCode&value=JWH")
    print("  - 11 requests total, one per product code, returns full ZIP")
    print("  - No pagination, no rate limit concerns")
    print("  TIME: ~1-2 minutes")
    print("  BENEFIT: One request per code, pipe-delimited CSV in ZIP")

    print("\nIMPROVEMENT:")
    print("  - Data volume: only relevant devices")
    print("  - Requests: 75,967 -> 11 (one per product code)")
    print("  - Simpler pipeline: no pre-filtering step needed")


def main():
    """Main execution"""
    
    print("\n" + "=" * 80)
    print("OPTIMIZED FDA GUDID BULK DOWNLOADER")
    print("=" * 80)
    print("\nThis method uses FDA's product code bulk download feature.")
    print("Much faster than individual device API calls!")
    print("=" * 80)
    
    # Show comparison
    compare_methods()
    
    # Run bulk download
    downloader = ProductCodeBulkDownloader(output_dir="gudid_downloads")
    
    logger.info("\n\nStarting bulk download...")
    df = downloader.download_all_knee_codes()
    
    if len(df) == 0:
        logger.error("Download failed!")
        return
    
    # Apply GMDN filtering to remove trials/instruments
    filtered_df = downloader.apply_gmdn_filtering(df)
    
    # Save filtered version
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = Path("gudid_downloads") / f"knee_implants_filtered_{timestamp}.csv"
    filtered_df.to_csv(output_file, index=False)
    
    logger.info("\n" + "=" * 80)
    logger.info("FINAL RESULTS")
    logger.info("=" * 80)
    logger.info(f"[OK] Total devices downloaded: {len(df):,}")
    logger.info(f"[OK] After GMDN filtering: {len(filtered_df):,}")
    logger.info(f"[OK] Output file: {output_file}")
    
    # Column info
    logger.info("\nAvailable columns:")
    for col in filtered_df.columns:
        logger.info(f"  - {col}")
    
    # Next steps
    print("\n" + "=" * 80)
    print("NEXT STEPS")
    print("=" * 80)
    print("\n1. [OK] Devices downloaded (1 minute)")
    print("2. → Enrich with MDALL materials (7 hours)")
    print("3. → Auto-download eIFUs (2-4 hours)")
    print("4. → Final enriched database (95-98% coverage)")
    
    return filtered_df


if __name__ == "__main__":
    df = main()