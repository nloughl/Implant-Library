"""
eIFU Material Extractor
=======================

Extracts material composition data from knee implant eIFU PDFs.

Manufacturer-specific parsers handle the different document structures:

  Stryker
    Section:  "Materials"
    Structure: ComponentName (XXXX-Y-XXX)
               Material Description (e.g. Cast Co-Cr-Mo / ASTM F75)
               Element  lo – hi     (one row per substance)
               Element  ≤max
    One catalogue-number family per component type.

  Zimmer (Persona, NexGen, etc.)
    Section:  "Materials and Compositions"
    Structure: Three-column table: Component | Material(s) | Composition
               Component labels:  Femoral (nonporous), Tibial (porous), etc.
               Compositions:      "Element: value%" one per line
    No catalogue numbers — identified by component type.

  DePuy (ATTUNE, Sigma PFC, etc.)
    Section:  "MATERIAL DESCRIPTION" or "MATERIALS"
    Structure A (simple): MaterialCode | Material Description
    Structure B (product table): ProductName | MaterialCode | Description | %
    No detailed elemental compositions.

Output
------
Each parser returns a list of dicts:

  {
    'source_file':        str,
    'manufacturer':       str,
    'system':             str,
    'component_type':     'femoral' | 'tibial_baseplate' | 'tibial_insert'
                          | 'patellar' | 'stem' | 'other',
    'component_label':    str,          # raw label from document
    'catalogue_family':   str | None,   # Stryker only, e.g. '5510-F-XXX'
    'material_name':      str,          # e.g. 'Cast Co-Cr-Mo', 'Ti-6Al-4V', 'UHMWPE'
    'alloy_standard':     str | None,   # e.g. 'ASTM F75'
    'composition':        dict,         # {element: 'lo–hi%' or '≤max%' or 'value%'}
  }

Usage
-----
  from eifu_material_extractor import extract_materials
  import pandas as pd

  rows = extract_materials('implant_eIFUs/Stryker_triathlon_eIFU.pdf')
  df = pd.DataFrame(rows)
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Standardise unicode characters that cause trouble in PDF-extracted text."""
    return (
        text
        .replace('\u2011', '-')   # non-breaking hyphen → regular hyphen
        .replace('\u2010', '-')   # hyphen
        .replace('\u2012', '-')   # figure dash
        .replace('\u00ae', '')    # ®
        .replace('\u2122', '')    # ™
        .replace('\u2019', "'")   # right single quote
        .replace('\u2018', "'")   # left single quote
        .replace('\u00b0', '')    # degree sign
        # Keep en-dash (–) as the range separator
    )


# ---------------------------------------------------------------------------
# PDF reading
# ---------------------------------------------------------------------------

def _read_pdf(filepath: Path) -> str:
    import pypdf
    reader = pypdf.PdfReader(str(filepath))
    pages = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            pages.append(t)
    text = '\n'.join(pages)
    logger.info('Read %s (%d pages, %d chars)', filepath.name, len(reader.pages), len(text))
    return text


# ---------------------------------------------------------------------------
# Filename-based manufacturer / system detection
# ---------------------------------------------------------------------------

def _detect_manufacturer(filepath: Path) -> str:
    name = filepath.name.lower()
    if 'stryker' in name:
        return 'Stryker'
    if any(k in name for k in ('zimmer', 'persona', 'nexgen', 'vanguard',
                                'gender', 'agc', 'nex_gen')):
        return 'Zimmer'
    if any(k in name for k in ('depuy', 'attune', 'sigma', 'pfc')):
        return 'DePuy'
    return 'Unknown'


def _detect_variant(filepath: Path) -> str:
    """
    Detect fixation / bearing variant from the eIFU filename.
    Returns a normalised lowercase tag, or '' if none detected.

    Used to build variant-specific lookup keys so that, e.g.,
    Attune cementless and Attune fixed-bearing map to different entries.
    """
    stem = filepath.stem.lower().replace('-', '_')
    if 'cementless' in stem or 'porocoat' in stem:
        return 'cementless'
    if 'porous' in stem:
        return 'cementless'   # porous fixation surface = cementless
    if 'cemented' in stem:
        return 'cemented'
    if 'mobile' in stem or 'rotating_bearing' in stem:
        return 'mobile'
    if 'fixed_bearing' in stem or 'fixed_bear' in stem:
        return 'fixed'
    return ''


def _detect_system(filepath: Path) -> str:
    stem = filepath.stem.lower()
    SYSTEMS = [
        'triathlon', 'scorpio', 'duracon', 'kinemax',
        'persona', 'nexgen', 'nex_gen', 'vanguard', 'gender_solutions', 'agc',
        'attune', 'sigma', 'pfc', 'mrh',
    ]
    for kw in SYSTEMS:
        if kw.replace('_', '') in stem.replace('_', ''):
            return kw.replace('_', ' ').title()
    return filepath.stem.replace('_', ' ').title()


# ---------------------------------------------------------------------------
# Component-type classifier
# ---------------------------------------------------------------------------

def _classify_component(label: str) -> str:
    l = label.lower()
    if any(k in l for k in ('patellar', 'patella')):
        return 'patellar'
    if any(k in l for k in ('tibial insert', 'tibial poly', 'insert', 'articular surface')):
        return 'tibial_insert'
    if any(k in l for k in ('tibial baseplate', 'tibial tray', 'baseplate', 'tibial base')):
        return 'tibial_baseplate'
    if 'tibial' in l:
        # If label has "insert" or is an articular surface it's an insert; default baseplate
        return 'tibial_baseplate'
    if 'femoral' in l:
        return 'femoral'
    if 'stem' in l:
        return 'stem'
    if 'augment' in l:
        return 'augment'
    return 'other'


# ---------------------------------------------------------------------------
# Stryker parser
# ---------------------------------------------------------------------------

_STR_COMPONENT_RE = re.compile(
    r'^(.+?)\s*\((\d{4}-[A-Z]-XXX)\)\s*$'
)
_STR_MATERIAL_RE = re.compile(
    r'(Cast\s+Co-Cr-Mo|Wrought\s+Co-Cr-Mo|Ti-6Al-4V|UHMWPE'
    r'|Ultra-High\s+Molecular\s+Weight\s+Polyethylene'
    r'|Hydroxyapatite|Peri-Apatite|Oxidized\s+Zirconium'
    r'|CoCr|Cobalt\s+Chrome)',
    re.IGNORECASE,
)
_STR_ASTM_RE    = re.compile(r'ASTM\s+F\d+')
_STR_RANGE_RE   = re.compile(
    r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+([\d.]+)\s*[–\-]\s*([\d.]+)'
)
_STR_MAX_RE     = re.compile(
    r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[≤≦<]\s*([\d.]+)'
)
_STR_HEADER_WORDS = re.compile(
    r'^(Description|Substances|Materials|Increments|Weight|Concentration|Range|w/w)\b',
    re.IGNORECASE,
)


def parse_stryker(text: str, source_file: str) -> List[Dict]:
    """Parse Stryker eIFU materials section."""
    text = _normalize(text)
    lines = text.split('\n')
    system = _detect_system(Path(source_file))

    # Locate "Materials" section header
    mat_start = 0
    for i, line in enumerate(lines):
        if re.match(r'^\s*Materials?\s*$', line, re.IGNORECASE):
            mat_start = i
            break

    records: List[Dict] = []

    # State
    cur_label: Optional[str] = None
    cur_family: Optional[str] = None
    cur_material: Optional[str] = None
    cur_standard: Optional[str] = None
    cur_comp: Dict[str, str] = {}

    def _flush():
        if cur_label and cur_material:
            records.append({
                'source_file':     source_file,
                'manufacturer':    'Stryker',
                'system':          system,
                'component_type':  _classify_component(cur_label),
                'component_label': cur_label,
                'catalogue_family': cur_family,
                'material_name':   cur_material,
                'alloy_standard':  cur_standard,
                'composition':     dict(cur_comp),
            })

    for line in lines[mat_start:]:
        stripped = line.strip()
        if not stripped:
            continue

        # New component header: "ComponentName (XXXX-Y-XXX)"
        m = _STR_COMPONENT_RE.match(stripped)
        if m:
            _flush()
            cur_label    = m.group(1).strip()
            # Skip non-English component labels (foreign language sections)
            if not cur_label.isascii():
                cur_label = None
                continue
            cur_family   = m.group(2)
            cur_material = None
            cur_standard = None
            cur_comp     = {}
            continue

        if cur_label is None:
            continue

        # Skip table header words
        if _STR_HEADER_WORDS.match(stripped):
            continue

        # Material description
        mat_m = _STR_MATERIAL_RE.search(stripped)
        if mat_m and cur_material is None:
            cur_material = mat_m.group(0).strip()

        # ASTM standard
        std_m = _STR_ASTM_RE.search(stripped)
        if std_m and cur_standard is None:
            cur_standard = std_m.group(0)

        # Substance with range: "Chromium 27.00 – 30.00 ..."
        rng = _STR_RANGE_RE.match(stripped)
        if rng:
            cur_comp[rng.group(1)] = f"{rng.group(2)}–{rng.group(3)}%"
            continue

        # Substance with max: "Nickel ≤0.50"
        mx = _STR_MAX_RE.match(stripped)
        if mx:
            cur_comp[mx.group(1)] = f"≤{mx.group(2)}%"

    _flush()
    return records


# ---------------------------------------------------------------------------
# Zimmer parser
# ---------------------------------------------------------------------------

# Known component anchors in Zimmer "Materials and Compositions" table.
# Each is (component_type_keyword, variant_keyword).
# We match by finding these words in close proximity across lines.
_ZIMMER_ANCHORS: List[Tuple[str, str]] = [
    ('Femoral',          'nonporous'),
    ('Femoral',          'porous'),
    ('Tibial',           'nonporous'),
    ('Tibial',           'porous'),
    ('Articular Surface', 'Vitamin'),
    ('Articular Surface', 'UHMWPE'),
    ('Patella',          'Vitamin'),
    ('Patella',          'UHMWPE'),
    ('Stem Extension',   ''),
]

# Known Zimmer material names (handles broken lines from PDF extraction)
_ZIMMER_MATERIAL_KEYWORDS = {
    'Co-Cr-Mo Alloy':  re.compile(
        r'Co.{0,5}Cr.{0,5}Mo.{0,10}Alloy'
        r'|C\s?o\s*-?\s*C\s?h?r\s*-?\s*M\s?o'
        r'|Cobalt.{0,50}C\s?hromium.{0,50}Molybdenum'
        r'|Zimaloy',
        re.IGNORECASE | re.DOTALL,
    ),
    'Ti-6Al-4V Alloy': re.compile(
        r'Titanium.{0,40}Aluminum.{0,40}Vanadium.{0,20}Alloy'
        r'|Ti.{0,5}6Al.{0,5}4V',
        re.IGNORECASE | re.DOTALL,
    ),
    'UHMWPE':          re.compile(r'Ultra.{0,10}High\s+Molecular.{0,20}Polyethylene'
                                  r'|\bUHMWPE\b', re.IGNORECASE),
    'VEHXPE':          re.compile(r'Vitamin.{0,5}E.{0,30}Stabilized.{0,30}Highly.{0,30}Cross'
                                  r'|\bVEHXPE\b', re.IGNORECASE | re.DOTALL),
    'Trabecular Metal': re.compile(r'Trabecular\s+Metal', re.IGNORECASE),
    'Titanium':         re.compile(r'^Titanium\s*$', re.MULTILINE),
}

_ZIMMER_COMP_LINE_RE = re.compile(
    r'^([A-Z][A-Za-z\s\-]{1,30}?):\s+(.+)$'
)


def _find_zimmer_anchor(chunk: str) -> Optional[Tuple[str, str]]:
    """Return (type, variant) if chunk contains one of the known component anchors."""
    for comp_type, variant in _ZIMMER_ANCHORS:
        if comp_type.lower() in chunk.lower():
            if not variant or variant.lower() in chunk.lower():
                return comp_type, variant
    return None


def parse_zimmer(text: str, source_file: str) -> List[Dict]:
    """Parse Zimmer eIFU 'Materials and Compositions' table."""
    text = _normalize(text)
    system = _detect_system(Path(source_file))

    sec = re.search(r'Materials\s+and\s+Compositions', text, re.IGNORECASE)
    if not sec:
        logger.warning('Zimmer: "Materials and Compositions" not found in %s', source_file)
        return []

    section = text[sec.start():]

    # Split section into component blocks by finding anchor positions
    # Strategy: scan lines, reconstruct 3-line windows to detect anchors
    lines = section.split('\n')
    anchor_positions: List[Tuple[int, str, str]] = []  # (line_idx, type, variant)

    window = 3  # lines to join when looking for "Femoral\n(nonporous)"
    for i in range(len(lines)):
        chunk = ' '.join(lines[i:i+window])
        anchor = _find_zimmer_anchor(chunk)
        if anchor:
            # Avoid duplicate detections of same anchor
            if not anchor_positions or anchor_positions[-1][0] < i - 1:
                anchor_positions.append((i, anchor[0], anchor[1]))

    if not anchor_positions:
        logger.warning('Zimmer: no component anchors found in %s', source_file)
        return []

    records: List[Dict] = []

    for idx, (line_i, comp_type, comp_variant) in enumerate(anchor_positions):
        # Block ends at next anchor or end of lines
        end_i = anchor_positions[idx + 1][0] if idx + 1 < len(anchor_positions) else len(lines)
        block_lines = lines[line_i:end_i]
        block_text = '\n'.join(block_lines)

        label = f"{comp_type} ({comp_variant})" if comp_variant else comp_type

        # Detect materials present in this block
        found_materials: List[str] = []
        for mat_name, pattern in _ZIMMER_MATERIAL_KEYWORDS.items():
            if pattern.search(block_text):
                found_materials.append(mat_name)

        # Extract composition lines: "Element: value"
        composition: Dict[str, str] = {}
        for bl in block_lines:
            m = _ZIMMER_COMP_LINE_RE.match(bl.strip())
            if m:
                element = m.group(1).strip()
                value   = m.group(2).strip().rstrip('*').rstrip('**')
                if element not in ('Component', 'Material', 'Material Composition',
                                   'Range or Maximum'):
                    composition[element] = value

        if not found_materials:
            found_materials = ['Unknown']

        comp_type_classified = _classify_component(label)

        for mat in found_materials:
            records.append({
                'source_file':     source_file,
                'manufacturer':    'Zimmer',
                'system':          system,
                'component_type':  comp_type_classified,
                'component_label': label,
                'catalogue_family': None,
                'material_name':   mat,
                'alloy_standard':  None,
                'composition':     composition,
            })

    return records


# ---------------------------------------------------------------------------
# DePuy parser
# ---------------------------------------------------------------------------

_DEPUY_MATCODE_RE  = re.compile(r'^([A-Z]\d)\s+([\w\s\-]+(?:Alloy|Polyethylene|UHMWPE|Ceramic)?)')
_DEPUY_PRODUCT_RE  = re.compile(r'^(ATTUNE\S*|SIGMA\S*|P\.F\.C\S*)\s+(.+)')
_DEPUY_COMP_RE     = re.compile(
    r'(Cobalt|Chromium|Molybdenum|Nickel|Iron|Carbon|Silicon|Manganese)'
    r'\s*\(?\s*([\d]+)\s*%\)?',
    re.IGNORECASE,
)


def parse_depuy(text: str, source_file: str) -> List[Dict]:
    """Parse DePuy eIFU material section."""
    text = _normalize(text)
    system = _detect_system(Path(source_file))

    # Find material section (may be "MATERIAL DESCRIPTION" or "MATERIALS")
    sec = re.search(r'MATERIALS?\s+DESCRIPTION|^MATERIALS\s*$', text,
                    re.IGNORECASE | re.MULTILINE)
    if not sec:
        logger.warning('DePuy: material section not found in %s', source_file)
        return []

    section = text[sec.start():]
    lines   = section.split('\n')

    # --- Step 1: build material-code → description mapping ---
    mat_map: Dict[str, str] = {}
    for line in lines[:30]:
        m = _DEPUY_MATCODE_RE.match(line.strip())
        if m:
            mat_map[m.group(1)] = m.group(2).strip()

    # --- Step 2: extract nominal composition from C1 block (if present) ---
    composition: Dict[str, str] = {}
    for i, line in enumerate(lines[:30]):
        for cm in _DEPUY_COMP_RE.finditer(line):
            composition[cm.group(1).capitalize()] = f"{cm.group(2)}%"
    # Also handle "Less than 1%" trace elements
    for line in lines[:30]:
        if 'less than 1%' in line.lower() or 'less than 1 %' in line.lower():
            for elem in ('Nickel', 'Iron', 'Carbon', 'Silicon', 'Manganese'):
                if elem.lower() in line.lower() or (
                    lines[min(lines.index(line)+1, len(lines)-1)] and
                    elem.lower() in lines[lines.index(line)+1].lower()
                ):
                    composition.setdefault(elem, '<1%')

    # --- Step 3: find product name → material code table ---
    records: List[Dict] = []
    seen_labels: set = set()

    in_product_table = False
    for line in lines:
        stripped = line.strip()

        # Detect start of product table
        if re.search(r'\bName\b.*\bMaterial\b', stripped, re.IGNORECASE):
            in_product_table = True
            continue

        if in_product_table and stripped.startswith('ATTUNE'):
            # Parse product row — may span multiple lines; grab what we can
            parts = stripped.split()
            mat_code = None
            # Look for a material code (C1, T1, P2, RS, etc.) in this or next line
            for i_tok, tok in enumerate(parts):
                if re.match(r'^[CPT]\d$', tok):
                    mat_code = tok
                    break

            # Component type from product name
            label = stripped
            mat_name = mat_map.get(mat_code, mat_code or 'Unknown') if mat_code else 'Unknown'
            comp_type = _classify_component(label)

            if label not in seen_labels:
                seen_labels.add(label)
                records.append({
                    'source_file':     source_file,
                    'manufacturer':    'DePuy',
                    'system':          system,
                    'component_type':  comp_type,
                    'component_label': label,
                    'catalogue_family': None,
                    'material_name':   mat_name,
                    'alloy_standard':  None,
                    'composition':     composition if mat_code and mat_code.startswith('C') else {},
                })

        # Stop at warnings / how supplied section
        if re.match(r'^(HOW SUPPLIED|WARNINGS|INTENDED USE)', stripped, re.IGNORECASE):
            break

    # If no product table found, create one record per material code
    if not records:
        for code, name in mat_map.items():
            # Map material code prefix to component type
            if code.startswith('C'):
                comp = 'femoral'      # CoCr → femoral/tibial metal
            elif code.startswith('T'):
                comp = 'tibial_baseplate'
            elif code.startswith('P'):
                comp = 'tibial_insert'
            else:
                comp = 'other'
            records.append({
                'source_file':     source_file,
                'manufacturer':    'DePuy',
                'system':          system,
                'component_type':  comp,
                'component_label': code,
                'catalogue_family': None,
                'material_name':   name,
                'alloy_standard':  None,
                'composition':     composition if code.startswith('C') else {},
            })

    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_materials(filepath) -> List[Dict]:
    """
    Extract material records from an eIFU PDF.

    Parameters
    ----------
    filepath : str or Path

    Returns
    -------
    list of dict  (see module docstring for field definitions)
    """
    filepath = Path(filepath)
    text = _read_pdf(filepath)
    mfr = _detect_manufacturer(filepath)

    if mfr == 'Stryker':
        return parse_stryker(text, str(filepath))
    elif mfr == 'Zimmer':
        return parse_zimmer(text, str(filepath))
    elif mfr == 'DePuy':
        return parse_depuy(text, str(filepath))
    else:
        # Try all parsers, return whichever yields results
        for parser in (parse_stryker, parse_zimmer, parse_depuy):
            rows = parser(text, str(filepath))
            if rows:
                return rows
        logger.warning('No material data extracted from %s', filepath.name)
        return []


def extract_all(eifu_dir: str = 'implant_eIFUs') -> 'pd.DataFrame':
    """
    Extract material data from all PDFs in eifu_dir and return a DataFrame.

    Usage
    -----
    >>> df = extract_all('implant_eIFUs')
    >>> print(df.groupby(['manufacturer', 'system', 'component_type'])['material_name'].first())
    """
    import pandas as pd

    rows = []
    for pdf in sorted(Path(eifu_dir).glob('*.pdf')):
        try:
            rows.extend(extract_materials(pdf))
        except Exception as e:
            logger.error('Failed to extract %s: %s', pdf.name, e)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Convert composition dict to JSON string for CSV compatibility
    df['composition_json'] = df['composition'].apply(json.dumps)
    df = df.drop(columns=['composition'])

    return df


# ---------------------------------------------------------------------------
# CLI / standalone usage
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    import pandas as pd

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    eifu_dir = sys.argv[1] if len(sys.argv) > 1 else 'implant_eIFUs'

    df = extract_all(eifu_dir)

    if df.empty:
        print('No data extracted.')
        sys.exit(1)

    print(df[['manufacturer', 'system', 'component_type', 'component_label',
              'catalogue_family', 'material_name', 'alloy_standard']].to_string(index=False))

    out = Path(eifu_dir) / 'extracted_materials.csv'
    df.to_csv(out, index=False)
    print(f'\nSaved {len(df)} records → {out}')
