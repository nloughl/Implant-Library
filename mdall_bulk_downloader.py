"""
Canadian MDALL Bulk Knee Implant Downloader
===========================================

Searches Health Canada's Medical Devices Active Licence Listing (MDALL) API
for Canadian-registered knee implant devices by common implant system names.

API endpoints used:
  GET https://health-products.canada.ca/api/medical-devices/device/?device_name=Triathlon
  GET https://health-products.canada.ca/api/medical-devices/deviceidentifier/?device_id=580691

Output CSV columns are aligned with optimized_gudid_downloader.py for easy merging:
  DEVICE_ID, DEVICE_ID_ISSUING_AGENCY, BRAND_NAME, COMPANY_NAME,
  VERSION_MODEL_NUMBER, LICENCE_NUMBER, LICENCE_STATUS, FIRST_LICENCE_DT,
  END_DATE, SOURCE, SEARCH_TERM

Merge key with GUDID: VERSION_MODEL_NUMBER (catalogue number)

Usage:
# Full download (all 19 search terms, ~25 min with identifiers)
python mdall_bulk_downloader.py

# Fast mode — no catalogue number lookup (~2 min)
python mdall_bulk_downloader.py --no-identifiers

# Single term
python mdall_bulk_downloader.py --term Triathlon

# Download + merge with GUDID output
python mdall_bulk_downloader.py --merge-gudid gudid_downloads/knee_devices_bulk_*.csv
"""

import os
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            f'logs/mdall_bulk_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search terms and manufacturer mappings
# ---------------------------------------------------------------------------

KNEE_IMPLANT_SEARCH_TERMS: Dict[str, str] = {
    # Stryker
    "Triathlon": "Stryker",
    "Scorpio": "Stryker",
    "Duracon": "Stryker",
    "Kinemax": "Stryker",
    "Oxinium": "Stryker",
    # DePuy / J&J
    "ATTUNE": "DePuy Synthes",
    "Sigma": "DePuy Synthes",
    "PFC Sigma": "DePuy Synthes",
    "LCS": "DePuy Synthes",
    # Zimmer Biomet
    "Persona": "Zimmer Biomet",
    "NexGen": "Zimmer Biomet",
    "Gender Solutions": "Zimmer Biomet",
    "Vanguard": "Zimmer Biomet",
    "Oxford": "Zimmer Biomet",
    "AGC": "Zimmer Biomet",
    # Smith & Nephew
    "Journey": "Smith & Nephew",
    "Legion": "Smith & Nephew",
    "Genesis": "Smith & Nephew",
    "Anthem": "Smith & Nephew",
    # Exactech
    "OPTETRAK": "Exactech",
    # ConforMIS
    "iTotal": "ConforMIS",
    # Medacta
    "GMK": "Medacta",
}

# Keywords indicating non-implant devices (instruments, trials, etc.)
EXCLUDE_KEYWORDS = [
    "trial",
    "instrument",
    "screw",
    "pin",
    "wire",
    "drill",
    "saw",
    "rasp",
    "reamer",
    "guide",
    "template",
    "impactor",
    "extractor",
    "inserter",
    "positioning",
    "alignment",
    "cutting",
    "removal",
    "planer",
    "retractor",
    "forceps",
    "clamp",
    "holder",
    "handle",
    "driver",
    "wrench",
    "syringe",
    "cement",
    "mixing",
    "dispenser",
    "spatula",
    "packing",
    "tray",
    "case",
    "container",
    "kit",
    "set",
    "system pack",
    "packaging",
    "balancer",
    "spacer",
    "tensor",
    "spreader",
    "surgical technique",
    "op tech",
    "optech",
    "surgical protocol",
    "slope block",
    "cutting block",
    "flexion block",
    "resection block",
    "resection",
    "stylus",
    "var-val block",
    "a/p shift block",
    "adapter",
    "adaptor",
    "connector",
    "coupler",
    "angle wing",
    "alignment handle",
    "tibial stylus",
    "substrate",
    "hearing aid",
    "transmitter",
    "examination",
    "hoses",
    "cut block",
    "valve",
    "antibacterial",
    "gloves",
    "cone",
    "wedge",
    "bolt",
]

BASE_URL = "https://health-products.canada.ca/api/medical-devices"


class MDALLBulkDownloader:
    """
    Bulk downloader for Canadian MDALL knee implant device data.

    Strategy:
      1. One API call per implant name  -> list of devices
      2. One API call per device        -> list of catalogue numbers (identifiers)
      3. Filter out instruments/trials
      4. Emit one row per catalogue number, aligned with GUDID output format
    """

    def __init__(self, output_dir: str = "mdall_downloads", request_delay: float = 0.3):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.request_delay = request_delay
        self._id_cache: Dict[int, List[str]] = {}  # device_id -> [identifiers]

    # ------------------------------------------------------------------
    # Low-level API helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict, timeout: int = 30) -> Optional[list]:
        """GET an MDALL API endpoint, returning parsed JSON list or None on error."""
        url = f"{BASE_URL}/{endpoint}/"
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=timeout)
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, list) else [data]
            except requests.exceptions.Timeout:
                logger.warning(
                    f"Timeout on {endpoint} (attempt {attempt+1}/3): {params}"
                )
                time.sleep(2**attempt)
            except requests.RequestException as e:
                logger.warning(f"Request error on {endpoint}: {e}")
                return None
        return None

    def _fetch_identifiers(self, device_id: int) -> List[str]:
        """Return all catalogue numbers (device_identifiers) for a given device_id."""
        if device_id in self._id_cache:
            return self._id_cache[device_id]

        data = self._get("deviceidentifier", {"device_id": device_id}, timeout=20)
        identifiers = []
        if data:
            identifiers = [
                row["device_identifier"]
                for row in data
                if row.get("device_identifier") and not row.get("end_date")
            ]
            # Fall back to all identifiers if every one has an end_date
            if not identifiers:
                identifiers = [
                    row["device_identifier"]
                    for row in data
                    if row.get("device_identifier")
                ]

        self._id_cache[device_id] = identifiers
        time.sleep(self.request_delay)
        return identifiers

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _is_instrument(trade_name: str) -> bool:
        name_lower = trade_name.lower()
        return any(kw in name_lower for kw in EXCLUDE_KEYWORDS)

    # ------------------------------------------------------------------
    # Main download methods
    # ------------------------------------------------------------------

    def download_by_name(
        self,
        search_term: str,
        manufacturer: str = "",
        fetch_identifiers: bool = True,
    ) -> pd.DataFrame:
        """
        Download all MDALL devices matching search_term.

        Args:
            search_term:        e.g. 'Triathlon'
            manufacturer:       known manufacturer string for that term
            fetch_identifiers:  if True, fetch catalogue numbers per device
                                (slower but required for merging with GUDID)

        Returns:
            DataFrame with one row per catalogue number (or per device if
            fetch_identifiers=False)
        """
        logger.info(f'\nSearching MDALL: "{search_term}" ({manufacturer})')

        devices = self._get("device", {"device_name": search_term})
        if not devices:
            logger.warning(f'  No results for "{search_term}"')
            return pd.DataFrame()

        time.sleep(self.request_delay)
        logger.info(f"  Raw results: {len(devices):,} devices")

        # Filter out instruments
        implants = [
            d for d in devices if not self._is_instrument(d.get("trade_name", ""))
        ]
        logger.info(f"  After filtering instruments: {len(implants):,} devices")

        rows = []
        for i, device in enumerate(implants):
            device_id = device.get("device_id")
            trade_name = device.get("trade_name", "")
            licence_no = device.get("original_licence_no")
            lic_status = "A" if not device.get("end_date") else "C"
            first_dt = device.get("first_licence_dt", "")
            end_dt = device.get("end_date", "")

            base_row = {
                "DEVICE_ID_ISSUING_AGENCY": "MDALL",
                "BRAND_NAME": trade_name,
                "COMPANY_NAME": manufacturer,
                "MRI_SAFETY_STATUS": "",
                "LABELED_CONTAINS_NRL": "",
                "GMDN_TERMS": "",
                "LICENCE_NUMBER": licence_no,
                "LICENCE_STATUS": lic_status,
                "FIRST_LICENCE_DT": first_dt,
                "END_DATE": end_dt,
                "MDALL_DEVICE_ID": device_id,
                "SOURCE": "MDALL",
                "SEARCH_TERM": search_term,
            }

            if fetch_identifiers and device_id:
                identifiers = self._fetch_identifiers(device_id)
                if identifiers:
                    for ident in identifiers:
                        rows.append(
                            {
                                "VERSION_MODEL_NUMBER": ident,
                                **base_row,
                            }
                        )
                else:
                    # No catalogue number found — include device with blank
                    rows.append(
                        {
                            "VERSION_MODEL_NUMBER": "",
                            **base_row,
                        }
                    )
            else:
                rows.append(
                    {
                        "VERSION_MODEL_NUMBER": "",
                        **base_row,
                    }
                )

            if fetch_identifiers and (i + 1) % 25 == 0:
                logger.info(
                    f"  [{i+1}/{len(implants)}] processed, {len(rows):,} rows so far"
                )

        df = pd.DataFrame(rows)
        logger.info(f'  [OK] {len(df):,} rows for "{search_term}"')
        return df

    def download_all(self, fetch_identifiers: bool = True) -> pd.DataFrame:
        """
        Download all knee implant systems defined in KNEE_IMPLANT_SEARCH_TERMS.

        Returns:
            Combined DataFrame with all results
        """
        logger.info("\n" + "=" * 80)
        logger.info("MDALL BULK KNEE IMPLANT DOWNLOAD")
        logger.info("=" * 80)
        logger.info(f"Search terms: {len(KNEE_IMPLANT_SEARCH_TERMS)}")
        logger.info(f"Fetch identifiers: {fetch_identifiers}")
        logger.info("=" * 80)

        all_frames = []

        for term, manufacturer in KNEE_IMPLANT_SEARCH_TERMS.items():
            df = self.download_by_name(term, manufacturer, fetch_identifiers)
            if not df.empty:
                all_frames.append(df)
            time.sleep(self.request_delay)

        if not all_frames:
            logger.error("No data downloaded.")
            return pd.DataFrame()

        combined = pd.concat(all_frames, ignore_index=True)

        # Deduplicate by (VERSION_MODEL_NUMBER, BRAND_NAME)
        before = len(combined)
        if "VERSION_MODEL_NUMBER" in combined.columns:
            combined = combined.drop_duplicates(
                subset=["VERSION_MODEL_NUMBER", "BRAND_NAME"]
            )
        logger.info(f"\nDeduplication: {before:,} -> {len(combined):,} rows")

        # Save
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.output_dir / f"mdall_knee_devices_{timestamp}.csv"
        combined.to_csv(out_path, index=False, encoding="utf-8")
        logger.info(f"[OK] Saved {len(combined):,} rows -> {out_path}")

        # Summary
        logger.info("\nDevices per search term:")
        if "SEARCH_TERM" in combined.columns:
            for term, count in (
                combined.groupby("SEARCH_TERM")
                .size()
                .sort_values(ascending=False)
                .items()
            ):
                logger.info(f"  {term:25s}: {count:5,}")

        return combined

    def apply_gmdn_filtering(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove residual non-implant devices not caught by name filtering.
        Mirrors the same method in optimized_gudid_downloader.py.
        """
        if "BRAND_NAME" not in df.columns:
            return df

        mask = df["BRAND_NAME"].str.lower().apply(self._is_instrument)
        filtered = df[~mask].copy()
        logger.info(f"GMDN-style filter: {len(df):,} -> {len(filtered):,} rows")
        return filtered


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------


def merge_gudid_mdall(gudid_csv: str, mdall_csv: str, output_csv: str) -> pd.DataFrame:
    """
    Merge GUDID and MDALL CSVs on catalogue number (VERSION_MODEL_NUMBER).

    Performs an outer merge so devices present in only one source are retained.

    Args:
        gudid_csv:  path to CSV from optimized_gudid_downloader.py
        mdall_csv:  path to CSV from this script
        output_csv: output path for merged CSV

    Returns:
        Merged DataFrame
    """
    gudid = pd.read_csv(gudid_csv, dtype=str).fillna("")
    mdall = pd.read_csv(mdall_csv, dtype=str).fillna("")

    # Normalise catalogue numbers: strip spaces and uppercase
    for df in (gudid, mdall):
        df["VERSION_MODEL_NUMBER"] = df["VERSION_MODEL_NUMBER"].str.strip().str.upper()

    merged = pd.merge(
        gudid,
        mdall,
        on="VERSION_MODEL_NUMBER",
        how="outer",
        suffixes=("_GUDID", "_MDALL"),
    )

    # Consolidate BRAND_NAME and COMPANY_NAME columns
    for col in ("BRAND_NAME", "COMPANY_NAME"):
        gudid_col = f"{col}_GUDID"
        mdall_col = f"{col}_MDALL"
        if gudid_col in merged.columns and mdall_col in merged.columns:
            merged[col] = merged[gudid_col].where(
                merged[gudid_col] != "", merged[mdall_col]
            )
            merged.drop(columns=[gudid_col, mdall_col], inplace=True)

    merged.to_csv(output_csv, index=False, encoding="utf-8")
    logger.info(
        f"Merged: {len(gudid):,} GUDID + {len(mdall):,} MDALL -> {len(merged):,} rows -> {output_csv}"
    )
    return merged


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="MDALL Bulk Knee Implant Downloader")
    parser.add_argument(
        "--no-identifiers",
        action="store_true",
        help="Skip per-device catalogue number lookups (faster, but no VERSION_MODEL_NUMBER)",
    )
    parser.add_argument(
        "--term",
        type=str,
        default=None,
        help='Download a single search term instead of all (e.g. "Triathlon")',
    )
    parser.add_argument(
        "--merge-gudid",
        type=str,
        default=None,
        help="Path to GUDID CSV to merge with MDALL output",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="mdall_downloads",
    )
    args = parser.parse_args()

    downloader = MDALLBulkDownloader(output_dir=args.output_dir)
    fetch_ids = not args.no_identifiers

    if args.term:
        manufacturer = KNEE_IMPLANT_SEARCH_TERMS.get(args.term, "")
        df = downloader.download_by_name(args.term, manufacturer, fetch_ids)
        if not df.empty:
            out = (
                Path(args.output_dir)
                / f'mdall_{args.term}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            )
            df.to_csv(out, index=False, encoding="utf-8")
            logger.info(f"Saved {len(df):,} rows -> {out}")
    else:
        df = downloader.download_all(fetch_identifiers=fetch_ids)

    if args.merge_gudid and not df.empty:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(args.output_dir) / f"merged_gudid_mdall_{timestamp}.csv"
        merge_gudid_mdall(
            args.merge_gudid,
            str(
                max(
                    Path(args.output_dir).glob("mdall_knee_devices_*.csv"),
                    key=lambda p: p.stat().st_mtime,
                )
            ),
            str(out_path),
        )


if __name__ == "__main__":
    main()
