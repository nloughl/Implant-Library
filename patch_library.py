"""
Patch Library
=============
Applies two targeted fixes to an existing knee_implant_library CSV without
re-running the full pipeline:

  1. Size date-coercion fix — prefixes date-like size values ("1-2", "3-4",
     "5-6", "7+") with "Size " so Excel does not interpret them as dates.

  2. Component-type "system" fix — re-classifies records that were wrongly
     tagged as "Femoral Stem" or "Tibial Stem" because the word "system"
     in the device name matched the old `"stem" in text` substring check.
     Uses the corrected word-boundary regex.

Usage
-----
  python patch_library.py --library outputs/knee_implant_library_*.csv
  python patch_library.py --library outputs/knee_implant_library_20260405_160015.csv
  python patch_library.py --library outputs/knee_implant_library_*.csv --output outputs/patched.csv
  python patch_library.py --library outputs/knee_implant_library_*.csv --dry-run
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Corrected component-type classifier (inline — no dependency on implant_lib_v3)
# ---------------------------------------------------------------------------

def _classify_text(text: str) -> Optional[str]:
    t = text.lower()

    if "hip" in t:
        return "Hip Prosthesis"

    # Use word boundary so "system" does NOT match \bstems?\b
    if re.search(r"\bstems?\b", t) or "extension stem" in t:
        if "femoral" in t or "femur" in t:
            return "Femoral Stem"
        if "tibial" in t or "tibia" in t:
            return "Tibial Stem"
        return "Stem"

    if any(w in t for w in ["insert", "bearing", "articular surface", "liner"]):
        return "Insert"

    if any(w in t for w in ["patellar", "patella", "patello"]):
        return "Patellar"

    if any(w in t for w in ["tibial", "tibia"]) and "insert" not in t:
        if any(w in t for w in ["baseplate", "tray", "platform", "component"]):
            return "Tibial"

    if any(w in t for w in ["femoral", "femur"]) and not re.search(r"\bstems?\b", t):
        if any(w in t for w in ["component", "condyle"]):
            return "Femoral"

    if "femorotibial" in t or ("femoral" in t and "tibial" in t):
        return "TKA System"

    return None


def classify(device_description: str = "", gmdn_name: str = "") -> Optional[str]:
    """Check device_description first; fall back to gmdn_name."""
    if device_description:
        result = _classify_text(device_description)
        if result is not None:
            return result
    if gmdn_name:
        return _classify_text(gmdn_name)
    return None


# ---------------------------------------------------------------------------
# Size fix
# ---------------------------------------------------------------------------

# Values that Excel parses as dates — match exactly so we don't touch
# things like "AB 1-2" (those already have a prefix).
_DATE_LIKE_SIZE = re.compile(r"^\d+-\d+$|^\d+\+$")


def fix_size(val: str) -> str:
    val = str(val).strip()
    if _DATE_LIKE_SIZE.match(val):
        return f"Size {val}"
    return val


# ---------------------------------------------------------------------------
# Main patch logic
# ---------------------------------------------------------------------------

def patch(library_path: str, output_path: str, dry_run: bool = False) -> pd.DataFrame:
    df = pd.read_csv(library_path, dtype=str).fillna("")

    logger.info("Loaded %d records from %s", len(df), library_path)

    # -- Fix 1: size date-coercion --
    if "size" in df.columns:
        old_sizes = df["size"].copy()
        df["size"] = df["size"].apply(fix_size)
        changed_sizes = (df["size"] != old_sizes).sum()
        logger.info("Size fix: updated %d cells", changed_sizes)
        if changed_sizes:
            examples = df.loc[df["size"] != old_sizes, ["catalogue_num", "size"]].head(5)
            logger.info("  Sample:\n%s", examples.to_string(index=False))
    else:
        logger.warning("'size' column not found — skipping size fix")

    # -- Fix 2: component_type reclassification --
    if "component_type" in df.columns:
        stem_types = {"Femoral Stem", "Tibial Stem", "Stem"}
        candidate_mask = df["component_type"].isin(stem_types)
        candidates = df[candidate_mask].copy()
        logger.info(
            "Component-type fix: checking %d records currently classified as a stem type",
            len(candidates),
        )

        n_changed = 0
        for idx, row in candidates.iterrows():
            new_type = classify(
                device_description=row.get("device_description", ""),
                gmdn_name=row.get("gmdn_name", ""),
            )
            if new_type is None:
                continue
            if new_type != row["component_type"]:
                logger.info(
                    "  [%s] %r  %s -> %s",
                    row.get("catalogue_num", ""),
                    (row.get("device_description") or row.get("gmdn_name", ""))[:80],
                    row["component_type"],
                    new_type,
                )
                df.at[idx, "component_type"] = new_type
                n_changed += 1

        logger.info("Component-type fix: reclassified %d records", n_changed)
    else:
        logger.warning("'component_type' column not found — skipping component-type fix")

    if dry_run:
        logger.info("[DRY RUN] No file written.")
    else:
        df.to_csv(output_path, index=False, encoding="utf-8")
        logger.info("[OK] Patched library saved: %s  (%d records)", output_path, len(df))

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_library(path_or_glob: str) -> str:
    if "*" not in path_or_glob:
        return path_or_glob
    candidates = sorted(Path(".").glob(path_or_glob), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No files matched: {path_or_glob}")
    logger.info("Resolved glob to: %s", candidates[-1])
    return str(candidates[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch an existing knee_implant_library CSV")
    parser.add_argument("--library", required=True, help="Path (or glob) to knee_implant_library_*.csv")
    parser.add_argument("--output", default=None, help="Output path (default: knee_implant_library_patched_<ts>.csv)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing output file")
    args = parser.parse_args()

    lib_path = _resolve_library(args.library)

    if args.output:
        out_path = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = str(Path(lib_path).parent / f"knee_implant_library_patched_{ts}.csv")

    patch(lib_path, out_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
