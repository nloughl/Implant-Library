"""
GUDID + MDALL Merger
====================

Merges FDA GUDID (optimized_gudid_downloader.py) and Health Canada MDALL
(mdall_bulk_downloader.py) outputs on catalogue number (VERSION_MODEL_NUMBER).

Match strategy (applied in order, best match wins):
  1. Exact match       – VERSION_MODEL_NUMBER identical after uppercasing + strip
  2. Normalized match  – same after also removing hyphens and spaces

Each output row is tagged with:
  SOURCE      : 'GUDID_ONLY' | 'MDALL_ONLY' | 'BOTH'
  MATCH_TYPE  : 'exact' | 'normalized' | 'unmatched'

Usage:
  python merge_gudid_mdall.py \\
      --gudid  gudid_downloads/knee_implants_filtered_*.csv \\
      --mdall  mdall_downloads/mdall_knee_devices_*.csv \\
      --output merged_gudid_mdall.csv
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column configuration
# ---------------------------------------------------------------------------

# Columns present in both sources that should be consolidated.
# GUDID value is preferred when non-empty; MDALL value used as fallback.
SHARED_COLS = ['BRAND_NAME', 'COMPANY_NAME', 'DEVICE_ID_ISSUING_AGENCY',
               'MRI_SAFETY_STATUS', 'LABELED_CONTAINS_NRL', 'GMDN_TERMS']

# Columns unique to each source (kept as-is with source prefix)
GUDID_ONLY_COLS = ['DEVICE_ID', 'product_code', 'product_code_description']
MDALL_ONLY_COLS = ['MDALL_DEVICE_ID', 'LICENCE_NUMBER', 'LICENCE_STATUS',
                   'FIRST_LICENCE_DT', 'END_DATE', 'SEARCH_TERM']


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_catnum(value: str) -> str:
    """
    Return a normalized catalogue number for fuzzy matching.
    Strips hyphens, spaces, converts to uppercase.
    """
    return value.replace('-', '').replace(' ', '').upper()


def load_latest(pattern: str) -> pd.DataFrame:
    """Load the most recently modified CSV matching a glob pattern."""
    matches = sorted(Path('.').glob(pattern), key=lambda p: p.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f'No file matching: {pattern}')
    path = matches[-1]
    logger.info(f'Loading {path}  ({path.stat().st_size / 1_048_576:.1f} MB)')
    return pd.read_csv(path, dtype=str).fillna('')


# ---------------------------------------------------------------------------
# Core merge
# ---------------------------------------------------------------------------

def merge(gudid: pd.DataFrame, mdall: pd.DataFrame) -> pd.DataFrame:
    """
    Merge GUDID and MDALL DataFrames.

    Returns a combined DataFrame with SOURCE and MATCH_TYPE columns.
    """
    # ---- normalise catalogue number columns --------------------------------
    gudid = gudid.copy()
    mdall = mdall.copy()

    gudid['_cat_exact'] = gudid['VERSION_MODEL_NUMBER'].str.upper().str.strip()
    mdall['_cat_exact'] = mdall['VERSION_MODEL_NUMBER'].str.upper().str.strip()

    gudid['_cat_norm'] = gudid['_cat_exact'].apply(normalize_catnum)
    mdall['_cat_norm'] = mdall['_cat_exact'].apply(normalize_catnum)

    # ---- pass 1: exact match -----------------------------------------------
    exact = pd.merge(
        gudid, mdall,
        on='_cat_exact',
        how='inner',
        suffixes=('_GUDID', '_MDALL'),
    )
    exact['MATCH_TYPE'] = 'exact'
    exact['VERSION_MODEL_NUMBER'] = exact['_cat_exact']
    logger.info(f'Pass 1 – exact matches:      {len(exact):>7,}')

    matched_gudid_exact = set(exact['_cat_exact'])
    matched_mdall_exact = set(exact['_cat_exact'])

    # ---- pass 2: normalized match on remaining rows ------------------------
    gudid_rem = gudid[~gudid['_cat_exact'].isin(matched_gudid_exact)].copy()
    mdall_rem = mdall[~mdall['_cat_exact'].isin(matched_mdall_exact)].copy()

    norm = pd.merge(
        gudid_rem, mdall_rem,
        on='_cat_norm',
        how='inner',
        suffixes=('_GUDID', '_MDALL'),
    )
    norm['MATCH_TYPE'] = 'normalized'
    # Keep the GUDID original as the canonical value
    norm['VERSION_MODEL_NUMBER'] = norm.get('VERSION_MODEL_NUMBER_GUDID',
                                            norm['_cat_norm'])
    logger.info(f'Pass 2 – normalized matches: {len(norm):>7,}')

    matched_gudid_norm  = set(norm['_cat_norm'])
    matched_mdall_norm  = set(norm['_cat_norm'])

    # ---- pass 3: unmatched rows from each source ---------------------------
    gudid_only = gudid[
        ~gudid['_cat_exact'].isin(matched_gudid_exact) &
        ~gudid['_cat_norm'].isin(matched_gudid_norm)
    ].copy()
    mdall_only = mdall[
        ~mdall['_cat_exact'].isin(matched_mdall_exact) &
        ~mdall['_cat_norm'].isin(matched_mdall_norm)
    ].copy()

    gudid_only['MATCH_TYPE'] = 'unmatched'
    mdall_only['MATCH_TYPE'] = 'unmatched'
    logger.info(f'GUDID-only rows:             {len(gudid_only):>7,}')
    logger.info(f'MDALL-only rows:             {len(mdall_only):>7,}')

    # ---- consolidate columns across all three groups -----------------------
    all_frames = []
    for frame, source in [(exact, 'BOTH'), (norm, 'BOTH'),
                          (gudid_only, 'GUDID_ONLY'), (mdall_only, 'MDALL_ONLY')]:
        frame = frame.copy()
        frame['SOURCE'] = source

        # Consolidate shared columns (prefer GUDID, fall back to MDALL)
        for col in SHARED_COLS:
            g_col = f'{col}_GUDID'
            m_col = f'{col}_MDALL'
            if g_col in frame.columns and m_col in frame.columns:
                # For BRAND_NAME, stash the raw MDALL value before coalescing
                if col == 'BRAND_NAME':
                    frame['MDALL_BRAND_NAME'] = frame[m_col]
                frame[col] = frame[g_col].where(frame[g_col] != '', frame[m_col])
                frame.drop(columns=[g_col, m_col], inplace=True)
            elif g_col in frame.columns:
                frame.rename(columns={g_col: col}, inplace=True)
            elif m_col in frame.columns:
                # MDALL-only row: MDALL brand name == the brand name itself
                if col == 'BRAND_NAME':
                    frame['MDALL_BRAND_NAME'] = frame[m_col]
                frame.rename(columns={m_col: col}, inplace=True)
            elif col not in frame.columns:
                frame[col] = ''
        # Ensure MDALL_BRAND_NAME always exists (GUDID-only rows have no MDALL name)
        if 'MDALL_BRAND_NAME' not in frame.columns:
            frame['MDALL_BRAND_NAME'] = ''

        # Ensure GUDID-only cols exist
        for col in GUDID_ONLY_COLS:
            g_col = f'{col}_GUDID'
            if g_col in frame.columns:
                frame.rename(columns={g_col: col}, inplace=True)
            if col not in frame.columns:
                frame[col] = ''

        # Ensure MDALL-only cols exist
        for col in MDALL_ONLY_COLS:
            m_col = f'{col}_MDALL'
            if m_col in frame.columns:
                frame.rename(columns={m_col: col}, inplace=True)
            if col not in frame.columns:
                frame[col] = ''

        # VERSION_MODEL_NUMBER — canonical value
        if 'VERSION_MODEL_NUMBER' not in frame.columns:
            for candidate in ('VERSION_MODEL_NUMBER_GUDID',
                              'VERSION_MODEL_NUMBER_MDALL',
                              '_cat_exact', '_cat_norm'):
                if candidate in frame.columns:
                    frame['VERSION_MODEL_NUMBER'] = frame[candidate]
                    break

        all_frames.append(frame)

    combined = pd.concat(all_frames, ignore_index=True)

    # MDALL stores the catalogue number in DEVICE_ID (not a real GUDID DI).
    # Clear it for MDALL-only rows so the description enricher doesn't try to
    # look up catalogue numbers as GUDID device identifiers.
    if 'DEVICE_ID' in combined.columns:
        combined.loc[combined['SOURCE'] == 'MDALL_ONLY', 'DEVICE_ID'] = ''

    # ---- drop internal work columns ----------------------------------------
    drop_cols = [c for c in combined.columns
                 if c.startswith('_cat') or c in ('VERSION_MODEL_NUMBER_GUDID',
                                                   'VERSION_MODEL_NUMBER_MDALL',
                                                   'DEVICE_ID_MDALL')]  # same as VERSION_MODEL_NUMBER in MDALL
    combined.drop(columns=[c for c in drop_cols if c in combined.columns], inplace=True)

    # ---- canonical column order ---------------------------------------------
    lead_cols = [
        'VERSION_MODEL_NUMBER', 'MATCH_TYPE', 'SOURCE',
        'BRAND_NAME', 'MDALL_BRAND_NAME', 'COMPANY_NAME',
        'DEVICE_ID', 'MDALL_DEVICE_ID',
        'LICENCE_NUMBER', 'LICENCE_STATUS', 'FIRST_LICENCE_DT', 'END_DATE',
        'DEVICE_ID_ISSUING_AGENCY',
        'MRI_SAFETY_STATUS', 'LABELED_CONTAINS_NRL', 'GMDN_TERMS',
        'product_code', 'product_code_description',
        'SEARCH_TERM', 'SOURCE',
    ]
    # Remove duplicate column names in lead list
    seen_lead: set = set()
    lead_cols = [c for c in lead_cols if not (c in seen_lead or seen_lead.add(c))]
    remaining = [c for c in combined.columns if c not in lead_cols]
    combined = combined[lead_cols + remaining]

    return combined


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame) -> None:
    total = len(df)
    logger.info('\n' + '=' * 60)
    logger.info('MERGE SUMMARY')
    logger.info('=' * 60)
    for col, label in [('SOURCE', 'by source'), ('MATCH_TYPE', 'by match type')]:
        logger.info(f'\nRows {label}:')
        for val, count in df[col].value_counts().items():
            logger.info(f'  {val:<25} {count:>8,}  ({count/total*100:.1f}%)')

    # Devices present in both sources
    both = df[df['SOURCE'] == 'BOTH']
    if not both.empty:
        logger.info(f'\nBrand coverage in matched rows:')
        for brand, cnt in both['BRAND_NAME'].value_counts().head(10).items():
            logger.info(f'  {brand:<45} {cnt:>6,}')
    logger.info('=' * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Merge GUDID and MDALL CSVs')
    parser.add_argument('--gudid',   required=True,
                        help='Glob pattern or path to GUDID CSV '
                             '(e.g. "gudid_downloads/knee_implants_filtered_*.csv")')
    parser.add_argument('--mdall',   required=True,
                        help='Glob pattern or path to MDALL CSV '
                             '(e.g. "mdall_downloads/mdall_knee_devices_*.csv")')
    parser.add_argument('--output',  default=None,
                        help='Output CSV path (default: merged_<timestamp>.csv)')
    args = parser.parse_args()

    gudid_df = load_latest(args.gudid)
    mdall_df = load_latest(args.mdall)

    logger.info(f'GUDID rows loaded: {len(gudid_df):,}')
    logger.info(f'MDALL rows loaded: {len(mdall_df):,}')

    merged = merge(gudid_df, mdall_df)

    out_path = args.output or f'merged_gudid_mdall_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    merged.to_csv(out_path, index=False, encoding='utf-8')

    print_summary(merged)
    logger.info(f'\n[OK] {len(merged):,} rows -> {out_path}')

    return merged


if __name__ == '__main__':
    main()
