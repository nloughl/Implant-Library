"""
GUDID Device Description Enricher
===================================
Fetches the `deviceDescription` field (and device sizes) from the FDA GUDID
REST API for each device in a DataFrame that has a DEVICE_ID column.

Results are cached to a local JSON file so subsequent runs are fast — only
newly-seen DEVICE_IDs trigger API calls.

Extracted fields written back to the DataFrame:
  DEVICE_DESCRIPTION   – free-text description, e.g.
                         "Triathlon CR Femoral Component, Size 3"
                         "ATTUNE Cementless PS Fixed Bearing Tibial Tray"
  DEVICE_SIZES_TEXT    – concatenated size annotations from deviceSizes[]
                         e.g. "Width: 60 mm | Height: 9 mm"

Usage (standalone):
  python gudid_description_enricher.py \\
      --input  outputs/cjrr_filtered.csv \\
      --output outputs/cjrr_filtered_enriched.csv

  python gudid_description_enricher.py \\
      --input  outputs/cjrr_filtered.csv \\
      --cache  gudid_downloads/device_descriptions_cache.json \\
      --rate   2          # requests per second (default: 2)
      --dry-run           # show what would be fetched, don't call API
"""

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE = "https://accessgudid.nlm.nih.gov/api/v3"
_LOOKUP_URL = f"{_API_BASE}/devices/lookup.json"
_DEFAULT_CACHE = Path("gudid_downloads") / "device_descriptions_cache.json"
_DEFAULT_RATE = 2   # requests per second — stay well within GUDID limits


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(cache_path: Path) -> Dict[str, dict]:
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not read cache %s: %s — starting fresh", cache_path, exc)
    return {}


def _save_cache(cache: Dict[str, dict], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# API response parsing
# ---------------------------------------------------------------------------

def _parse_response(data: dict) -> Tuple[str, str]:
    """
    Extract (device_description, sizes_text) from a GUDID API response.

    Handles both the v3 envelope structure and bare device dicts.
    Each field is parsed independently so a failure in one does not
    wipe out a successfully-parsed other field.
    """
    # Unwrap v3 envelope: {"gudid": {"device": {...}}}
    try:
        device = data["gudid"]["device"] if "gudid" in data else data
    except Exception:
        return "", ""

    # Description — parsed independently
    try:
        description = str(device.get("deviceDescription") or "").strip()
    except Exception:
        description = ""

    # Sizes — parsed independently so an exception here doesn't lose description.
    # Actual API structure:
    #   "deviceSizes": {"deviceSize": [{"sizeType": ..., "sizeText": ...,
    #                                   "size": {"unit": ..., "value": ...}}, ...]}
    sizes_text = ""
    try:
        sizes_wrapper = device.get("deviceSizes") or {}
        size_list = sizes_wrapper if isinstance(sizes_wrapper, list) else (
            sizes_wrapper.get("deviceSize") or []
        )
        size_parts = []
        for s in size_list:
            stype = s.get("sizeType", "")
            stext = s.get("sizeText", "")
            nested = s.get("size") or {}
            sval = nested.get("value", "")
            sunit = nested.get("unit", "")
            if stext:
                part = f"{stype}: {stext}".strip(": ")
            elif sval:
                part = f"{stype}: {sval}{' ' + sunit if sunit else ''}".strip(": ")
            else:
                part = ""
            if part:
                size_parts.append(part)
        sizes_text = " | ".join(size_parts)
    except Exception as exc:
        logger.debug("deviceSizes parse error: %s", exc)

    return description, sizes_text


# ---------------------------------------------------------------------------
# Core enrichment
# ---------------------------------------------------------------------------

def enrich_dataframe(
    df: pd.DataFrame,
    cache_path: Path = _DEFAULT_CACHE,
    rate_per_second: float = _DEFAULT_RATE,
    dry_run: bool = False,
) -> pd.DataFrame:
    """
    Add DEVICE_DESCRIPTION and DEVICE_SIZES_TEXT columns to df by fetching
    from the GUDID API for any DEVICE_ID not already in the cache.

    Parameters
    ----------
    df               : DataFrame with a DEVICE_ID column
    cache_path       : path to the JSON cache file
    rate_per_second  : max API requests per second
    dry_run          : if True, log what would be fetched but make no calls

    Returns
    -------
    df with DEVICE_DESCRIPTION and DEVICE_SIZES_TEXT columns added/updated.
    """
    if "DEVICE_ID" not in df.columns:
        logger.warning("DEVICE_DESCRIPTION enrichment skipped: no DEVICE_ID column")
        df["DEVICE_DESCRIPTION"] = ""
        df["DEVICE_SIZES_TEXT"] = ""
        return df

    cache = _load_cache(cache_path)

    # Collect unique, non-empty DIs that aren't cached yet
    all_dis = df["DEVICE_ID"].dropna().unique().tolist()
    all_dis = [d for d in all_dis if str(d).strip() and str(d).strip() != "nan"]
    uncached = [d for d in all_dis if d not in cache]

    logger.info(
        "GUDID description enrichment: %d total DIs, %d cached, %d to fetch",
        len(all_dis), len(all_dis) - len(uncached), len(uncached),
    )

    if uncached and not dry_run:
        session = requests.Session()
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "KneeImplantLibrary/1.0",
        })
        min_interval = 1.0 / rate_per_second
        last_call = 0.0
        n_ok = 0
        n_fail = 0

        for i, di in enumerate(uncached, 1):
            # Rate limit
            wait = min_interval - (time.time() - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.time()

            try:
                resp = session.get(_LOOKUP_URL, params={"di": di}, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                desc, sizes = _parse_response(data)
                cache[di] = {"device_description": desc, "device_sizes_text": sizes}
                n_ok += 1
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    # Device not found — cache empty entry to avoid re-fetching
                    cache[di] = {"device_description": "", "device_sizes_text": ""}
                    logger.debug("DI not found: %s", di)
                else:
                    cache[di] = {"device_description": "", "device_sizes_text": ""}
                    logger.warning("HTTP error for DI %s: %s", di, exc)
                n_fail += 1
            except Exception as exc:
                cache[di] = {"device_description": "", "device_sizes_text": ""}
                logger.warning("Error fetching DI %s: %s", di, exc)
                n_fail += 1

            # Save cache periodically (every 100 fetches) and at the end
            if i % 100 == 0 or i == len(uncached):
                _save_cache(cache, cache_path)
                logger.info(
                    "  %d / %d fetched  (%d ok, %d failed)",
                    i, len(uncached), n_ok, n_fail,
                )

        logger.info(
            "GUDID description fetch complete: %d ok, %d failed — cache saved to %s",
            n_ok, n_fail, cache_path,
        )

    elif dry_run and uncached:
        logger.info("Dry run — would fetch %d DIs (not calling API)", len(uncached))

    # Join cache entries back to the dataframe
    df = df.copy()
    df["DEVICE_DESCRIPTION"] = df["DEVICE_ID"].map(
        lambda di: cache.get(str(di), {}).get("device_description", "")
    ).fillna("")
    df["DEVICE_SIZES_TEXT"] = df["DEVICE_ID"].map(
        lambda di: cache.get(str(di), {}).get("device_sizes_text", "")
    ).fillna("")

    n_filled = (df["DEVICE_DESCRIPTION"] != "").sum()
    logger.info(
        "DEVICE_DESCRIPTION populated for %d / %d records (%.0f%%)",
        n_filled, len(df), 100 * n_filled / max(len(df), 1),
    )
    return df


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Enrich a CJRR-filtered CSV with GUDID device descriptions"
    )
    parser.add_argument("--input",  required=True, help="Input CSV with DEVICE_ID column")
    parser.add_argument("--output", default=None,  help="Output CSV path (default: overwrites input)")
    parser.add_argument(
        "--cache", default=str(_DEFAULT_CACHE),
        help=f"JSON cache file path (default: {_DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--rate", type=float, default=_DEFAULT_RATE,
        help=f"Max API requests per second (default: {_DEFAULT_RATE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be fetched without calling the API",
    )
    args = parser.parse_args()

    df = pd.read_csv(args.input, dtype=str).fillna("")
    logger.info("Loaded %d records from %s", len(df), args.input)

    df = enrich_dataframe(
        df,
        cache_path=Path(args.cache),
        rate_per_second=args.rate,
        dry_run=args.dry_run,
    )

    out = args.output or args.input
    df.to_csv(out, index=False, encoding="utf-8")
    logger.info("[OK] Saved %d records to %s", len(df), out)


if __name__ == "__main__":
    main()
