#!/usr/bin/env python3
"""
enrich_reference_data.py

Post-processing enrichment from two supplemental reference files:
  - ZimmerOrtechImplants.xlsx  (procedure records -> catalogue-number lookup)
  - TKA Model Info With Dimensions.xlsx  (model/size -> AP/ML/thickness lookup)

For components with identical medial and lateral AP depths (most symmetric designs),
both ap_medial and ap_lateral receive the same value.  For asymmetric tibial
baseplates (Persona Keeled), ap_medial and ap_lateral differ.

Usage:
    python enrich_reference_data.py --library outputs/knee_implant_library_*.csv
    python enrich_reference_data.py --library outputs/... \\
        --zimmer-ortech ZimmerOrtechImplants.xlsx \\
        --tka-dims "TKA Model Info With Dimensions.xlsx"

Output:
    outputs/knee_implant_library_enriched_<timestamp>.csv
"""

import argparse
import glob
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Fields this script can fill (never overwrite existing non-blank values)
_FILLABLE = [
    "ap_medial", "ap_lateral", "ml_width", "thickness", "diameter",
    "metal_material", "poly_material", "antioxidant",
]

_BLANK_SENTINELS = {"", "n/a", "nan", "none"}


def _is_blank(v: Any) -> bool:
    return not isinstance(v, str) or v.strip().lower() in _BLANK_SENTINELS


def _normalize_cat(raw: Any) -> str:
    """Coerce a catalogue number to a plain digit string (no hyphens or spaces)."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    s = raw.strip()
    # Convert scientific-notation floats that pandas may produce when reading xlsx
    if "e" in s.lower() or (s.replace(".", "", 1).isdigit() and "." in s):
        try:
            s = str(int(float(s)))
        except (ValueError, OverflowError):
            pass
    return s.replace("-", "").replace(" ", "")


# ---------------------------------------------------------------------------
# ZimmerOrtech loader
# ---------------------------------------------------------------------------

class ZimmerOrtecLoader:
    """
    Reads ZimmerOrtechImplants.xlsx and builds a catalogue-number -> enrichment dict.

    The file stores three component columns per row (Femoral, Tibial, Bearing).
    Each is parsed for AP/ML dimensions, thickness, and material.
    """

    # -- Tibial / Femoral AP / ML patterns ---
    _AP_BEFORE = re.compile(r"(\d+\.?\d*)\s*mm\s+A/P", re.I)   # "46mm A/P"
    _ML_BEFORE = re.compile(r"(\d+\.?\d*)\s*mm\s+M/L", re.I)   # "66mm M/L"
    _AP_AFTER  = re.compile(r"A/P\s+(\d+\.?\d*)\s*mm", re.I)   # "A/P 61.5mm"
    _ML_AFTER  = re.compile(r"M/L\s+(\d+\.?\d*)\s*mm", re.I)   # "M/L 68mm"

    # -- Bearing thickness patterns ---
    _THICK_LABEL = re.compile(r"Thickness\s+(\d+\.?\d*)\s*mm", re.I)
    _THICK_LEAD  = re.compile(r"^(\d+\.?\d*)\s*mm\b", re.I)    # "10mm, VE, MC, ..."

    def _parse_tibial_details(self, text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not isinstance(text, str):
            return out
        m = self._AP_BEFORE.search(text) or self._AP_AFTER.search(text)
        if m:
            v = float(m.group(1))
            out["ap_medial"] = v
            out["ap_lateral"] = v  # tibial plates are symmetric
        m = self._ML_BEFORE.search(text) or self._ML_AFTER.search(text)
        if m:
            out["ml_width"] = float(m.group(1))
        return out

    def _parse_femoral_details(self, text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not isinstance(text, str):
            return out
        # Material
        if re.search(r"\bzimaloy\b", text, re.I):
            out["metal_material"] = "CoCr"
        elif re.search(r"\boxinium\b", text, re.I):
            out["metal_material"] = "OxZr"
        # AP / ML (e.g. "A/P 70.4mm, M/L 76.5mm")
        m = self._AP_AFTER.search(text) or self._AP_BEFORE.search(text)
        if m:
            v = float(m.group(1))
            out["ap_medial"] = v
            out["ap_lateral"] = v  # femoral AP is a single measurement
        m = self._ML_AFTER.search(text) or self._ML_BEFORE.search(text)
        if m:
            out["ml_width"] = float(m.group(1))
        return out

    def _parse_bearing(
        self, details: str, mat_general: str, mat_detail: str
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if isinstance(details, str):
            m = self._THICK_LABEL.search(details)
            if not m:
                m = self._THICK_LEAD.match(details)
            if m:
                out["thickness"] = float(m.group(1))
            # Some CR-Flex inserts carry AP/ML for the tibial plateau
            m = self._AP_BEFORE.search(details) or self._AP_AFTER.search(details)
            if m:
                v = float(m.group(1))
                out["ap_medial"] = v
                out["ap_lateral"] = v
            m = self._ML_BEFORE.search(details) or self._ML_AFTER.search(details)
            if m:
                out["ml_width"] = float(m.group(1))
        # Material: detail takes priority over general
        if isinstance(mat_detail, str) and re.search(r"\bvivacit\b", mat_detail, re.I):
            out["poly_material"] = "HXLPE+VitE"
            out["antioxidant"] = "Vitamin E"
        elif isinstance(mat_general, str):
            if re.search(r"cross.?linked", mat_general, re.I):
                out["poly_material"] = "XLPE"
            elif re.search(r"\buhmwpe\b", mat_general, re.I):
                out["poly_material"] = "UHMWPE"
        return out

    def load(self, path: str) -> Dict[str, Dict[str, Any]]:
        """Return {normalized_cat_num: {field: value}} for every catalogue number found."""
        df = pd.read_excel(path, header=0, dtype=str)
        # Columns have form "Devices (Knee Arthroplasty)|FieldName"
        col_map = {c: c.split("|")[1] for c in df.columns if "|" in c}
        df.rename(columns=col_map, inplace=True)
        df = df.fillna("")

        result: Dict[str, Dict[str, Any]] = {}

        def _merge(cat_raw: str, data: Dict[str, Any]) -> None:
            cat = _normalize_cat(cat_raw)
            if not cat or not data:
                return
            if cat not in result:
                result[cat] = {}
            for k, v in data.items():
                if k not in result[cat]:   # first value wins per cat num
                    result[cat][k] = v

        for _, row in df.iterrows():
            tib_det = row.get("Tibial_Details", "")
            _merge(row.get("Tibial_CatNum", ""),  self._parse_tibial_details(tib_det))
            # Tibial_CatNum2 is sometimes identical; merge it anyway (harmless dup)
            _merge(row.get("Tibial_CatNum2", ""), self._parse_tibial_details(tib_det))
            _merge(row.get("Femoral_CatNum", ""), self._parse_femoral_details(
                row.get("Femoral_Details", "")))
            _merge(row.get("Bearing_CatNum", ""), self._parse_bearing(
                row.get("Bearing_Details", ""),
                row.get("Bearing_Material_General", ""),
                row.get("Bearing_Material_Detail", ""),
            ))

        log.info("ZimmerOrtech: %d unique catalogue numbers loaded from %s",
                 len(result), os.path.basename(path))
        return result


# ---------------------------------------------------------------------------
# TKA Dimensions loader
# ---------------------------------------------------------------------------

class TKADimensionsLoader:
    """
    Reads TKA Model Info With Dimensions.xlsx and builds a lookup keyed on
    (model_norm, comp_norm, size_norm) -> {ap_medial, ap_lateral, ml_width, ...}.
    """

    # Map TKA file component type strings -> normalised library component_type tokens
    _COMP_TOKENS: List[Tuple[str, str]] = [
        ("non-modular (monoblock) tibial", "monoblock tibial"),
        ("tibial insert",                  "insert"),
        ("tibial base",                    "tibial"),   # "Tibial Base", "Tibial base Plate", "Tibial Baseplate"
        ("tibial component",               "tibial"),
        ("tibial (stemmed)",               "tibial"),
        ("tibial",                         "tibial"),
        ("femoral",                        "femoral"),
        ("patellar",                       "patellar"),
        ("insert",                         "insert"),
    ]

    # Map library component_type -> normalised token used in the lookup above
    _LIB_COMP_MAP: Dict[str, str] = {
        "Femoral":          "femoral",
        "Tibial":           "tibial",
        "Monoblock Tibial": "monoblock tibial",
        "Insert":           "insert",
        "Patellar":         "patellar",
        "Femoral Stem":     "femoral",
        "Tibial Stem":      "tibial",
        "TKA System":       "femoral",   # best guess for dimension matching
    }

    @staticmethod
    def _norm_comp_tka(s: str) -> str:
        s = s.strip().lower()
        if s in ("nan", "none", ""):
            return ""
        for pattern, token in TKADimensionsLoader._COMP_TOKENS:
            if pattern in s:
                return token
        return s

    @staticmethod
    def _norm_model(s: str) -> str:
        return re.sub(r"\s+", " ", str(s).strip().lower())

    @staticmethod
    def _norm_size(v: Any) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip().upper()

    def load(self, path: str) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """Return {(model_norm, comp_norm, size_norm): dims_dict}."""
        df = pd.read_excel(path)
        df.columns = [c.strip() for c in df.columns]

        result: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        for _, row in df.iterrows():
            model = str(row.get("ModelName", "")).strip()
            comp  = str(row.get("ComponentType", "")).strip()
            size  = row.get("Size", None)
            if not model or model.lower() == "nan":
                continue

            ap_med = row.get("AP Medial")
            ap_lat = row.get("AP Lateral")
            ml     = row.get("ML")
            thick  = row.get("Thick")
            diam   = row.get("Diameter (mm)")

            dims: Dict[str, Any] = {}
            if pd.notna(ap_med):
                dims["ap_medial"] = float(ap_med)
                dims["ap_lateral"] = float(ap_lat) if pd.notna(ap_lat) else float(ap_med)
            if pd.notna(ml):
                dims["ml_width"] = float(ml)
            if pd.notna(thick):
                t_str = str(thick).strip()
                # Skip multi-value strings like "9, 10, 12, 14, 17, 20"
                if re.match(r"^\d+\.?\d*$", t_str):
                    dims["thickness"] = float(t_str)
            if pd.notna(diam):
                dims["diameter"] = float(diam)

            if not dims:
                continue

            key = (
                self._norm_model(model),
                self._norm_comp_tka(comp),
                self._norm_size(size),
            )
            if key not in result:
                result[key] = dims

        log.info("TKADims: %d (model, comp, size) entries loaded from %s",
                 len(result), os.path.basename(path))
        return result

    def find(
        self,
        lookup: Dict[Tuple[str, str, str], Dict[str, Any]],
        brand_name: str,
        version_model: str,
        comp_type: str,
        size: str,
    ) -> Optional[Dict[str, Any]]:
        """Match a library record against the TKA dimensions lookup."""
        comp_norm = self._LIB_COMP_MAP.get(comp_type, "")
        size_norm = self._norm_size(size)

        # Build candidate text to search for model name (normalise hyphens)
        search = re.sub(r"[-]", " ", " ".join(filter(None, [
            brand_name.lower() if isinstance(brand_name, str) else "",
            version_model.lower() if isinstance(version_model, str) else "",
        ])))

        # Collect candidate model names for this component type, longest-first
        # so more specific names (e.g. "nexgen lps flex") beat shorter ones ("nexgen")
        model_candidates = sorted(
            {k[0] for k in lookup if k[1] == comp_norm},
            key=len,
            reverse=True,
        )

        for model in model_candidates:
            # Normalise hyphens in model name too before substring test
            model_norm_hyph = re.sub(r"[-]", " ", model)
            if model_norm_hyph not in search:
                continue
            key = (model, comp_norm, size_norm)
            if key in lookup:
                return lookup[key]

        # Fallback: some TKA rows have a blank ComponentType (e.g. Attune, Sigma femoral).
        # The AP/ML ranges in those rows are femoral-scale; only use for femoral queries.
        if comp_norm == "femoral":
            model_candidates_blank = sorted(
                {k[0] for k in lookup if k[1] == ""},
                key=len,
                reverse=True,
            )
            for model in model_candidates_blank:
                model_norm_hyph = re.sub(r"[-]", " ", model)
                if model_norm_hyph not in search:
                    continue
                key = (model, "", size_norm)
                if key in lookup:
                    return lookup[key]

        return None


# ---------------------------------------------------------------------------
# Library enrichment
# ---------------------------------------------------------------------------

def _resolve_glob(pattern: str) -> str:
    """Expand a glob pattern and return the most-recently-modified match."""
    matches = glob.glob(pattern)
    if not matches:
        sys.exit(f"ERROR: no file matching '{pattern}'")
    return max(matches, key=os.path.getmtime)


def enrich_library(
    library_path: str,
    zimmer_ortech_path: Optional[str],
    tka_dims_path: Optional[str],
    output_dir: str = "outputs",
) -> str:
    """Load library excel file, apply enrichments, write enriched CSV.  Returns output path."""
    library_path = _resolve_glob(library_path)
    log.info("Library: %s", library_path)

    df = pd.read_excel(library_path, dtype=str, keep_default_na=False)
    if "ap_length" in df.columns and "ap_medial" not in df.columns:
        df.rename(columns={"ap_length": "ap_medial"}, inplace=True)
        if "ap_lateral" not in df.columns:
            df.insert(df.columns.get_loc("ap_medial") + 1, "ap_lateral", "")
        log.info("Renamed legacy 'ap_length' column to 'ap_medial' and added 'ap_lateral'.")

    # Ensure columns exist
    for col in _FILLABLE:
        if col not in df.columns:
            df[col] = ""

    # --- Build lookup tables ---
    ortech_lookup: Dict[str, Dict[str, Any]] = {}
    tka_loader: Optional[TKADimensionsLoader] = None
    tka_lookup: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    if zimmer_ortech_path:
        p = _resolve_glob(zimmer_ortech_path)
        ortech_lookup = ZimmerOrtecLoader().load(p)

    if tka_dims_path:
        p = _resolve_glob(tka_dims_path)
        tka_loader = TKADimensionsLoader()
        tka_lookup = tka_loader.load(p)

    # --- Apply enrichments ---
    n_ortech = 0
    n_tka = 0
    n_fields_ortech = 0
    n_fields_tka = 0

    for idx, row in df.iterrows():
        cat_norm = _normalize_cat(row.get("catalogue_num", ""))
        brand    = row.get("brand_name", "")
        version  = row.get("version_model_number", "")
        comp     = row.get("component_type", "")
        size     = row.get("size", "")

        touched_ortech = False
        touched_tka    = False

        # -- Phase 1: ZimmerOrtech catalogue-num match --
        if cat_norm and cat_norm in ortech_lookup:
            enrichment = ortech_lookup[cat_norm]
            for field, value in enrichment.items():
                if field in _FILLABLE and _is_blank(row.get(field, "")):
                    df.at[idx, field] = str(value)
                    n_fields_ortech += 1
                    touched_ortech = True

        # -- Phase 2: TKA dimensions model+size match --
        if tka_loader and tka_lookup:
            dims = tka_loader.find(tka_lookup, brand, version, comp, size)
            if dims:
                for field, value in dims.items():
                    if field in _FILLABLE and _is_blank(df.at[idx, field]):
                        df.at[idx, field] = str(value)
                        n_fields_tka += 1
                        touched_tka = True

        # -- Update data_source --
        if touched_ortech or touched_tka:
            source_tags = []
            if touched_ortech:
                source_tags.append("ZimmerOrtech")
                n_ortech += 1
            if touched_tka:
                source_tags.append("TKADims")
                n_tka += 1
            existing_src = df.at[idx, "data_source"] if "data_source" in df.columns else ""
            new_tags = "|".join(source_tags)
            df.at[idx, "data_source"] = (
                f"{existing_src}|{new_tags}" if existing_src else new_tags
            )

    # --- Backfill ap_lateral from ap_medial for symmetric records ---
    # Any record that has ap_medial but still lacks ap_lateral (including records
    # from the legacy ap_length rename) gets ap_lateral = ap_medial, since a single
    # AP measurement implies a symmetric component.
    if "ap_medial" in df.columns and "ap_lateral" in df.columns:
        backfill_mask = (
            ~df["ap_medial"].apply(_is_blank)
            & df["ap_lateral"].apply(_is_blank)
        )
        df.loc[backfill_mask, "ap_lateral"] = df.loc[backfill_mask, "ap_medial"]
        n_backfilled = backfill_mask.sum()
        if n_backfilled:
            log.info("Backfilled ap_lateral = ap_medial for %d symmetric records", n_backfilled)

    # --- Write output ---
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(output_dir, f"knee_implant_library_enriched_{timestamp}.csv")
    df.to_csv(out_path, index=False)

    log.info("ZimmerOrtech: %d records touched, %d fields filled", n_ortech, n_fields_ortech)
    log.info("TKADims:      %d records touched, %d fields filled", n_tka, n_fields_tka)
    log.info("Output: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich knee implant library with AP/ML dimensions and material data "
                    "from ZimmerOrtechImplants.xlsx and TKA Model Info With Dimensions.xlsx."
    )
    parser.add_argument(
        "--library",
        required=True,
        help="Path or glob to the library CSV (e.g. outputs/knee_implant_library_*.csv)",
    )
    parser.add_argument(
        "--zimmer-ortech",
        default="ZimmerOrtechImplants.xlsx",
        help="Path to ZimmerOrtechImplants.xlsx (default: ZimmerOrtechImplants.xlsx)",
    )
    parser.add_argument(
        "--tka-dims",
        default="TKA Model Info With Dimensions.xlsx",
        help="Path to TKA Model Info With Dimensions.xlsx",
    )
    parser.add_argument(
        "--no-zimmer-ortech",
        action="store_true",
        help="Skip ZimmerOrtech enrichment",
    )
    parser.add_argument(
        "--no-tka-dims",
        action="store_true",
        help="Skip TKA dimensions enrichment",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output directory (default: outputs)",
    )
    args = parser.parse_args()

    enrich_library(
        library_path=args.library,
        zimmer_ortech_path=None if args.no_zimmer_ortech else args.zimmer_ortech,
        tka_dims_path=None if args.no_tka_dims else args.tka_dims,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
