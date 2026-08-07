# Knee Implant Reference Library

A data pipeline for building a structured reference library of Canadian knee implant devices. Combines regulatory database downloads (FDA GUDID, Health Canada MDALL), electronic Instructions for Use (eIFU) PDFs, and manual supplementation to produce an enriched CSV suitable for use as a synthetic dataset or clinical reference.

---

## Overview

```
FDA GUDID ──► GMDN Filter ──────────┐
                                     ├─► Merge ─► CJRR Filter ─► GUDID API Enrichment ─► eIFU Download (opt)
MDALL ──────► Keyword Filter ───────┘                                                    ─► eIFU Extraction
                                                                                          ─► NZ CSV Supplement
                                                                                          ─► Catalogue Size Fallback
                                                                                          ─► Export + Review Report
                                                                                          ─► enrich_reference_data.py (post)
```

The pipeline pulls device records from two regulatory sources, filters out non-implant instruments via GMDN terms (GUDID) and trade-name keywords (MDALL), merges on catalogue number, filters to CJRR-recognised manufacturers, enriches device descriptions via the GUDID REST API, extracts material compositions from eIFU PDFs, and flags records that still require manual data entry.

`enrich_reference_data.py` is a standalone post-processing step that enriches the library with AP/ML dimensions and material data from two supplemental Excel reference files.

---

## Quick Start

### Full pipeline (downloads everything from scratch)
```bash
python implant_lib_v3.py
```

### Start from an existing CJRR-filtered CSV (most common)
```bash
python implant_lib_v3.py --cjrr cjrr_filtered_20260314_160006.csv
```

### Start from a pre-merged CSV
```bash
python implant_lib_v3.py --merged merged_gudid_mdall.csv
```

### Skip all eIFU activity (fast run, no Selenium required)
```bash
python implant_lib_v3.py --cjrr cjrr_filtered.csv --no-eifus
```

### Download missing eIFUs automatically before enrichment
```bash
python implant_lib_v3.py --cjrr cjrr_filtered.csv --download-eifus
python implant_lib_v3.py --cjrr cjrr_filtered.csv --download-eifus --eifu-headless
```

### Skip GUDID API description enrichment (offline / fast run)
```bash
python implant_lib_v3.py --cjrr cjrr_filtered.csv --no-gudid-api
```

### Apply a filled review CSV back to the library
```bash
python apply_review.py \
        --library "outputs/knee_implant_library_*.csv" \
        --review   outputs/needs_review_filled.csv
```

### Enrich with AP/ML dimensions and material data from reference files
```bash
python enrich_reference_data.py --library "outputs/knee_implant_library_*.csv"
```

---

## Pipeline Stages

| # | Stage | Module / Class | Output |
|---|-------|----------------|--------|
| 1 | GUDID bulk download | `optimized_gudid_downloader.py` | `gudid_downloads/knee_implants_filtered_*.csv` |
| 1a | GMDN instrument filter | `KneeImplantPipeline._filter_gmdn_exclusions()` | Applied in-memory to GUDID DataFrame before merge |
| 2 | MDALL bulk download | `mdall_bulk_downloader.py` | `mdall_downloads/mdall_knee_devices_*.csv` |
| 2a | MDALL keyword exclusion filter | `MDALLBulkDownloader._is_instrument()` | Applied in-memory to MDALL records before/while downloading |
| 3 | Merge GUDID + MDALL | `merge_gudid_mdall.py` | `merged_gudid_mdall_*.csv` |
| 4 | CJRR manufacturer filter | `filter_cjrr.py` | `cjrr_filtered_*.csv` |
| 4c | GUDID API description enrichment | `gudid_description_enricher.py` | Adds `DEVICE_DESCRIPTION`, `DEVICE_SIZES_TEXT` columns |
| 5 | eIFU download (optional) | `eifu_downloader.py` | `implant_eIFUs/*.pdf` |
| 6 | NZ medical devices supplement | `CSVSupplementer` | Merged in-memory |
| 7 | eIFU material extraction | `eifu_material_extractor.py` | Used in-memory |
| 8 | Component classification | `ComponentTypeClassifier` | `component_type` field |
| 9 | Field extraction | `FieldExtractor` | All design/material fields |
| 9a | Catalogue-number size fallback | `catalogue_num_sizer.py` | `size`, `side`, `thickness`, `stability` fields |
| 10 | Field applicability matrix | `FieldApplicabilityMatrix` | Marks fields as N/A or missing |
| 11 | Export | `KneeImplantPipeline` | `outputs/knee_implant_library_*.csv`, `outputs/needs_review_*.csv` |
| — | Reference data enrichment (post) | `enrich_reference_data.py` | `outputs/knee_implant_library_enriched_*.csv` |

---

## CLI Reference

```
python implant_lib_v3.py [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--gudid PATH` | *(download)* | Path/glob to existing GUDID CSV — skips GUDID download |
| `--mdall PATH` | *(download)* | Path to existing MDALL CSV — skips MDALL download |
| `--merged PATH` | *(download+merge)* | Path to pre-merged GUDID+MDALL CSV — skips stages 1–3 |
| `--cjrr PATH` | *(download+merge+filter)* | Path to CJRR-filtered CSV — skips all earlier stages |
| `--csv PATH` | *(none)* | Path to NZ medical devices supplemental CSV |
| `--eifu-dir DIR` | `implant_eIFUs` | Directory containing eIFU PDFs |
| `--no-cjrr-filter` | | Skip CJRR manufacturer filter |
| `--no-gmdn-filter` | | Skip GMDN instrument filter |
| `--download-eifus` | | Automatically download missing eIFU PDFs (requires Selenium) |
| `--eifu-headless` | | Run eIFU browser in headless mode |
| `--no-eifus` | | Skip all eIFU activity — no download, no extraction |
| `--no-gudid-api` | | Skip GUDID deviceDescription API enrichment |
| `--gudid-desc-cache PATH` | `gudid_downloads/device_descriptions_cache.json` | GUDID description JSON cache file |
| `--output-dir DIR` | `outputs` | Output directory |

---

## Outputs

All outputs are written to the `outputs/` directory by default.

### `knee_implant_library_<timestamp>.csv`
The main enriched implant library. One row per catalogue number. See **Field Reference** below for column definitions.

### `needs_review_<timestamp>.csv`
A review report for records that could not be fully auto-populated. **One row per manufacturer / brand / component-type group** so that a single fill-in applies to all catalogue numbers in that group.

Columns:
- `manufacturer`, `brand_name`, `component_type` — group keys
- `catalogue_nums` — pipe-separated list of all catalogue numbers in the group
- `n_records` — number of catalogue numbers
- Editable field columns: `stability`, `fixation`, `bearing_type`, `metal_material`, `poly_material`, `material_standard`, `design_fixation_surface`, `design_articular_surface`, `antioxidant`, `surface_treatment`
  - Pre-populated with the consensus value if all records in the group already agree; otherwise left blank for manual entry

Fill in the blank fields and run `apply_review.py` to apply them back to the library.

### `knee_implant_library_enriched_<timestamp>.csv`
Output of `enrich_reference_data.py`. Adds or fills `ap_medial`, `ap_lateral`, `ml_width`, `thickness`, `diameter`, `metal_material`, `poly_material`, and `antioxidant` from two supplemental reference files.

### Intermediate files (not in `outputs/`)

| File | Produced by | Description |
|------|-------------|-------------|
| `gudid_downloads/knee_implants_filtered_*.csv` | `optimized_gudid_downloader.py` | Raw GUDID device records for knee-implant product codes |
| `gudid_downloads/device_descriptions_cache.json` | `gudid_description_enricher.py` | Local cache of GUDID API `deviceDescription` responses |
| `mdall_downloads/mdall_knee_devices_*.csv` | `mdall_bulk_downloader.py` | Raw MDALL device records |
| `merged_gudid_mdall_*.csv` | `merge_gudid_mdall.py` | GUDID + MDALL joined on catalogue number |
| `cjrr_filtered_*.csv` | `filter_cjrr.py` | Subset of merged records matching CJRR manufacturers |
| `implant_eIFUs/*.pdf` | `eifu_downloader.py` | Downloaded eIFU PDFs |

---

## Field Reference

### Identifiers

| Field | Source | Description |
|-------|--------|-------------|
| `catalogue_num` | GUDID / MDALL | VERSION_MODEL_NUMBER (canonical) |
| `device_id` | GUDID | Device identifier (DI / GTIN-14 or similar) |
| `gtin` | GUDID | Global Trade Item Number |
| `mdall_licence_number` | MDALL | Health Canada licence number |
| `mdall_device_id` | MDALL | MDALL internal device identifier |

### Basic Info

| Field | Source | Description |
|-------|--------|-------------|
| `brand_name` | GUDID (MDALL fallback) | Commercial brand name |
| `mdall_brand_name` | MDALL | Raw MDALL device name — often richer than GUDID (includes CR/PS/fixation info) |
| `company_name` | GUDID / MDALL | Manufacturer company name |
| `manufacturer` | CJRR mapping | CJRR canonical manufacturer name |
| `product_code` | GUDID | FDA 3-letter product code (e.g. JWH, MBH) |
| `device_name` | GUDID | Product code description (e.g. "Knee Joint Femoral Component") |
| `device_description` | GUDID REST API | Per-device free-text description (e.g. "Triathlon CR Femoral Component, Size 3") |
| `device_sizes_text` | GUDID REST API | Size annotations from GUDID (e.g. "Width: 60 mm \| Height: 9 mm") |
| `gmdn_name` | GUDID / MDALL | GMDN term |

### Component Classification

| Field | Values | Description |
|-------|--------|-------------|
| `component_type` | Femoral, Tibial, Insert, Patellar, TKA System, Femoral Stem, Tibial Stem, Stem | Classified from GMDN name |
| `replacement_type` | P, R | P = Primary, R = Revision |

### Design Characteristics

| Field | Applicable to | Values / Notes |
|-------|--------------|----------------|
| `fixation` | Femoral, Tibial, TKA System | Cemented, Cementless, Hybrid |
| `stability` | Femoral, Insert, TKA System | CR, PS, CPS, BCS, Semi-Constrained, Constrained |
| `bearing_type` | Insert | Fixed Bearing, Mobile Bearing, Rotating Platform, Medial Pivot |
| `side` | all | Left, Right, Universal |
| `size` | all | Numeric or letter code (e.g. "3", "D", "AB 1-2") |
| `surface_treatment` | Femoral, Tibial | Fixation-surface coating: HA, TiN, TPS, CaP, PMMA. PA/Peri-Apatite normalised to HA. |

### Materials

| Field | Applicable to | Values / Notes |
|-------|--------------|----------------|
| `metal_material` | Femoral, Tibial, Patellar | CoCr, Titanium, OxZr |
| `poly_material` | Insert, Patellar | UHMWPE, XLPE, UHMWPE+VitE, XLPE+VitE |
| `ceramic_material` | Femoral | e.g. ZrO2 |
| `antioxidant` | Insert | "Vitamin E" — populated whenever `poly_material` contains VitE (e.g. Vivacit-E products) |
| `material_standard` | all | ISO/ASTM standard reference |

### Dimensions

| Field | Applicable to | Description |
|-------|--------------|-------------|
| `ap_medial` | Femoral, Tibial, Monoblock Tibial | Anteroposterior depth on the medial side (mm) |
| `ap_lateral` | Femoral, Tibial, Monoblock Tibial | Anteroposterior depth on the lateral side (mm). Equal to `ap_medial` for symmetric components; differs for asymmetric tibial baseplates (e.g. Persona Keeled). |
| `ml_width` | Tibial, Monoblock Tibial | Mediolateral width (mm) |
| `thickness` | Insert | Polyethylene thickness (mm) |
| `diameter` | Patellar, Stems | Diameter (mm) |

### Design Features

| Field | Applicable to | Description |
|-------|--------------|-------------|
| `design_articular_surface` | Femoral, Insert | Description of articulating surface geometry |
| `design_fixation_surface` | Femoral, Tibial, Patellar | Fixation surface description (keel, pegs, cruciate, etc.) |

### Source / Metadata

| Field | Description |
|-------|-------------|
| `data_source` | List of sources contributing to this record (GUDID, MDALL, eIFU, CSV, ZimmerOrtech, TKADims) |
| `match_type` | How GUDID and MDALL were matched: exact, normalized, unmatched |
| `source_db` | BOTH, GUDID_ONLY, MDALL_ONLY |
| `confidence_score` | Fraction of applicable fields that are populated (0–1) |
| `needs_review` | True if any applicable field is missing |
| `field_status` | Per-field status dict: present, missing, not_applicable |
| `notes` | Free-text notes |

---

## Module Reference

### `implant_lib_v3.py` — Pipeline Orchestrator
Main entry point. Coordinates all pipeline stages. Contains:
- `ImplantRecord` — dataclass holding all fields for one catalogue number
- `ComponentTypeClassifier` — classifies component type from GMDN term
- `FieldExtractor` — regex-based extraction of fixation, stability, bearing type, materials, surface treatment, size, side
- `FieldApplicabilityMatrix` — marks fields as N/A or missing based on component type
- `KneeImplantPipeline` — orchestrates stages 1–11
- `CSVSupplementer` — merges NZ medical devices CSV data

### `enrich_reference_data.py` — Reference Data Enricher
Post-processing script that enriches a library CSV with AP/ML dimensions and material data from two supplemental Excel files in the project root.

**Sources:**
- `ZimmerOrtechImplants.xlsx` — Ortech procedure records; provides AP/ML (tibial), metal material (femoral NexGen), thickness and poly material (bearings). Zimmer NexGen and Persona only.
- `TKA Model Info With Dimensions.xlsx` — dimension lookup table by model/component/size; covers Stryker, DePuy, Smith & Nephew, and Zimmer/Biomet.

**Fields filled:** `ap_medial`, `ap_lateral`, `ml_width`, `thickness`, `diameter`, `metal_material`, `poly_material`, `antioxidant`.

Never overwrites existing non-blank values. Fills `ap_lateral = ap_medial` for symmetric components; fills them independently for asymmetric components (Persona Keeled Tibial Baseplate).

Reads `ap_length` from older library CSVs and renames it automatically.

```bash
# Default (uses both sources from project root)
python enrich_reference_data.py --library "outputs/knee_implant_library_*.csv"

# Custom paths
python enrich_reference_data.py \
    --library "outputs/knee_implant_library_*.csv" \
    --zimmer-ortech ZimmerOrtechImplants.xlsx \
    --tka-dims "TKA Model Info With Dimensions.xlsx"

# Skip one source
python enrich_reference_data.py --library "outputs/..." --no-zimmer-ortech
python enrich_reference_data.py --library "outputs/..." --no-tka-dims
```

### `merge_gudid_mdall.py` — GUDID + MDALL Merger
Merges the two source DataFrames on `VERSION_MODEL_NUMBER` using:
1. Exact match (after uppercase + strip)
2. Normalized match (after also removing hyphens and spaces)

Output columns include `SOURCE` (BOTH / GUDID_ONLY / MDALL_ONLY), `MATCH_TYPE` (exact / normalized / unmatched), and `MDALL_BRAND_NAME` (the raw MDALL device name, preserved separately before coalescing with the GUDID brand name).

```bash
python merge_gudid_mdall.py \
        --gudid "gudid_downloads/knee_implants_filtered_*.csv" \
        --mdall "mdall_downloads/mdall_knee_devices_*.csv" \
        --output merged_gudid_mdall.csv
```

### `gudid_description_enricher.py` — GUDID API Description Enrichment
Fetches `deviceDescription` and `deviceSizes` from the FDA GUDID REST API for each unique Device Identifier (DI). Results are cached locally in a JSON file so only new DIs trigger API calls.

- API endpoint: `https://accessgudid.nlm.nih.gov/api/v3/devices/lookup.json?di={di}`
- Default cache: `gudid_downloads/device_descriptions_cache.json`
- Cache is saved every 100 fetches; 404 responses are cached as empty to avoid re-fetching
- Adds `DEVICE_DESCRIPTION` and `DEVICE_SIZES_TEXT` columns

```bash
python gudid_description_enricher.py \
        --input   outputs/cjrr_filtered.csv \
        --output  outputs/cjrr_filtered_enriched.csv \
        --dry-run
```

### `catalogue_num_sizer.py` — Catalogue Number Size Extractor
Fallback size extraction using manufacturer-specific catalogue number conventions. Called automatically in the enrichment stage when `size`, `side`, `thickness`, or `stability` could not be determined from free text.

See [`docs/catalogue_num_sizer_rules.md`](docs/catalogue_num_sizer_rules.md) for the full per-manufacturer/system rule reference (every regex, lookup table, and decoding rule).

Supported manufacturers and systems:

| Manufacturer | System | Fields decoded |
|-------------|--------|----------------|
| Zimmer | Persona | size, side, thickness, stability (insert) |
| Zimmer | NexGen | size, side, thickness |
| Stryker | All (F/B/G/P letter codes) | size, side, thickness |
| MicroPort / Wright | Femoral, Tibial | size, side |
| DePuy | ATTUNE | size, side, thickness |
| DePuy | Sigma | size, side |
| DePuy | PFC Sigma | size, side |
| Smith & Nephew | Femoral | size |

```python
from catalogue_num_sizer import extract_from_catalogue
result = extract_from_catalogue(
        catalogue_num="42-5050-065-09",
        manufacturer="Zimmer",
        component_type="Insert",
        brand_name="Persona",
)
# => {"size": "1", "side": None, "thickness": "9", "stability": "CR"}
```

### `apply_review.py` — Apply Filled Review CSV
Reads a filled-in `needs_review` CSV and applies the manually entered field values back to the library. Matches on `manufacturer + brand_name + component_type`.

- By default only fills blank/missing fields
- Use `--overwrite` to also replace existing non-blank values
- Supports glob patterns for the library path (picks most recently modified match)

```bash
# Typical usage after filling in needs_review CSV:
python apply_review.py \
        --library "outputs/knee_implant_library_*.csv" \
        --review   outputs/needs_review_filled.csv

# Overwrite existing values too:
python apply_review.py \
        --library outputs/knee_implant_library_20260314_160034.csv \
        --review   outputs/needs_review_filled.csv \
        --overwrite

# Explicit output path:
python apply_review.py \
        --library outputs/knee_implant_library_20260314_160034.csv \
        --review   outputs/needs_review_filled.csv \
        --output   outputs/knee_implant_library_updated.csv
```

### `optimized_gudid_downloader.py` — GUDID Bulk Downloader
Downloads device records from the FDA GUDID for knee-implant product codes (JWH, MBH, etc.). Uses the GUDID bulk download API. Output saved to `gudid_downloads/`.

### `mdall_bulk_downloader.py` — MDALL Bulk Downloader
Downloads device records from the Health Canada Medical Devices Active Licence Listing (MDALL) for knee implant search terms. Output saved to `mdall_downloads/`.

Applies a keyword exclusion filter (`_is_instrument()` against `EXCLUDE_KEYWORDS`) to trade names — see [MDALL Keyword Exclusion Filter](#mdall-keyword-exclusion-filter) below.

### `filter_cjrr.py` — CJRR Manufacturer Filter
Filters the merged dataset to records belonging to manufacturers recognised by the Canadian Joint Replacement Registry (CJRR). Output saved as `cjrr_filtered_*.csv`.

### `eifu_downloader.py` — eIFU PDF Downloader
Downloads electronic Instructions for Use (eIFU) PDFs for supported implant systems. Uses Selenium to navigate manufacturer eIFU portals. PDFs are saved to `implant_eIFUs/` using the naming convention:
```
{manufacturer}_{system_name}_{doc_id}.pdf
```
Example: `stryker_triathlon_QIN4396EIFU.pdf`

Checks for existing PDFs before downloading — will not re-download a file already present.

### `eifu_material_extractor.py` — eIFU Material Extractor
Extracts material composition information from eIFU PDFs. Routes to manufacturer-specific parsers based on the filename prefix. One PDF may cover many catalogue numbers. Handles multiple eIFU variants for the same system (e.g. ATTUNE Fixed Bearing vs ATTUNE Cementless).

### Other modules

| Module | Description |
|--------|-------------|
| `get_fda_product_codes.py` | Fetches FDA product code descriptions |
| `CJRR_manual_lib_add_implant_fields.py` | Adds CJRR-specific fields to manual library entries |

---

## GMDN Exclusion Filter

Before merging with MDALL, GUDID records whose GMDN term matches any of the following categories are excluded to remove instruments and non-implant devices:

- **Surgical instruments**: mallet, drill bit, punch, broach, rasp, reamer, chisel, osteotome, curette, forceps, clamp, retractor, hook, file, cutter, impactor
- **Implant trials**: trial component, trial insert, trial femoral, trial tibial
- **Revision hardware**: rod, stem extension, sleeve, augment, wedge, sidebar
- **Non-knee implants**: hip prosthesis, acetabular, femoral stem (hip context)
- **Fixation hardware**: screw driver, screwdriver, tap, pin, wire
- **Packaging / kits**: instrument kit, surgical kit, splint

A safety-net pass of the same filter is also applied to `base_df` when entering the pipeline from `--merged` or `--cjrr` (where the GUDID download step is skipped).

---

## MDALL Keyword Exclusion Filter

MDALL has no GMDN-equivalent category field, so `mdall_bulk_downloader.py` filters out non-implant devices by matching trade names against a flat keyword list (`EXCLUDE_KEYWORDS` in `mdall_bulk_downloader.py`) instead. `MDALLBulkDownloader._is_instrument(trade_name)` lowercases the trade name and returns `True` if any keyword is a substring match.

Applied in two places:
- **`download_by_name()`** — filters raw search results immediately after each MDALL query, before rows are built
- **`_filter_gmdn_style()`** — a safety-net pass over the `BRAND_NAME` column, mirroring the GUDID GMDN filter, for cases where MDALL data enters the pipeline pre-downloaded

Keyword categories in `EXCLUDE_KEYWORDS`:
- **Instruments / tools**: instrument, drill, saw, rasp, reamer, impactor, extractor, inserter, retractor, forceps, clamp, holder, handle, driver, wrench, stylus, tibial stylus, adapter, adaptor, connector, coupler, distractor, exerciser
- **Trial components**: trial
- **Fixation hardware**: screw, pin, wire, bolt
- **Guides / blocks**: guide, template, positioning, alignment, alignment handle, cutting, cutting block, cut block, resection, resection block, flexion block, slope block, var-val block, a/p shift block, planer, angle wing
- **Cement / consumable supplies**: cement, mixing, dispenser, spatula, packing, fill tips
- **Packaging**: tray, case, container, kit, set, system pack, packaging
- **Soft-tissue balancing**: balancer, spacer, tensor, spreader, wedge, cone
- **Documentation**: surgical technique, op tech, optech, surgical protocol, examination
- **Unrelated medical devices / anatomy** (broad net catching non-knee, non-orthopedic device names): syringe, catheter, suction, ultrasound, aortic, electrodes, penile, biliary, inhaler, plasma, saline, urinary, cardiopledgia, iliac, endotracheal, anesth, hoses, valve, antibacterial, gloves, exercise, transmitter, hearing aid, substrate, "ai"

**Note**: `"ai"` and `"set"` are broad substring matches — they will also match inside unrelated words (e.g. "ai" matches "maintenance", "set" matches "reset" or "offset"). This is a known tradeoff for catching abbreviations like AI-coated liners; review MDALL output for false-positive exclusions if a brand name unexpectedly disappears.

---

## eIFU File Naming Convention

eIFU PDFs in `implant_eIFUs/` follow this naming pattern:

```
{manufacturer}_{system_name}_{doc_id}.pdf
```

The manufacturer prefix routes to the correct parser in `eifu_material_extractor.py`. Where a system has multiple eIFU variants (e.g. ATTUNE cementless vs fixed bearing), the variant is encoded in the filename and detected via `_detect_eifu_variant()`.

---

## Manual Review Workflow

1. Run the pipeline to generate `outputs/needs_review_<timestamp>.csv`
2. Open the CSV in Excel or a text editor
3. Fill in the blank editable-field columns for each group row
   - Each row represents all catalogue numbers for a manufacturer / brand / component combination
   - The `catalogue_nums` column shows which specific catalogue numbers are in the group
   - Fields already consistent across the group are pre-filled; conflicting or missing fields are blank
4. Save the filled CSV
5. Run `apply_review.py` to apply your entries back to the library:
   ```bash
   python apply_review.py \
           --library "outputs/knee_implant_library_*.csv" \
           --review   outputs/needs_review_filled.csv
   ```
6. A new `knee_implant_library_reviewed_<timestamp>.csv` is written to the same directory as the library

---

## Dependencies

```
pandas
requests
selenium                # only for --download-eifus
openpyxl                # for reading .xlsx files (enrich_reference_data.py)
pdfplumber              # for eIFU PDF text extraction
playwright              # for GUDID ES export fallback (small product codes)
```

Install with:
```bash
pip install pandas requests selenium openpyxl pdfplumber playwright
playwright install chromium
```

For `--download-eifus`, a compatible WebDriver (e.g. ChromeDriver) must also be installed and on your PATH.
