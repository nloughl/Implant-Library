"""
Apply Manual Review
===================
Reads a filled-in needs_review CSV (produced by implant_lib_v3.py) and
applies the manually entered field values back to a knee_implant_library CSV.

Match strategy
--------------
Each row in the review file represents one manufacturer + brand_name +
component_type group.  For every editable field column that has been filled
in, the value is written to all matching records in the library that
currently have that field blank or flagged as missing.

By default only blank fields are overwritten.  Use --overwrite to also
replace existing non-blank values.

Usage
-----
  # Typical: fill in needs_review CSV, then run:
  python apply_review.py \\
      --library outputs/knee_implant_library_20260314_160034.csv \\
      --review  outputs/needs_review_20260314_160034.csv

  # With glob for latest library:
  python apply_review.py \\
      --library "outputs/knee_implant_library_*.csv" \\
      --review  outputs/needs_review_filled.csv

  # Force-overwrite existing values too:
  python apply_review.py \\
      --library outputs/knee_implant_library_20260314_160034.csv \\
      --review  outputs/needs_review_filled.csv \\
      --overwrite

  # Specify output path explicitly:
  python apply_review.py \\
      --library outputs/knee_implant_library_20260314_160034.csv \\
      --review  outputs/needs_review_filled.csv \\
      --output  outputs/knee_implant_library_updated.csv
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Fields that the review CSV may contain filled-in values for.
# Order must match the column order written by implant_lib_v3.py.
_FILLABLE_FIELDS: List[str] = [
    'stability',
    'fixation',
    'bearing_type',
    'metal_material',
    'poly_material',
    'material_standard',
    'design_fixation_surface',
    'design_articular_surface',
]

# Columns used to match review rows to library records.
_MATCH_KEYS: List[str] = ['manufacturer', 'brand_name', 'component_type']


def _is_blank(val) -> bool:
    """True if a library cell is empty / N/A / NaN."""
    if val is None:
        return True
    s = str(val).strip()
    return s == '' or s.lower() in ('nan', 'n/a', 'none')


def apply_review(
    library_path: str,
    review_path: str,
    output_path: str,
    overwrite: bool = False,
) -> pd.DataFrame:
    """
    Apply filled review values to the library and save.

    Parameters
    ----------
    library_path  : path to the source knee_implant_library CSV
    review_path   : path to the filled needs_review CSV
    output_path   : where to write the updated library
    overwrite     : if True, overwrite existing non-blank values too

    Returns
    -------
    Updated library DataFrame.
    """
    lib = pd.read_csv(library_path, dtype=str).fillna('')
    rev = pd.read_csv(review_path, dtype=str).fillna('')

    editable = [f for f in _FILLABLE_FIELDS if f in rev.columns and f in lib.columns]
    if not editable:
        logger.error(
            "No editable fields found that exist in both files.\n"
            "  Expected fields: %s\n"
            "  Review columns:  %s\n"
            "  Library columns: %s",
            _FILLABLE_FIELDS, list(rev.columns), list(lib.columns),
        )
        return lib

    logger.info("Library:  %s  (%d records)", library_path, len(lib))
    logger.info("Review:   %s  (%d groups)", review_path, len(rev))
    logger.info("Editable fields: %s", editable)

    n_fields_applied = 0
    n_records_touched = 0

    for _, rev_row in rev.iterrows():
        # Build the boolean mask for matching library records
        mask = pd.Series(True, index=lib.index)
        for key in _MATCH_KEYS:
            if key not in lib.columns or key not in rev_row.index:
                continue
            val = str(rev_row[key]).strip()
            if val:
                mask &= lib[key].str.strip() == val

        matched = lib[mask]
        if matched.empty:
            logger.debug(
                "No library records matched: %s / %s / %s",
                rev_row.get('manufacturer', ''),
                rev_row.get('brand_name', ''),
                rev_row.get('component_type', ''),
            )
            continue

        record_touched = False
        for fname in editable:
            fill_val = str(rev_row.get(fname, '')).strip()
            if not fill_val or fill_val.lower() in ('nan', 'n/a', 'none'):
                continue  # nothing entered for this field → skip

            if overwrite:
                target_idx = matched.index
            else:
                # Only fill rows where the field is currently blank
                target_idx = matched[matched[fname].apply(_is_blank)].index

            if len(target_idx):
                lib.loc[target_idx, fname] = fill_val
                n_fields_applied += len(target_idx)
                record_touched = True
                logger.debug(
                    "  %s / %s / %s — set %s=%r on %d records",
                    rev_row.get('manufacturer', ''), rev_row.get('brand_name', ''),
                    rev_row.get('component_type', ''), fname, fill_val, len(target_idx),
                )

        if record_touched:
            n_records_touched += int(mask.sum())

    logger.info(
        "Applied %d field values across %d records",
        n_fields_applied, n_records_touched,
    )

    lib.to_csv(output_path, index=False, encoding='utf-8')
    logger.info("[OK] Updated library saved: %s  (%d records)", output_path, len(lib))
    return lib


def _resolve_library(path_or_glob: str) -> str:
    """Resolve a path or glob pattern to the most recently modified match."""
    if '*' not in path_or_glob:
        return path_or_glob
    candidates = sorted(Path('.').glob(path_or_glob), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No files matched: {path_or_glob}")
    logger.info("Resolved library glob to: %s", candidates[-1])
    return str(candidates[-1])


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Apply filled manual-review CSV back to the implant library'
    )
    parser.add_argument(
        '--library', required=True,
        help='Path (or glob) to knee_implant_library_*.csv',
    )
    parser.add_argument(
        '--review', required=True,
        help='Path to the filled needs_review_*.csv',
    )
    parser.add_argument(
        '--output', default=None,
        help='Output path for updated library '
             '(default: knee_implant_library_reviewed_<ts>.csv beside the library)',
    )
    parser.add_argument(
        '--overwrite', action='store_true',
        help='Also overwrite existing non-blank values in the library '
             '(default: only fill fields that are currently blank)',
    )
    args = parser.parse_args()

    lib_path = _resolve_library(args.library)

    if args.output:
        out_path = args.output
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_path = str(
            Path(lib_path).parent / f"knee_implant_library_reviewed_{ts}.csv"
        )

    apply_review(lib_path, args.review, out_path, overwrite=args.overwrite)


if __name__ == '__main__':
    main()
