"""
Knee Implant Reference Library Pipeline - v3
=============================================

Orchestrates the full pipeline:
  1. Bulk download FDA GUDID data          (optimized_gudid_downloader.py)
  2. Bulk download Health Canada MDALL     (mdall_bulk_downloader.py)
  3. Merge both sources on catalogue #     (merge_gudid_mdall.py)
  4. Filter to CJRR manufacturers          (filter_cjrr.py)
  5. Download missing eIFU PDFs            (eifu_downloader.py)   [optional]
  6. Supplement with NZ medical devices CSV (CSVSupplementer)
  7. Enrich materials from eIFU PDFs        (eifu_material_extractor.py)
  8. Classify component types              (ComponentTypeClassifier)
  9. Extract design fields                 (FieldExtractor)
 10. Apply field applicability matrix      (FieldApplicabilityMatrix)
 11. Export enriched ImplantRecord CSV

eIFU file naming convention (used by eifu_downloader.py):
  {manufacturer}_{system_name}_{doc_id}.pdf
  e.g.  stryker_triathlon_QIN4396EIFU.pdf
        zimmer_persona_eIFU.pdf
        depuy_attune_eIFU.pdf

  The manufacturer prefix routes to the correct parser in
  eifu_material_extractor.py.  One PDF covers many catalogue numbers
  (e.g. all femoral sizes of Triathlon) — the extractor handles this.

Usage (full pipeline):
  python implant_lib_v3.py

Usage (skip data downloads, use existing files):
  python implant_lib_v3.py --cjrr cjrr_filtered_20260311_203320.csv

Usage (also download missing eIFUs automatically):
  python implant_lib_v3.py --cjrr cjrr_filtered.csv --download-eifus

Usage (start from a pre-merged file):
  python implant_lib_v3.py --merged merged_gudid_mdall.csv
"""

import argparse
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Pipeline module imports
# ---------------------------------------------------------------------------
from optimized_gudid_downloader import ProductCodeBulkDownloader
from mdall_bulk_downloader import MDALLBulkDownloader
import merge_gudid_mdall as _merger
import filter_cjrr as _cjrr_filter
from eifu_material_extractor import (
    extract_materials as _extract_eifu_materials,
    _detect_variant as _detect_eifu_variant,
)
from eifu_downloader import (
    EIFUDownloadOrchestrator,
    _brand_to_system,
)
from gudid_description_enricher import enrich_dataframe as _enrich_gudid_descriptions
from catalogue_num_sizer import extract_from_catalogue as _cat_size

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ===========================================================================
# Data structures
# ===========================================================================


class FieldStatus(Enum):
    PRESENT = "present"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass
class ImplantRecord:
    """Core implant data structure."""

    # --- Identifiers --------------------------------------------------------
    device_id: Optional[str] = None
    catalogue_num: Optional[str] = None
    gtin: Optional[str] = None
    mdall_licence_number: Optional[str] = None
    mdall_device_id: Optional[str] = None

    # --- Basic info ---------------------------------------------------------
    brand_name: Optional[str] = None
    mdall_brand_name: Optional[str] = (
        None  # raw MDALL device name (often richer than GUDID)
    )
    version_model_number: Optional[str] = None
    company_name: Optional[str] = None
    manufacturer: Optional[str] = None  # CJRR canonical name
    product_code: Optional[str] = None
    device_name: Optional[str] = (
        None  # product code description (e.g. "Knee Joint Femoral Component")
    )
    device_description: Optional[str] = (
        None  # GUDID per-device description (e.g. "Triathlon CR Femoral, Size 3")
    )
    device_sizes_text: Optional[str] = None  # GUDID size annotations
    gmdn_name: Optional[str] = None

    # --- Component classification -------------------------------------------
    component_type: Optional[str] = None
    component_description: Optional[str] = None
    replacement_type: str = "P"  # P = primary, R = revision

    # --- Design characteristics ---------------------------------------------
    fixation: Optional[str] = None
    stability: Optional[str] = None
    bearing_type: Optional[str] = (
        None  # Fixed Bearing | Mobile Bearing | Rotating Platform
    )
    side: Optional[str] = None

    # --- Materials (from eIFU / text extraction) ----------------------------
    metal_material: Optional[str] = None
    poly_material: Optional[str] = None
    ceramic_material: Optional[str] = None
    antioxidant: Optional[str] = None           # e.g. "Vitamin E" (Vivacit-E, etc.)
    material_standard: Optional[str] = None

    # --- Dimensions ---------------------------------------------------------
    size: Optional[str] = None
    ap_length: Optional[float] = None
    ml_width: Optional[float] = None
    thickness: Optional[float] = None
    diameter: Optional[float] = None

    # --- Design features ----------------------------------------------------
    design_articular_surface: Optional[str] = None
    design_fixation_surface: Optional[str] = None
    surface_treatment: Optional[str] = None        # fixation-surface coating: HA, PMMA, TiN, TPS, CaP …

    # --- Source tracing -----------------------------------------------------
    data_source: List[str] = field(default_factory=list)
    match_type: Optional[str] = None  # exact | normalized | unmatched
    source_db: Optional[str] = None  # BOTH | GUDID_ONLY | MDALL_ONLY

    # --- Metadata -----------------------------------------------------------
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    confidence_score: float = 0.0
    needs_review: bool = False
    notes: str = ""
    field_status: Dict[str, str] = field(default_factory=dict)


# ===========================================================================
# Utility / enrichment classes (kept from v2, minimal changes)
# ===========================================================================


class ComponentTypeClassifier:
    """Classifies implant component type from GMDN terms and product name."""

    @staticmethod
    def classify(gmdn_name: str = "") -> Optional[str]:
        text = gmdn_name.lower()

        if "hip" in text:
            return "Hip Prosthesis"

        if any(w in text for w in ["stem", "extension stem"]):
            if "femoral" in text or "femur" in text:
                return "Femoral Stem"
            if "tibial" in text or "tibia" in text:
                return "Tibial Stem"
            return "Stem"

        if any(w in text for w in ["insert", "bearing", "articular surface", "liner"]):
            return "Insert"

        if any(w in text for w in ["patellar", "patella", "patello"]):
            return "Patellar"

        if any(w in text for w in ["tibial", "tibia"]) and "insert" not in text:
            if any(w in text for w in ["baseplate", "tray", "platform", "component"]):
                return "Tibial"

        if any(w in text for w in ["femoral", "femur"]) and "stem" not in text:
            if any(w in text for w in ["component", "condyle"]):
                return "Femoral"

        if "femorotibial" in text or ("femoral" in text and "tibial" in text):
            return "TKA System"

        return None


class FieldExtractor:
    """Extracts structured design fields from free text."""

    @staticmethod
    def extract_fixation(text: str) -> Optional[str]:
        if re.search(r"\bcement(?:ed)?\b", text, re.I):
            return "Cemented"
        if re.search(r"\bcementless\b|\buncemented\b|\bporous\b", text, re.I):
            return "Cementless"
        if re.search(r"\bhybrid\b", text, re.I):
            return "Hybrid"
        return None

    @staticmethod
    def extract_stability(text: str) -> Optional[str]:
        if re.search(r"\bsemi[- ]constrain(?:ed)?\b", text, re.I):
            return "Semi-Constrained"
        if re.search(r"\bPS\b|\bposterior[- ]stabili[sz]ed\b", text, re.I):
            return "PS"
        if re.search(r"\bCR\b|\bcruciate[- ]retain(?:ing)?\b", text, re.I):
            return "CR"
        if re.search(r"\bCPS\b", text, re.I):
            return "CPS"
        if re.search(r"\bBCS\b|\bbi[- ]cruciate\b", text, re.I):
            return "BCS"
        if re.search(r"\bconstrain(?:ed)?\b", text, re.I):
            return "Constrained"
        return None

    @staticmethod
    def extract_bearing_type(text: str) -> Optional[str]:
        if re.search(r"\brotating\s+platform\b", text, re.I):
            return "Rotating Platform"
        if re.search(r"\bmedial\s+pivot\b", text, re.I):
            return "Medial Pivot"
        if re.search(r"\bmobile\s+bearing\b|\bmobile\b|\brotating\b", text, re.I):
            return "Mobile Bearing"
        if re.search(r"\bfixed\s+bearing\b|\bfixed\b", text, re.I):
            return "Fixed Bearing"
        return None

    @staticmethod
    def extract_side(text: str) -> Optional[str]:
        if re.search(r"\bleft\b", text, re.I):
            return "Left"
        if re.search(r"\bright\b", text, re.I):
            return "Right"
        if re.search(r"\buniversal\b|\bneutral\b", text, re.I):
            return "Universal"
        return None

    @staticmethod
    def extract_material(
        text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (metal, polymer, antioxidant).

        Polymer priority: cross-linking is detected first; Vitamin E is then
        checked independently so that XLPE+VitE products (e.g. Vivacit-E) are
        classified correctly rather than defaulting to UHMWPE+VitE.
        """
        t = text.lower()

        metal = None
        if "cobalt" in t or "cocr" in t or "co-cr" in t:
            metal = "CoCr"
        elif "titanium" in t or "ti-6al-4v" in t:
            metal = "Titanium"
        elif "oxinium" in t or "oxidized zirconium" in t:
            metal = "OxZr"

        polymer = None
        antioxidant = None
        if "uhmwpe" in t or "polyethylene" in t:
            is_xlpe = bool(
                re.search(r"xlpe|cross.?link(?:ed)?", t)
            )
            is_vite = bool(
                re.search(r"vitamin\s*e\b|vit\.?\s*e\b|vivacit", t)
            )

            if is_xlpe and is_vite:
                polymer = "XLPE+VitE"
            elif is_xlpe:
                polymer = "XLPE"
            elif is_vite:
                polymer = "UHMWPE+VitE"
            else:
                polymer = "UHMWPE"

            if is_vite:
                antioxidant = "Vitamin E"

        return metal, polymer, antioxidant

    @staticmethod
    def extract_surface_treatment(text: str) -> Optional[str]:
        """Return the fixation-surface coating code, or None.

        PA (Peri-Apatite, Stryker brand name) is normalised to HA.
        Values are checked in specificity order so that, e.g., an HA-coated
        titanium spray surface is reported as HA rather than TPS.
        """
        t = text.lower()
        # Hydroxyapatite — including Peri-Apatite (Stryker) and plain "HA"
        if re.search(
            r'hydroxyapatite|peri.?apatite|\bha\s+coat|\bha\b.*coat|coat.*\bha\b',
            t,
        ):
            return 'HA'
        # Titanium nitride
        if re.search(r'\btin\b|titanium\s+nitride', t):
            return 'TiN'
        # Titanium plasma spray
        if re.search(r'\btps\b|titanium\s+plasma\s+spray|plasma\s+spray', t):
            return 'TPS'
        # Calcium phosphate / tricalcium phosphate
        if re.search(r'calcium\s+phosphate|\bca\s*p\b|tricalcium', t):
            return 'CaP'
        # PMMA / bone cement (cemented implants with pre-coat)
        if re.search(r'\bpmma\b|polymethylmethacrylate', t):
            return 'PMMA'
        return None

    @staticmethod
    def extract_size(text: str) -> Optional[str]:
        m = re.search(r"size\s+(\d+(?:-\d+)?)", text, re.I)
        return m.group(1) if m else None


class MDALLFormatter:
    """
    Formats catalogue numbers for MDALL lookup using CJRR-validated
    manufacturer-specific rules.  Retained for manual/targeted MDALL
    single-device lookups; bulk downloads use MDALLBulkDownloader directly.
    """

    @staticmethod
    def format_for_mdall(catalogue_num: str, manufacturer: str) -> str:
        if not catalogue_num or not manufacturer:
            return ""
        cat = re.sub(r"[^A-Z0-9]", "", str(catalogue_num).strip().upper())
        norm = MDALLFormatter._normalize_manufacturer(manufacturer)
        if norm == "Stryker/Osteonics/Howmedica":
            return MDALLFormatter._fmt_stryker(cat)
        if norm == "Zimmer/Biomet/Sulzer/Centerpulse":
            return MDALLFormatter._fmt_zimmer(cat)
        if norm == "DePuy/Finsbury/J & J":
            return MDALLFormatter._fmt_depuy(cat)
        return cat

    @staticmethod
    def _normalize_manufacturer(mfr: str) -> str:
        m = mfr.lower()
        if any(x in m for x in ["depuy", "finsbury", "j & j", "j&j", "johnson"]):
            return "DePuy/Finsbury/J & J"
        if any(x in m for x in ["zimmer", "biomet", "sulzer", "centerpulse"]):
            return "Zimmer/Biomet/Sulzer/Centerpulse"
        if any(x in m for x in ["stryker", "osteonics", "howmedica"]):
            return "Stryker/Osteonics/Howmedica"
        if "smith" in m and "nephew" in m:
            return "Smith & Nephew"
        if any(x in m for x in ["microport", "wright"]):
            return "MicroPort/Wright Medical"
        return mfr

    @staticmethod
    def _fmt_stryker(c: str) -> str:
        if re.search(r"\d[A-Z]\d", c):
            return re.sub(r"(\d)([A-Z])(\d)", r"\1-\2-\3", c)
        if re.search(r"\d{2}[A-Z0-9]*[A-Z]$", c):
            return c[:2] + "-" + c[2:]
        if len(c) == 7:
            return c[:2] + "-" + c[2] + "-" + c[3:]
        return c

    @staticmethod
    def _fmt_zimmer(c: str) -> str:
        if len(c) == 11:
            return f"{c[:2]}-{c[2:6]}-{c[6:9]}-{c[9:]}"
        if len(c) == 9:
            if c[4] == "0":
                return f"00-{c[:4]}-{c[4:7]}-{c[7:]}"
            return f"{c[:4]}-{c[4:6]}-{c[6:]}"
        return c

    @staticmethod
    def _fmt_depuy(c: str) -> str:
        if c[:3] == "178":
            return c
        if len(c) == 9:
            return f"{c[:4]}-{c[4:6]}-{c[6:]}"
        return c


class CSVSupplementer:
    """Supplements records with data from the NZ medical devices CSV."""

    RELEVANT_CATEGORIES = {
        "Femoral knee component",
        "Femoral knee components",
        "Femoral component",
        "Tibial baseplate",
        "Baseplates - tibial",
        "Tibial baseplates or trays",
        "Tibial Insert",
        "Tibial inserts or bearings or articular surfaces",
        "Patellar implant",
        "Patellar implants",
        "Patella implant",
        "Tibial extension stem",
        "Femoral knee extension stem",
        "Tibial or femoral stems",
        "Tibial extension stems",
        "Femoral knee extension stems",
        "Tibial stems",
        "Tibial components",
    }

    @staticmethod
    def load(csv_path: str) -> pd.DataFrame:
        logger.info(f"Loading supplemental CSV: {csv_path}")
        df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str).fillna("")
        if "CategoryName3" in df.columns:
            df = df[df["CategoryName3"].isin(CSVSupplementer.RELEVANT_CATEGORIES)]
        logger.info(f"  {len(df):,} relevant rows loaded")
        return df

    @staticmethod
    def supplement(record: ImplantRecord, df: pd.DataFrame) -> None:
        """Fill missing fields on record from the CSV."""
        if not record.catalogue_num:
            return
        matches = df[df["SupplierProductCode"].astype(str) == str(record.catalogue_num)]
        if matches.empty and record.brand_name and record.company_name:
            matches = df[
                df["BrandName"].str.contains(record.brand_name, case=False, na=False)
                & df["SupplierName"].str.contains(
                    record.company_name, case=False, na=False
                )
            ]
        if matches.empty:
            return
        row = matches.iloc[0]
        if not record.size and row.get("DeviceName2", ""):
            record.size = row["DeviceName2"]
        extra = row.get("AdditionalInformation", "")
        if extra:
            record.notes = (record.notes + f" | CSV: {extra}").lstrip(" | ")
        if "Medical_Devices_CSV" not in record.data_source:
            record.data_source.append("Medical_Devices_CSV")


class FieldApplicabilityMatrix:
    """Marks fields as N/A or Missing based on component type."""

    APPLICABILITY: Dict[str, List[str]] = {
        "stability": ["Femoral", "Insert", "TKA System"],
        "bearing_type": ["Insert"],
        "fixation": ["Femoral", "Tibial", "TKA System"],
        "surface_treatment": ["Femoral", "Tibial"],
        "design_fixation_surface": ["Femoral", "Tibial", "Patellar"],
        "design_articular_surface": ["Femoral", "Insert"],
        "ap_length": ["Femoral", "Tibial"],
        "ml_width": ["Tibial"],
        "diameter": ["Patellar", "Femoral Stem", "Tibial Stem", "Stem"],
        "thickness": ["Insert"],
    }

    @staticmethod
    def mark(record: ImplantRecord) -> None:
        if not record.component_type:
            return
        for fname, applicable_types in FieldApplicabilityMatrix.APPLICABILITY.items():
            val = getattr(record, fname, None)
            if record.component_type not in applicable_types:
                record.field_status[fname] = FieldStatus.NOT_APPLICABLE.value
                setattr(record, fname, "N/A")
            elif val is None or val == "":
                record.field_status[fname] = FieldStatus.MISSING.value
                record.needs_review = True
            else:
                record.field_status[fname] = FieldStatus.PRESENT.value


# ===========================================================================
# GMDN term exclusions — non-implant records to drop after CJRR filter
# ===========================================================================

# Exact GMDN terms that are instruments, kits, hip implants, or data errors.
# Matched after stripping leading/trailing whitespace and quotes (CSV artefact).
_GMDN_EXCLUSIONS: frozenset = frozenset(
    {
        # Surgical instruments
        "Bone file",
        "Bone holding forceps",
        "Implant handling forceps",
        "Surgical implant handling forceps",
        "Orthopaedic bone calliper",
        "Orthopaedic broach",
        "Orthopaedic osteotome",
        "Orthopaedic implant aiming/guiding block",
        "Orthopaedic knee-balance analyser",
        "Optical surgical navigation device tracking system",
        # Kits / containers
        "Device sterilization/disinfection container",
        "Joint prosthesis implantation kit",
        "Orthopaedic surgical procedure kit",
        # Hip / acetabular implants (not knee)
        "Uncoated hip femur prosthesis",
        "Acetabular augmentation implant",
        "Acetabular shell",
        "Constrained polyethylene acetabular liner",
        "Non-constrained polyethylene acetabular liner",
        "Metallic acetabulum prosthesis",
        # Temporary / non-permanent devices
        "Femoral stem centralizer",
        "Orthopaedic cement spacer",
        # Data-quality artefacts
        "False",
        "True",
    }
)

# Keyword patterns to catch variants not in the exact list above
_GMDN_EXCLUSION_PATTERN = re.compile(
    r"forceps|screwdriver|mallet|drill\s*bit|punch|broach|osteotome"
    r"|calliper|caliper|acetabul|hip\s+femur|steriliz|navigation"
    r"|disinfect|splint|sidebar|clamp|cutter|file\b|hook\b",
    re.IGNORECASE,
)


# ===========================================================================
# Main pipeline
# ===========================================================================


class KneeImplantPipeline:
    """
    Orchestrates all pipeline stages.

    Stages can be entered at any point by supplying pre-built CSVs:
      - gudid_csv / mdall_csv  -> skip downloads
      - merged_csv             -> skip downloads + merge
      - cjrr_csv               -> skip downloads + merge + CJRR filter
    """

    def __init__(
        self,
        gudid_output_dir: str = "gudid_downloads",
        mdall_output_dir: str = "mdall_downloads",
        medical_devices_csv_path: Optional[str] = None,
        eifu_directory: Optional[str] = "implant_eIFUs",
        apply_cjrr_filter: bool = True,
        apply_gmdn_filter: bool = True,
        download_eifus: bool = False,  # auto-download missing eIFU PDFs
        download_eifus_headless: bool = False,
        skip_eifus: bool = False,  # disable ALL eIFU activity (download + extraction)
        enrich_gudid_descriptions: bool = True,  # fetch deviceDescription from GUDID API
        gudid_description_cache: str = "gudid_downloads/device_descriptions_cache.json",
        # --- short-circuit inputs (skip earlier stages) ---
        gudid_csv: Optional[str] = None,
        mdall_csv: Optional[str] = None,
        merged_csv: Optional[str] = None,
        cjrr_csv: Optional[str] = None,
        output_dir: str = "outputs",
    ):
        self.gudid_output_dir = gudid_output_dir
        self.mdall_output_dir = mdall_output_dir
        self.csv_path = medical_devices_csv_path
        self.eifu_dir = Path(eifu_directory) if eifu_directory else None
        self.apply_cjrr_filter = apply_cjrr_filter
        self.apply_gmdn_filter = apply_gmdn_filter
        self.download_eifus = download_eifus
        self.download_eifus_headless = download_eifus_headless
        self.skip_eifus = skip_eifus
        self.enrich_gudid_descriptions = enrich_gudid_descriptions
        self.gudid_description_cache = Path(gudid_description_cache)
        self.gudid_csv = gudid_csv
        self.mdall_csv = mdall_csv
        self.merged_csv = merged_csv
        self.cjrr_csv = cjrr_csv
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

        self.records: List[ImplantRecord] = []
        self.csv_df: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Stage 1: GUDID download
    # ------------------------------------------------------------------

    def _get_gudid(self) -> pd.DataFrame:
        """Return GUDID DataFrame — download or load from existing file."""
        if self.gudid_csv:
            path = (
                _merger.load_latest(self.gudid_csv)
                if "*" in self.gudid_csv
                else pd.read_csv(self.gudid_csv, dtype=str).fillna("")
            )
            if isinstance(path, pd.DataFrame):
                df = path
            else:
                df = path
            logger.info(f"Loaded GUDID from file: {len(df):,} rows")
            return df

        # Try to find the latest existing download
        existing = sorted(
            Path(self.gudid_output_dir).glob("knee_devices_bulk_*.csv"),
            key=lambda p: p.stat().st_mtime,
        )
        if existing:
            logger.info(f"Using existing GUDID file: {existing[-1]}")
            df = pd.read_csv(existing[-1], dtype=str).fillna("")
            logger.info(f"  {len(df):,} rows")
            return df

        # Fresh download
        logger.info("Downloading GUDID data (bulk product code download)...")
        downloader = ProductCodeBulkDownloader(output_dir=self.gudid_output_dir)
        df = downloader.download_all_knee_codes()
        if self.apply_gmdn_filter and not df.empty:
            df = downloader.apply_gmdn_filtering(df)
        return df

    # ------------------------------------------------------------------
    # Stage 2: MDALL download
    # ------------------------------------------------------------------

    def _get_mdall(self) -> pd.DataFrame:
        """Return MDALL DataFrame — download or load from existing file."""
        if self.mdall_csv:
            df = pd.read_csv(self.mdall_csv, dtype=str).fillna("")
            logger.info(f"Loaded MDALL from file: {len(df):,} rows")
            return df

        existing = sorted(
            Path(self.mdall_output_dir).glob("mdall_knee_devices_*.csv"),
            key=lambda p: p.stat().st_mtime,
        )
        if existing:
            logger.info(f"Using existing MDALL file: {existing[-1]}")
            df = pd.read_csv(existing[-1], dtype=str).fillna("")
            logger.info(f"  {len(df):,} rows")
            return df

        logger.info("Downloading MDALL data...")
        downloader = MDALLBulkDownloader(output_dir=self.mdall_output_dir)
        df = downloader.download_all(fetch_identifiers=True)
        return df

    # ------------------------------------------------------------------
    # Stage 3: Merge
    # ------------------------------------------------------------------

    def _get_merged(
        self, gudid_df: pd.DataFrame, mdall_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Merge GUDID and MDALL DataFrames using the two-pass catalogue match."""
        if self.merged_csv:
            df = pd.read_csv(self.merged_csv, dtype=str).fillna("")
            logger.info(f"Loaded merged CSV: {len(df):,} rows")
            return df

        # If both inputs are empty (--cjrr path skips download+merge),
        # return an empty DataFrame — _apply_cjrr will load the CJRR CSV.
        if gudid_df.empty and mdall_df.empty:
            existing = sorted(
                Path(".").glob("merged_gudid_mdall*.csv"),
                key=lambda p: p.stat().st_mtime,
            )
            if existing:
                logger.info(f"Using existing merged file: {existing[-1]}")
                return pd.read_csv(existing[-1], dtype=str).fillna("")
            return pd.DataFrame()

        logger.info("Merging GUDID and MDALL...")
        merged = _merger.merge(gudid_df, mdall_df)
        _merger.print_summary(merged)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.output_dir / f"merged_gudid_mdall_{ts}.csv"
        merged.to_csv(out, index=False, encoding="utf-8")
        logger.info(f"[OK] Merged CSV saved: {out}")
        return merged

    # ------------------------------------------------------------------
    # Stage 4: CJRR filter
    # ------------------------------------------------------------------

    def _filter_gmdn_exclusions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop records whose GMDN term is an instrument, kit, hip implant, etc."""
        if "GMDN_TERMS" not in df.columns:
            return df
        # Normalise: strip whitespace + stray leading/trailing quotes (CSV artefact)
        normalized = df["GMDN_TERMS"].astype(str).str.strip().str.strip('"').str.strip()
        exact_mask = normalized.isin(_GMDN_EXCLUSIONS)
        keyword_mask = normalized.str.contains(_GMDN_EXCLUSION_PATTERN, na=False)
        excluded = exact_mask | keyword_mask
        n = excluded.sum()
        if n:
            logger.info(
                "GMDN exclusion filter: removing %d records with non-implant GMDN terms",
                n,
            )
            for term, cnt in df.loc[excluded, "GMDN_TERMS"].value_counts().items():
                logger.info("  excluded %3d  %r", cnt, term)
        return df[~excluded].reset_index(drop=True)

    def _apply_cjrr(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter to CJRR manufacturers and exclude partial knees."""
        if self.cjrr_csv:
            filtered = pd.read_csv(self.cjrr_csv, dtype=str).fillna("")
            logger.info(f"Loaded CJRR-filtered CSV: {len(filtered):,} rows")
            return filtered

        if not self.apply_cjrr_filter:
            return df

        logger.info("Applying CJRR manufacturer filter...")
        filtered = _cjrr_filter.filter_cjrr(df)
        _cjrr_filter.print_summary(df, filtered)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.output_dir / f"cjrr_filtered_{ts}.csv"
        filtered.to_csv(out, index=False, encoding="utf-8")
        logger.info(f"[OK] CJRR-filtered CSV saved: {out}")
        return filtered

    # ------------------------------------------------------------------
    # Stage 5: Parse DataFrame rows -> ImplantRecord
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: pd.Series) -> ImplantRecord:
        """Convert a merged/filtered DataFrame row into an ImplantRecord."""
        r = ImplantRecord()
        r.version_model_number = row.get("VERSION_MODEL_NUMBER", "") or None
        r.catalogue_num = r.version_model_number
        r.brand_name = row.get("BRAND_NAME", "") or None
        r.mdall_brand_name = row.get("MDALL_BRAND_NAME", "") or None
        r.company_name = row.get("COMPANY_NAME", "") or None
        r.manufacturer = row.get("CJRR_MANUFACTURER", "") or r.company_name
        r.device_id = row.get("DEVICE_ID", "") or None
        r.device_description = row.get("DEVICE_DESCRIPTION", "") or None
        r.device_sizes_text = row.get("DEVICE_SIZES_TEXT", "") or None
        r.gmdn_name = row.get("GMDN_TERMS", "") or None
        r.product_code = row.get("product_code", "") or None
        r.device_name = row.get("product_code_description", "") or None
        r.mdall_licence_number = row.get("LICENCE_NUMBER", "") or None
        r.mdall_device_id = row.get("MDALL_DEVICE_ID", "") or None
        r.match_type = row.get("MATCH_TYPE", "") or None
        r.source_db = row.get("SOURCE", "") or None

        # Populate data_source list
        src = row.get("SOURCE", "")
        if "GUDID" in src or src == "BOTH":
            r.data_source.append("FDA_GUDID")
        if "MDALL" in src or src == "BOTH":
            r.data_source.append("MDALL")

        # Quick text-based material hints from GMDN
        if r.gmdn_name:
            metal, polymer, antioxidant = FieldExtractor.extract_material(r.gmdn_name)
            r.metal_material = metal
            r.poly_material  = polymer
            r.antioxidant    = antioxidant

        notes_parts = []
        mri = row.get("MRI_SAFETY_STATUS", "")
        if mri:
            notes_parts.append(f"MRI: {mri}")
        lic_status = row.get("LICENCE_STATUS", "")
        if lic_status:
            notes_parts.append(f"MDALL status: {lic_status}")
        r.notes = " | ".join(notes_parts)

        return r

    # ------------------------------------------------------------------
    # Stage 4c: GUDID device description enrichment
    # ------------------------------------------------------------------

    def _maybe_enrich_gudid_descriptions(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Fetch deviceDescription + deviceSizes from the GUDID REST API for
        records that have a DEVICE_ID.  Results are cached locally so
        subsequent runs only fetch new DIs.  Skipped when
        enrich_gudid_descriptions=False or no DEVICE_ID column present.
        """
        if not self.enrich_gudid_descriptions:
            logger.info(
                "GUDID description enrichment skipped (enrich_gudid_descriptions=False)"
            )
            return df
        return _enrich_gudid_descriptions(
            df,
            cache_path=self.gudid_description_cache,
        )

    # ------------------------------------------------------------------
    # Stage 5b: Download missing eIFU PDFs (optional)
    # ------------------------------------------------------------------

    def _maybe_download_eifus(self, base_df: pd.DataFrame) -> None:
        """
        If download_eifus=True, trigger EIFUDownloadOrchestrator to fetch
        any eIFU PDFs for systems present in base_df that are not yet
        in eifu_dir.  Runs before _build_eifu_lookup so the freshly
        downloaded PDFs are available immediately.
        """
        if not self.download_eifus or not self.eifu_dir:
            return
        logger.info("Checking for missing eIFU PDFs...")
        orch = EIFUDownloadOrchestrator(
            eifu_dir=str(self.eifu_dir),
            headless=self.download_eifus_headless,
        )
        results = orch.download_missing_for_dataframe(base_df)
        if results:
            orch.print_summary(results)

    # ------------------------------------------------------------------
    # Stage 6: eIFU material enrichment
    # ------------------------------------------------------------------

    def _build_eifu_lookup(self) -> Dict[str, Dict]:
        """
        Scan the eIFU directory, extract materials from each PDF, and build
        a flat lookup with two key strategies:

          (a) Normalised catalogue family prefix — Stryker catalogue-number match
              e.g. "5512FXXX" or "5512F" for family "5512-F-XXX"

          (b) "sys:{manufacturer}:{system}:{component_type}" — Zimmer/DePuy match
              e.g. "sys:zimmer:persona:femoral"

        Values are eIFU material record dicts as returned by extract_materials().
        """
        lookup: Dict[str, Dict] = {}

        if not self.eifu_dir or not self.eifu_dir.exists():
            return lookup

        pdf_files = list(self.eifu_dir.glob("*.pdf"))
        if not pdf_files:
            logger.info("No eIFU PDFs found in %s", self.eifu_dir)
            return lookup

        logger.info(f"Extracting materials from {len(pdf_files)} eIFU PDF(s)...")

        for pdf in pdf_files:
            try:
                records = _extract_eifu_materials(pdf)
                n_before = len(lookup)

                variant = _detect_eifu_variant(pdf)

                for rec in records:
                    # (a) Catalogue-family keys (Stryker)
                    cat_fam = rec.get("catalogue_family")
                    if cat_fam:
                        for k in _eifu_keys(cat_fam):
                            lookup.setdefault(k, rec)

                    # (b) System-level key (all manufacturers)
                    mfr = rec.get("manufacturer", "").lower()
                    sys_ = rec.get("system", "").lower().replace(" ", "_")
                    comp = rec.get("component_type", "").lower()
                    if mfr and sys_ and comp:
                        # Variant-specific key (e.g. attune_cementless) takes
                        # priority; bare key acts as fallback for unmatched fixation
                        if variant:
                            lookup.setdefault(f"sys:{mfr}:{sys_}:{variant}:{comp}", rec)
                        lookup.setdefault(f"sys:{mfr}:{sys_}:{comp}", rec)

                logger.info(
                    "  %s: %d records, %d new lookup keys",
                    pdf.name,
                    len(records),
                    len(lookup) - n_before,
                )
            except Exception as exc:
                logger.warning(f"  {pdf.name}: extraction failed — {exc}")

        logger.info(f"eIFU lookup: {len(lookup)} total keys")
        return lookup

    @staticmethod
    def _enrich_from_eifu(record: ImplantRecord, eifu_lookup: Dict[str, Dict]) -> None:
        """Populate material fields on record from the eIFU lookup."""
        if not eifu_lookup:
            return

        mat: Optional[Dict] = None

        # (a) Catalogue-number prefix match (works for Stryker)
        if record.catalogue_num:
            mat = _find_eifu_match(record.catalogue_num, eifu_lookup)

        # (b) System + component_type match (Zimmer / DePuy)
        if not mat:
            mapping = _brand_to_system(record.brand_name or "")
            if mapping:
                _, _, system = mapping
                mfr_short = (
                    "stryker"
                    if "stryker" in (record.manufacturer or "").lower()
                    else (
                        "zimmer"
                        if "zimmer" in (record.manufacturer or "").lower()
                        else (
                            "depuy"
                            if "depuy" in (record.manufacturer or "").lower()
                            else ""
                        )
                    )
                )
                comp = (record.component_type or "").lower()
                if mfr_short and system and comp:
                    sys_lower = system.lower()
                    # Derive variant from known fixation so we prefer the matching PDF
                    fixation = (record.fixation or "").lower()
                    variant = (
                        "cementless"
                        if fixation in ("cementless", "uncemented")
                        else (
                            "cemented"
                            if fixation == "cemented"
                            else (
                                "mobile"
                                if fixation == "mobile"
                                else "fixed" if fixation == "fixed" else ""
                            )
                        )
                    )
                    # Try variant-specific key first, then bare system key
                    if variant:
                        mat = eifu_lookup.get(
                            f"sys:{mfr_short}:{sys_lower}:{variant}:{comp}"
                        )
                    if not mat:
                        mat = eifu_lookup.get(f"sys:{mfr_short}:{sys_lower}:{comp}")

        if not mat:
            return

        mat_name = mat.get("material_name", "").lower()
        if not record.metal_material and mat_name:
            if "cobalt" in mat_name or "co-cr" in mat_name or "cocr" in mat_name:
                record.metal_material = "CoCr"
            elif "titanium" in mat_name or "ti-6al" in mat_name:
                record.metal_material = "Titanium"
            elif "oxidized" in mat_name or "zirconium" in mat_name:
                record.metal_material = "OxZr"
        if not record.poly_material and mat_name:
            if "uhmwpe" in mat_name or "polyethylene" in mat_name:
                record.poly_material = (
                    "UHMWPE+VitE"
                    if ("vitamin" in mat_name or "vehxpe" in mat_name)
                    else "UHMWPE"
                )

        alloy_std = mat.get("alloy_standard")
        if alloy_std and not record.material_standard:
            record.material_standard = alloy_std

        # Surface treatment from eIFU material name
        # (PA = Peri-Apatite → HA; Hydroxyapatite → HA; TiN → TiN)
        if not record.surface_treatment and mat_name:
            if re.search(r'hydroxyapatite|peri.?apatite', mat_name):
                record.surface_treatment = 'HA'
            elif re.search(r'\btin\b|titanium\s+nitride', mat_name):
                record.surface_treatment = 'TiN'
            elif re.search(r'titanium\s+plasma|plasma\s+spray|\btps\b', mat_name):
                record.surface_treatment = 'TPS'

        if "eIFU" not in record.data_source:
            record.data_source.append("eIFU")

    # ------------------------------------------------------------------
    # Stage 7: General enrichment (classify + extract fields)
    # ------------------------------------------------------------------

    def _enrich(self, record: ImplantRecord, eifu_lookup: Dict[str, Dict]) -> None:
        """Classify component type, extract design fields, enrich from eIFU."""
        search_text = " ".join(
            filter(
                None,
                [
                    record.device_description,
                    record.device_name,
                    record.brand_name,
                    record.mdall_brand_name,
                    record.version_model_number,
                    record.gmdn_name,
                    record.notes,
                ],
            )
        )

        # Component type
        if not record.component_type:
            record.component_type = ComponentTypeClassifier.classify(
                record.gmdn_name or "",
            )

        # Design fields
        if not record.fixation:
            record.fixation = FieldExtractor.extract_fixation(search_text)
        if not record.stability:
            record.stability = FieldExtractor.extract_stability(search_text)
        if not record.bearing_type:
            record.bearing_type = FieldExtractor.extract_bearing_type(search_text)
        if not record.surface_treatment:
            record.surface_treatment = FieldExtractor.extract_surface_treatment(search_text)
        if not record.side:
            record.side = FieldExtractor.extract_side(search_text)
        if not record.size:
            record.size = FieldExtractor.extract_size(search_text)

        # Materials from text (only if not already set)
        if not record.metal_material or not record.poly_material:
            metal, polymer, antioxidant = FieldExtractor.extract_material(search_text)
            if not record.metal_material:
                record.metal_material = metal
            if not record.poly_material:
                record.poly_material = polymer
            if not record.antioxidant:
                record.antioxidant = antioxidant

        # eIFU material enrichment
        self._enrich_from_eifu(record, eifu_lookup)

        # Catalogue-number size extraction (fallback for fields still unset)
        cat_fields = _cat_size(
            catalogue_num=record.catalogue_num or "",
            manufacturer=record.manufacturer or record.company_name or "",
            component_type=record.component_type or "",
            brand_name=record.brand_name or "",
        )
        if not record.size and cat_fields["size"]:
            record.size = cat_fields["size"]
        if not record.side and cat_fields["side"]:
            record.side = cat_fields["side"]
        if not record.thickness and cat_fields["thickness"]:
            record.thickness = cat_fields["thickness"]
        # Only fill stability from catalogue if not yet set and applicable
        if not record.stability and cat_fields["stability"]:
            record.stability = cat_fields["stability"]

        # Confidence score: fraction of non-empty non-meta fields
        total = 0
        filled = 0
        skip = {
            "field_status",
            "data_source",
            "last_updated",
            "notes",
            "needs_review",
            "confidence_score",
        }
        for k, v in asdict(record).items():
            if k in skip:
                continue
            total += 1
            if v is not None and v != "" and v != []:
                filled += 1
        record.confidence_score = filled / total if total else 0.0

        # Field applicability matrix
        FieldApplicabilityMatrix.mark(record)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _to_dataframe(self) -> pd.DataFrame:
        rows = [asdict(r) for r in self.records]
        df = pd.DataFrame(rows)
        if not df.empty:
            df["data_source"] = df["data_source"].apply(
                lambda x: ", ".join(x) if isinstance(x, list) else x
            )
            df["field_status"] = df["field_status"].apply(json.dumps)
        return df

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_summary(self, df: pd.DataFrame) -> None:
        logger.info("\n" + "=" * 70)
        logger.info("PIPELINE SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Total records:           {len(df):>8,}")
        if df.empty:
            return
        logger.info(f"Records needing review:  {df['needs_review'].sum():>8,}")
        logger.info(f"Avg confidence score:    {df['confidence_score'].mean():>8.2f}")

        if "component_type" in df.columns:
            logger.info("\nComponent types:")
            for ct, n in df["component_type"].value_counts().items():
                logger.info(f"  {ct:<35} {n:>7,}")

        if "manufacturer" in df.columns:
            logger.info("\nTop manufacturers (CJRR canonical):")
            for mfr, n in df["manufacturer"].value_counts().head(15).items():
                logger.info(f"  {mfr:<40} {n:>7,}")

        if "source_db" in df.columns:
            logger.info("\nData source:")
            for src, n in df["source_db"].value_counts().items():
                logger.info(f"  {src:<25} {n:>7,}")
        logger.info("=" * 70)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> pd.DataFrame:
        logger.info("=" * 70)
        logger.info("KNEE IMPLANT REFERENCE LIBRARY PIPELINE  v3")
        logger.info("=" * 70)

        # -- Stage 1 + 2: download (may be skipped) ----------------------
        if self.cjrr_csv or self.merged_csv:
            # Jump straight to the later stage
            gudid_df = pd.DataFrame()
            mdall_df = pd.DataFrame()
        else:
            gudid_df = self._get_gudid()
            logger.info(f"GUDID: {len(gudid_df):,} rows")
            # Filter non-implant GMDN terms from GUDID *before* merging with
            # MDALL so instruments / kits / hip devices are never matched
            gudid_df = self._filter_gmdn_exclusions(gudid_df)
            logger.info(f"GUDID after GMDN exclusion: {len(gudid_df):,} rows")
            mdall_df = self._get_mdall()
            logger.info(f"MDALL: {len(mdall_df):,} rows")

        # -- Stage 3: merge ----------------------------------------------
        merged_df = self._get_merged(gudid_df, mdall_df)
        logger.info(f"Merged: {len(merged_df):,} rows")

        # -- Stage 4: CJRR filter ----------------------------------------
        base_df = self._apply_cjrr(merged_df)
        logger.info(f"After CJRR filter: {len(base_df):,} rows")
        # Safety net: also apply GMDN exclusion when entering from --merged
        # or --cjrr (GUDID step was skipped so filter has not run yet)
        if self.merged_csv or self.cjrr_csv:
            base_df = self._filter_gmdn_exclusions(base_df)
            logger.info(f"After GMDN exclusion filter: {len(base_df):,} rows")

        # -- Stage 4c: GUDID device description enrichment ---------------
        base_df = self._maybe_enrich_gudid_descriptions(base_df)

        # -- Stage 5a: Download missing eIFUs (optional) -----------------
        if not self.skip_eifus:
            self._maybe_download_eifus(base_df)
        else:
            logger.info("eIFU download skipped (skip_eifus=True)")

        # -- Stage 5b: CSV supplement ------------------------------------
        if self.csv_path and Path(self.csv_path).exists():
            self.csv_df = CSVSupplementer.load(self.csv_path)

        # -- Stage 6: eIFU lookup ----------------------------------------
        if not self.skip_eifus:
            eifu_lookup = self._build_eifu_lookup()
        else:
            eifu_lookup = {}
            logger.info("eIFU material extraction skipped (skip_eifus=True)")

        # -- Stage 7: parse rows -> ImplantRecord + enrich ---------------
        logger.info(f"Building and enriching {len(base_df):,} ImplantRecords...")
        self.records = []
        for _, row in base_df.iterrows():
            record = self._row_to_record(row)
            if self.csv_df is not None:
                CSVSupplementer.supplement(record, self.csv_df)
            self._enrich(record, eifu_lookup)
            self.records.append(record)

        # -- Stage 8: export ---------------------------------------------
        df = self._to_dataframe()
        self._print_summary(df)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        main_out = self.output_dir / f"knee_implant_library_{ts}.csv"
        df.to_csv(main_out, index=False, encoding="utf-8")
        logger.info(f"\n[OK] Full library saved: {main_out}  ({len(df):,} records)")

        # Needs-review subset + detailed review report
        self._generate_review_report(df, ts)

        return df

    # ------------------------------------------------------------------
    # Review report
    # ------------------------------------------------------------------

    def _generate_review_report(self, df: pd.DataFrame, ts: str) -> None:
        """
        Build and save two complementary review outputs:

          needs_review_{ts}.csv
            One row per manufacturer/brand/component_type group that has at
            least one record needing review.  Editable field columns are
            pre-populated with the current consensus value (if consistent
            across the group) or left blank (mixed / missing).  Fill in the
            blanks and feed the file back via apply_review.py.

            Columns: manufacturer, brand_name, component_type, n_records,
                     n_needing_review, missing_fields, has_eifu_coverage,
                     <editable fields ...>, catalogue_nums

          review_report_{ts}.csv
            Aggregated summary of ALL groups (not just those needing review)
            for reference.
        """
        if df.empty:
            return

        from collections import Counter

        # Fields the user can fill in — order determines column order in CSV
        _FILLABLE = [
            "stability",
            "fixation",
            "bearing_type",
            "metal_material",
            "poly_material",
            "antioxidant",
            "surface_treatment",
            "material_standard",
            "design_fixation_surface",
            "design_articular_surface",
        ]

        def _consensus(series: pd.Series) -> str:
            """Single consistent non-null/non-NA value, else blank."""
            vals = (
                series.replace({"": None, "N/A": None, "n/a": None}).dropna().unique()
            )
            return str(vals[0]) if len(vals) == 1 else ""

        def _missing_fields(field_status_series: pd.Series) -> List[str]:
            missing: set = set()
            for fs_raw in field_status_series:
                try:
                    fs = (
                        json.loads(fs_raw)
                        if isinstance(fs_raw, str)
                        else (fs_raw or {})
                    )
                except Exception:
                    fs = {}
                missing.update(
                    k for k, v in fs.items() if v == FieldStatus.MISSING.value
                )
            return sorted(missing)

        # ----------------------------------------------------------------
        # 1. Grouped fillable needs-review
        # ----------------------------------------------------------------
        review_rows = []
        for (mfr, brand, comp), grp in df.groupby(
            ["manufacturer", "brand_name", "component_type"], dropna=False
        ):
            if not grp["needs_review"].any():
                continue

            has_eifu = grp["data_source"].str.contains("eIFU", na=False).any()
            missing = _missing_fields(grp["field_status"])

            row: Dict = {
                "manufacturer": mfr,
                "brand_name": brand,
                "component_type": comp,
                "n_records": len(grp),
                "n_needing_review": int(grp["needs_review"].sum()),
                "missing_fields": ", ".join(missing),
                "has_eifu_coverage": bool(has_eifu),
            }
            for fname in _FILLABLE:
                row[fname] = _consensus(grp[fname]) if fname in grp.columns else ""
            row["catalogue_nums"] = " | ".join(
                grp["catalogue_num"].dropna().unique().tolist()
            )
            review_rows.append(row)

        review_df = pd.DataFrame(review_rows).sort_values(
            ["manufacturer", "brand_name", "component_type"]
        )
        if not review_df.empty:
            rev_out = self.output_dir / f"needs_review_{ts}.csv"
            review_df.to_csv(rev_out, index=False, encoding="utf-8")
            logger.info(
                "[OK] Needs-review (grouped, fillable) saved: %s  (%d groups)",
                rev_out,
                len(review_df),
            )

        # ----------------------------------------------------------------
        # 2. Full aggregated summary (all groups, for reference)
        # ----------------------------------------------------------------
        agg_rows = []
        for (mfr, brand, comp), grp in df.groupby(
            ["manufacturer", "brand_name", "component_type"], dropna=False
        ):
            missing = _missing_fields(grp["field_status"])
            has_eifu = grp["data_source"].str.contains("eIFU", na=False).any()
            agg_rows.append(
                {
                    "manufacturer": mfr,
                    "brand_name": brand,
                    "component_type": comp,
                    "n_records": len(grp),
                    "n_needing_review": int(grp["needs_review"].sum()),
                    "has_eifu_coverage": bool(has_eifu),
                    "pct_metal_filled": f"{(grp['metal_material'].ne('') & grp['metal_material'].notna()).mean()*100:.0f}%",
                    "pct_poly_filled": f"{(grp['poly_material'].ne('') & grp['poly_material'].notna()).mean()*100:.0f}%",
                    "missing_fields": ", ".join(missing),
                    "sample_cat_nums": " | ".join(
                        grp["catalogue_num"].dropna().unique()[:5].tolist()
                    ),
                }
            )

        agg_df = pd.DataFrame(agg_rows).sort_values(
            ["manufacturer", "brand_name", "component_type"]
        )
        rpt_out = self.output_dir / f"review_report_{ts}.csv"
        agg_df.to_csv(rpt_out, index=False, encoding="utf-8")
        logger.info(
            "[OK] Review report saved: %s  (%d system/component rows)",
            rpt_out,
            len(agg_df),
        )

        # ----------------------------------------------------------------
        # 3. Console summary
        # ----------------------------------------------------------------
        needs_manual = agg_df[agg_df["n_needing_review"] > 0]
        no_eifu = agg_df[~agg_df["has_eifu_coverage"]]

        logger.info("\n" + "=" * 70)
        logger.info("MANUAL ENTRY REQUIRED — SUMMARY")
        logger.info("=" * 70)
        logger.info("  %d system/component groups need attention", len(needs_manual))
        logger.info(
            "  %d individual records flagged", int(agg_df["n_needing_review"].sum())
        )

        if not no_eifu.empty:
            logger.info("\n  Groups with NO eIFU coverage (%d):", len(no_eifu))
            for _, r in no_eifu.iterrows():
                logger.info(
                    "    %-35s  %-30s  %-20s  %d records",
                    r["manufacturer"],
                    r["brand_name"],
                    r["component_type"],
                    r["n_records"],
                )

        if not needs_manual.empty:
            logger.info("\n  Missing-field breakdown (fields needing manual entry):")
            all_mf: List[str] = []
            for mf in needs_manual["missing_fields"]:
                all_mf.extend(f.strip() for f in mf.split(",") if f.strip())
            for fname, cnt in Counter(all_mf).most_common():
                logger.info("    %-35s  %d groups", fname, cnt)

        logger.info("=" * 70)


# ===========================================================================
# eIFU catalogue matching helpers (module-level, used by pipeline)
# ===========================================================================


def _norm_cat(value: str) -> str:
    """Normalise catalogue number: uppercase, strip hyphens + spaces."""
    return re.sub(r"[-\s]", "", value.upper())


def _eifu_keys(catalogue_family: str) -> List[str]:
    """
    Return lookup keys for a catalogue family.

    e.g. "5512-F-XXX" -> ["5512FXXX", "5512F"]
         "5512-F-101" -> ["5512F101", "5512F"]
    """
    norm = _norm_cat(catalogue_family)
    keys = [norm]
    # Also add prefix without trailing digits or XXX suffix
    prefix = re.sub(r"(?:XXX|\d{3})$", "", norm)
    if prefix and prefix != norm:
        keys.append(prefix)
    return keys


def _find_eifu_match(catalogue_num: str, lookup: Dict[str, Dict]) -> Optional[Dict]:
    """Match a catalogue number against the eIFU lookup (exact, then prefix).
    Only scans catalogue-family keys (skips 'sys:...' system-level keys).
    """
    norm = _norm_cat(catalogue_num)
    if norm in lookup:
        return lookup[norm]
    prefix = re.sub(r"\d{3}$", "", norm)
    if prefix != norm and prefix in lookup:
        return lookup[prefix]
    for key, mat in lookup.items():
        if key.startswith("sys:"):
            continue
        if norm.startswith(key) or key.startswith(norm):
            return mat
    return None


# ===========================================================================
# CLI
# ===========================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Knee Implant Reference Library Pipeline v3"
    )
    parser.add_argument(
        "--gudid",
        default=None,
        help="Path (or glob) to existing GUDID CSV — skips GUDID download",
    )
    parser.add_argument(
        "--mdall",
        default=None,
        help="Path to existing MDALL CSV — skips MDALL download",
    )
    parser.add_argument(
        "--merged",
        default=None,
        help="Path to existing merged GUDID+MDALL CSV — skips download + merge",
    )
    parser.add_argument(
        "--cjrr",
        default=None,
        help="Path to existing CJRR-filtered CSV — skips all earlier stages",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Path to NZ medical devices supplemental CSV",
    )
    parser.add_argument(
        "--eifu-dir",
        default="implant_eIFUs",
        help="Directory containing eIFU PDFs (default: implant_eIFUs)",
    )
    parser.add_argument(
        "--no-cjrr-filter",
        action="store_true",
        help="Skip CJRR manufacturer filter",
    )
    parser.add_argument(
        "--no-gmdn-filter",
        action="store_true",
        help="Skip GMDN instrument filter after GUDID download",
    )
    parser.add_argument(
        "--download-eifus",
        action="store_true",
        help="Automatically download missing eIFU PDFs before enrichment "
        "(uses eifu_downloader.py; requires Selenium)",
    )
    parser.add_argument(
        "--eifu-headless",
        action="store_true",
        help="Run eIFU browser downloads in headless mode (no visible window)",
    )
    parser.add_argument(
        "--no-eifus",
        action="store_true",
        help="Skip all eIFU activity — no downloading, no PDF extraction "
        "(useful for quick runs or when Selenium is unavailable)",
    )
    parser.add_argument(
        "--no-gudid-api",
        action="store_true",
        help="Skip GUDID deviceDescription API enrichment "
        "(useful for offline runs; cached results still used if available)",
    )
    parser.add_argument(
        "--gudid-desc-cache",
        default="gudid_downloads/device_descriptions_cache.json",
        help="Path to the GUDID description JSON cache file",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output directory (default: outputs)",
    )
    args = parser.parse_args()

    pipeline = KneeImplantPipeline(
        medical_devices_csv_path=args.csv,
        eifu_directory=args.eifu_dir,
        apply_cjrr_filter=not args.no_cjrr_filter,
        apply_gmdn_filter=not args.no_gmdn_filter,
        download_eifus=args.download_eifus,
        download_eifus_headless=args.eifu_headless,
        skip_eifus=args.no_eifus,
        enrich_gudid_descriptions=not args.no_gudid_api,
        gudid_description_cache=args.gudid_desc_cache,
        gudid_csv=args.gudid,
        mdall_csv=args.mdall,
        merged_csv=args.merged,
        cjrr_csv=args.cjrr,
        output_dir=args.output_dir,
    )

    df = pipeline.run()
    print(
        f"\n[OK] Pipeline complete — {len(df):,} records written to {args.output_dir}/"
    )
    return df


if __name__ == "__main__":
    main()
