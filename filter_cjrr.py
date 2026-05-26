"""
CJRR Filter
===========

Filters the merged GUDID/MDALL dataset to:
  1. Keep only CJRR-recognised manufacturers
  2. Exclude partial / unicompartmental knee replacements

Input : merged_gudid_mdall.csv  (from merge_gudid_mdall.py)
Output: cjrr_filtered_<timestamp>.csv

Usage:
  python filter_cjrr.py
  python filter_cjrr.py --input merged_gudid_mdall.csv --output cjrr_filtered.csv
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CJRR manufacturer mapping
# Each entry:  CJRR canonical name  ->  list of keywords (case-insensitive)
#              matched against COMPANY_NAME
# ---------------------------------------------------------------------------

CJRR_MANUFACTURERS: dict[str, list[str]] = {
    'Zimmer/Biomet/Sulzer/Centerpulse': [
        'zimmer', 'biomet', 'sulzer', 'centerpulse',
    ],
    'Stryker/Osteonics/Howmedica': [
        'stryker', 'osteonics', 'howmedica',
    ],
    'DePuy/Finsbury/J & J': [
        'depuy', 'finsbury', 'j & j', 'j&j', 'johnson',
    ],
    'Smith & Nephew': [
        'smith & nephew', 'smith and nephew',
    ],
    'MicroPort/Wright Medical': [
        'microport', 'wright medical', 'wright ortho',
    ],
    'Corin': [
        'corin',
    ],
    'Medacta International': [
        'medacta',
    ],
    'Link': [
        'waldemar link', ' link ', 'link gmbh',
    ],
    'Tecres Medical': [
        'tecres',
    ],
    'Ceraver': [
        'ceraver',
    ],
}

# ---------------------------------------------------------------------------
# Partial / unicompartmental knee exclusion patterns
# Applied to GMDN_TERMS and BRAND_NAME (case-insensitive)
# ---------------------------------------------------------------------------

PARTIAL_KNEE_PATTERNS: list[str] = [
    # GMDN-based (most reliable)
    r'unicondylar',
    r'unicompartmental',
    # Brand / product name based
    r'\buni\b',                      # "Uni" as standalone word
    r'partial knee',
    r'patellofemoral\s+(?:joint\s+)?(?:prosthesis|replacement|implant|system)',
    r'\bpkr\b',
    r'\bpka\b',
    r'\bpfj\b',                      # patellofemoral joint
    r'oxford\s+partial',
    r'oxford\s+uni',
    r'oxford\s+knee',                # Oxford Knee = unicompartmental
    r'\bgenesis\s+uni\b',
    r'gender\s+solutions\s+pfj',     # Zimmer patellofemoral
    r'journey\s+pfj',
    r'vanguard\s+m\b',               # Vanguard M = partial
    r'repicci',
]

# ---------------------------------------------------------------------------
# Brand-name exclusion pattern
# Applied to BRAND_NAME + MDALL_BRAND_NAME (case-insensitive).
# Catches devices that slip through the GMDN filter because their GMDN term is
# valid but their product name is clearly non-knee or non-implant.
# ---------------------------------------------------------------------------

BRAND_NAME_EXCLUSION_PATTERN = re.compile(
    # Knee-adjacent non-implant orthopaedic items
    r"\baugment\w*\b"                                   # augment, augmentation
    r"|\bprovisional\b"                                 # provisional sizing components
    r"|\bsizing\s+trial\b"                              # "sizing trial" phrase
    r"|\btrial\s+(?:insert|component|femor|tibial)\b"   # trial insert / component
    r"|\binstrument(?:ation|s)?\b"                      # instrumentation / instruments
    # Completely unrelated medical device categories
    r"|\bcatheter\b"
    r"|\belectrode\b"
    r"|\bhearing\s+(?:aid|system)\b"
    r"|\bdefibrillator\b"
    r"|\bpacemaker\b"
    r"|\bcardioplegia\b"
    r"|\becg\b|\beeg\b"
    r"|\bmicrowave\b"
    r"|\btransmitter\b"
    r"|\bgloves?\b"
    r"|\btoothbrush\b"
    r"|\bdental\b"
    r"|\bneedle\b"
    r"|\bsheath\b"
    r"|\bvalve\b"
    r"|\bcanul[ae]\b"
    r"|\bipg\b"             # implantable pulse generator (cardiac)
    r"|\bpmr\b"             # unrelated to knee (physical medicine/radio)
    r"|\baudio\s+cable\b"
    r"|\bjig\b"             # cutting jig = instrument
    r"|\bmri\b"
    r"|\bcement\s+spacer\b"
    # spacer but NOT "tibial spacer" (tibial spacers are revision knee components)
    r"|\bspacer\b(?!.*\btibial\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Core filter functions
# ---------------------------------------------------------------------------

def _make_cjrr_lookup() -> dict[str, str]:
    """Build a lowercase-keyword -> CJRR canonical name lookup dict."""
    lookup: dict[str, str] = {}
    for canonical, keywords in CJRR_MANUFACTURERS.items():
        for kw in keywords:
            lookup[kw.lower()] = canonical
    return lookup


_KW_LOOKUP = _make_cjrr_lookup()


def assign_cjrr_manufacturer(company: str) -> str:
    """
    Return the CJRR canonical manufacturer name, or '' if not recognised.
    Matches are case-insensitive substring checks.
    """
    if not company:
        return ''
    company_lower = company.lower()
    for kw, canonical in _KW_LOOKUP.items():
        if kw in company_lower:
            return canonical
    return ''


def is_irrelevant_by_brand(brand: str, mdall_brand: str) -> bool:
    """Return True if either brand name matches a non-implant exclusion pattern."""
    combined = f"{brand} {mdall_brand}"
    return bool(BRAND_NAME_EXCLUSION_PATTERN.search(combined))


def is_partial_knee(brand: str, gmdn: str) -> bool:
    """Return True if the device appears to be a partial/unicompartmental knee."""
    text = f'{brand} {gmdn}'.lower()
    for pattern in PARTIAL_KNEE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def filter_cjrr(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply CJRR manufacturer filter and partial-knee exclusion.

    Returns filtered DataFrame with an added CJRR_MANUFACTURER column.
    """
    df = df.copy()

    # 1. Assign canonical CJRR manufacturer
    df['CJRR_MANUFACTURER'] = df['COMPANY_NAME'].apply(assign_cjrr_manufacturer)

    n_total = len(df)

    # 2. Keep only recognised CJRR manufacturers
    cjrr_mask = df['CJRR_MANUFACTURER'] != ''
    df_cjrr = df[cjrr_mask].copy()
    n_after_mfr = len(df_cjrr)
    logger.info(f'Manufacturer filter: {n_total:,} -> {n_after_mfr:,} rows '
                f'({n_total - n_after_mfr:,} removed)')

    # Log per-manufacturer counts
    logger.info('\nRows per CJRR manufacturer:')
    for mfr, count in df_cjrr['CJRR_MANUFACTURER'].value_counts().items():
        logger.info(f'  {mfr:<40} {count:>7,}')

    # 3. Exclude partial knees
    partial_mask = df_cjrr.apply(
        lambda r: is_partial_knee(r.get('BRAND_NAME', ''), r.get('GMDN_TERMS', '')),
        axis=1,
    )
    df_filtered = df_cjrr[~partial_mask].copy()
    n_after_partial = len(df_filtered)
    logger.info(f'\nPartial knee exclusion: {n_after_mfr:,} -> {n_after_partial:,} rows '
                f'({n_after_mfr - n_after_partial:,} removed)')

    # 4. Exclude irrelevant devices by brand name / mdall brand name
    brand_mask = df_filtered.apply(
        lambda r: is_irrelevant_by_brand(
            r.get('BRAND_NAME', ''),
            r.get('MDALL_BRAND_NAME', ''),
        ),
        axis=1,
    )
    df_brand_filtered = df_filtered[~brand_mask].copy()
    n_after_brand = len(df_brand_filtered)
    logger.info(f'\nBrand-name exclusion: {n_after_partial:,} -> {n_after_brand:,} rows '
                f'({n_after_partial - n_after_brand:,} removed)')

    return df_brand_filtered


def print_summary(original: pd.DataFrame, filtered: pd.DataFrame) -> None:
    logger.info('\n' + '=' * 60)
    logger.info('FILTER SUMMARY')
    logger.info('=' * 60)
    logger.info(f'Input rows:          {len(original):>8,}')
    logger.info(f'After mfr filter:    {len(filtered) + filtered.shape[0] - filtered.shape[0]:>8,}')  # placeholder
    logger.info(f'Final output rows:   {len(filtered):>8,}')
    logger.info(f'Rows removed:        {len(original) - len(filtered):>8,}  '
                f'({(len(original)-len(filtered))/len(original)*100:.1f}%)')

    if 'SOURCE' in filtered.columns:
        logger.info('\nSource breakdown:')
        for src, cnt in filtered['SOURCE'].value_counts().items():
            logger.info(f'  {src:<20} {cnt:>7,}')

    if 'CJRR_MANUFACTURER' in filtered.columns:
        logger.info('\nFinal rows per CJRR manufacturer:')
        for mfr, cnt in filtered['CJRR_MANUFACTURER'].value_counts().items():
            logger.info(f'  {mfr:<40} {cnt:>7,}')
    logger.info('=' * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Filter merged GUDID/MDALL to CJRR manufacturers')
    parser.add_argument('--input',  default='merged_gudid_mdall.csv',
                        help='Input merged CSV (default: merged_gudid_mdall.csv)')
    parser.add_argument('--output', default=None,
                        help='Output CSV path (default: cjrr_filtered_<timestamp>.csv)')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        # Try to find the latest merged file
        candidates = sorted(Path('.').glob('merged_gudid_mdall*.csv'),
                            key=lambda p: p.stat().st_mtime)
        if candidates:
            input_path = candidates[-1]
            logger.info(f'Using latest merged file: {input_path}')
        else:
            raise FileNotFoundError(f'Cannot find input file: {args.input}')

    logger.info(f'Loading {input_path}')
    df = pd.read_csv(input_path, dtype=str).fillna('')
    logger.info(f'Loaded {len(df):,} rows')

    filtered = filter_cjrr(df)
    print_summary(df, filtered)

    out_path = args.output or f'cjrr_filtered_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    filtered.to_csv(out_path, index=False, encoding='utf-8')
    logger.info(f'\n[OK] Saved {len(filtered):,} rows -> {out_path}')


if __name__ == '__main__':
    main()
