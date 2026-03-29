"""
Catalogue Number Size Extractor
================================
Extracts size, side, thickness, and (where encoded) stability from knee
implant catalogue numbers using manufacturer / model–specific conventions.

Used as a fallback in the enrichment pipeline when these fields cannot be
determined from free-text fields.

Return value
------------
All functions return a dict with the following keys (all Optional[str]):
  size       – numeric or letter size code (e.g. "3", "D", "AB 1-2")
  side       – "Left" | "Right" | None
  thickness  – thickness in mm as a plain string (e.g. "9", "10")
  stability  – "CR" | "PS" | "CPS" | "UC" | "MC" | "CCK" | None
               (currently only decoded for Zimmer Persona inserts)

Supported systems
-----------------
  Zimmer    Persona   – Femoral, Tibial, Insert
  Zimmer    NexGen    – Femoral, Tibial, Insert
  Stryker   (all)     – Femoral (XXXX-F-NNN), Tibial (XXXX-B-NNN),
                        Insert  (XXXX-G/P-NNN)
  MicroPort / Wright  – Femoral, Tibial (two sub-formats)
  DePuy     ATTUNE    – Femoral, Tibial, Insert
  DePuy     Sigma     – Femoral, Tibial
  DePuy     PFC/Sigma – Femoral, Tibial
  Smith & Nephew      – Femoral
"""

import re
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Return-value helpers
# ---------------------------------------------------------------------------

_EMPTY: Dict[str, Optional[str]] = {
    "size": None,
    "side": None,
    "thickness": None,
    "stability": None,
}


def _result(**kwargs) -> Dict[str, Optional[str]]:
    r = dict(_EMPTY)
    r.update(kwargs)
    return r


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _norm_comp(component_type: str) -> str:
    """Map component_type string to a canonical short form."""
    c = (component_type or "").lower()
    if "femoral" in c and "stem" not in c:
        return "femoral"
    if ("tibial" in c or "tibia" in c) and "insert" not in c and "stem" not in c:
        return "tibial"
    if "insert" in c or ("bearing" in c and "tibial" not in c):
        return "insert"
    if "patellar" in c or "patella" in c:
        return "patellar"
    if "stem" in c:
        return "stem"
    return c.strip()


def _norm_mfr(manufacturer: str) -> str:
    return (manufacturer or "").lower()


# ===========================================================================
# Zimmer – Persona
# ===========================================================================

# Femoral: 42-5NNN-MMM-SS   (MMM = size code, SS = side)
# Tibial:  42-5NNN-MMM-SS   (MMM = size code, SS = side 01/02)
# Insert:  42-5LVS-MMM-TT   (L=laterality, V=vivacit flag, S=stability,
#                             MMM = tibia size compat, TT = thickness)

_PERSONA_RE = re.compile(r"^42-5(\d{3})-(\d{3})-(\d{2})$")

_PERSONA_FEM_SIZE = {
    "050": "1",
    "052": "2",
    "054": "3",
    "056": "4",
    "058": "5",
    "060": "6",
    "062": "7",
    "064": "8",
    "066": "9",
    "068": "10",
    "070": "11",
    "074": "12",
}
_PERSONA_TIB_SIZE = {
    "058": "A",
    "061": "B",
    "064": "C",
    "067": "D",
    "071": "E",
    "075": "F",
    "079": "G",
    "083": "H",
    "088": "J",
}
_PERSONA_INS_STAB = {
    "0": "CR",
    "1": "MC",
    "2": "UC",
    "4": "PS",
    "6": "CPS",
    "8": "CCK",
}


def _zimmer_persona(cat: str, comp: str) -> Dict[str, Optional[str]]:
    m = _PERSONA_RE.match(cat)
    if not m:
        return _result()
    grp1, grp2, grp3 = m.group(1), m.group(2), m.group(3)

    side = "Left" if grp3 == "01" else "Right" if grp3 == "02" else None

    if comp == "femoral":
        return _result(
            size=_PERSONA_FEM_SIZE.get(grp2),
            side=side,
        )

    if comp == "tibial":
        return _result(
            size=_PERSONA_TIB_SIZE.get(grp2),
            side=side,
        )

    if comp == "insert":
        lat, stab_code = grp1[0], grp1[2]
        return _result(
            size=_PERSONA_TIB_SIZE.get(grp2),  # tibial size compatibility
            side="Left" if lat == "1" else "Right" if lat == "2" else None,
            thickness=str(int(grp3)),  # strip leading zero
            stability=_PERSONA_INS_STAB.get(stab_code),
        )

    return _result()


# ===========================================================================
# Zimmer – NexGen
# ===========================================================================
# Catalogue numbers may have:
#   • a "00-" or "90-" prefix  (e.g. 00-5964-012-01, 90-5954-37-02)
#   • a missing leading zero in the middle group  (e.g. 5964-12-01)
#
# All three forms are normalised to NNNN-0NN-NN before parsing.

_NEXGEN_STRIP_PREFIX = re.compile(r"^(?:00|90)-")
_NEXGEN_PAD_MIDDLE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _normalize_nexgen(cat: str) -> str:
    c = _NEXGEN_STRIP_PREFIX.sub("", cat.strip())
    # Pad 2-digit middle group to 3 digits
    c = _NEXGEN_PAD_MIDDLE.sub(lambda m: f"{m.group(1)}-0{m.group(2)}-{m.group(3)}", c)
    return c


_NEXGEN_RE = re.compile(r"^(\d{4})-(\d{3})-(\d{2})$")

# Femoral (base 5964): middle 014-018 → D-H
_NEXGEN_FEM_SIZE = {"014": "D", "015": "E", "016": "F", "017": "G", "018": "H"}

# Tibial (base 5981): (middle, last) → size
_NEXGEN_TIB_SIZE = {
    ("037", "01"): "3",
    ("037", "02"): "4",
    ("047", "01"): "5",
    ("047", "02"): "6",
    ("057", "01"): "7",
    ("057", "02"): "8",
}

# Insert (base 5964): middle → femur+tibia size label
_NEXGEN_INS_SIZE = {
    "020": "AB 1-2",
    "022": "CD 1-2",
    "031": "AB 3-4",
    "030": "CD 3-4",
    "032": "EF 3-4",
    "041": "CD 5-6",
    "040": "EF 5-6",
    "042": "GH 5-6",
    "051": "EF 7-10",
    "050": "GH 7-10",
}


def _zimmer_nexgen(cat: str, comp: str) -> Dict[str, Optional[str]]:
    norm = _normalize_nexgen(cat)
    m = _NEXGEN_RE.match(norm)
    if not m:
        return _result()
    base, mid, last = m.group(1), m.group(2), m.group(3)

    if comp == "femoral" and base == "5964":
        side = "Left" if last == "01" else "Right" if last == "02" else None
        return _result(size=_NEXGEN_FEM_SIZE.get(mid), side=side)

    if comp == "tibial" and base == "5981":
        size = _NEXGEN_TIB_SIZE.get((mid, last))
        return _result(size=size)

    if comp == "insert" and base == "5964":
        return _result(
            size=_NEXGEN_INS_SIZE.get(mid),
            thickness=str(int(last)),  # last 2 = thickness mm
        )

    return _result()


# ===========================================================================
# Stryker
# ===========================================================================
# Format:  XXXX-[FGPB]-NNN
#   F = Femoral   B = Tibial   G = Insert (standard/CR)   P = Insert (PS)
# NNN = [size][side_or_thickness_hi][side_or_thickness_lo]
#   Femoral:  last 2 digits = 01 Left / 02 Right
#   Insert:   last 2 digits = thickness (strip leading zero)
#   Tibial:   first digit after letter = size  (last 2 not specified)

_STRYKER_RE = re.compile(r"^(\d{4})-([FGPB])-(\d{3})$", re.IGNORECASE)
_STRYKER_LETTER_COMP = {
    "F": "femoral",
    "B": "tibial",
    "G": "insert",
    "P": "insert",
}


def _stryker(cat: str, comp: str) -> Dict[str, Optional[str]]:
    m = _STRYKER_RE.match(cat)
    if not m:
        return _result()
    letter = m.group(2).upper()
    suffix = m.group(3)  # 3 digits after letter
    size = suffix[0]  # first digit = size
    remainder = suffix[1:]  # last 2 digits

    # Soft validation: letter should match the passed component type
    expected = _STRYKER_LETTER_COMP.get(letter)
    if expected and comp and comp != expected:
        logger.debug(
            "Stryker catalogue letter %s suggests %s but component_type is %s",
            letter,
            expected,
            comp,
        )

    if letter == "F":
        side = "Left" if remainder == "01" else "Right" if remainder == "02" else None
        return _result(size=size, side=side)

    if letter == "B":
        return _result(size=size)

    if letter in ("G", "P"):
        return _result(size=size, thickness=str(int(remainder)))  # strip leading zero

    return _result()


# ===========================================================================
# MicroPort / Wright Medical
# ===========================================================================
# All formats are 8 alphanumeric characters (no hyphens).
#
# Femoral:    E F S R N [size] P [L/R]      pos 1=F, pos 5=size, pos 7=L/R
# Tibia fmt1: E T P K N [size] [S/P] R      pos 1=T, pos 5=size, pos 6=S/P
# Tibia fmt2: K T C C N P [size] [0/1]      pos 1=T, pos 6=size, pos 7=plus?
#
# Distinguisher: pos 0 = E → femoral or tibial fmt1
#                pos 0 = K → tibial fmt2

_MICROPORT_RE = re.compile(r"^[A-Z0-9]{8}$", re.IGNORECASE)


def _microport(cat: str, comp: str) -> Dict[str, Optional[str]]:
    c = cat.upper().strip()
    if not _MICROPORT_RE.match(c):
        return _result()

    comp_letter = c[1]  # F = femoral, T = tibial

    if comp_letter == "F" and comp == "femoral":
        size = c[5] if c[5].isdigit() else None
        side = "Left" if c[7] == "L" else "Right" if c[7] == "R" else None
        return _result(size=size, side=side)

    if comp_letter == "T" and comp == "tibial":
        if c[0] == "E":
            # Format 1: ETPKN[size][S/P]R
            size = c[5] if c[5].isdigit() else None
            return _result(size=size)
        if c[0] == "K":
            # Format 2: KTCCNP[size][0/1]
            size = c[6] if c[6].isdigit() else None
            return _result(size=size)

    return _result()


# ===========================================================================
# DePuy – ATTUNE
# ===========================================================================
# Tibia:   1506-XX-00S   → size = last digit of last group
# Femoral: 1504-XX-XYZ   → Z = size, Y = side (1=L, 2=R)
# Insert:  1517-XX-STT   → S = size (1st digit of last group), TT = thickness

_ATTUNE_RE = re.compile(r"^(1504|1506|1517)-\d{2}-(\d{3})$")


def _depuy_attune(cat: str, comp: str) -> Dict[str, Optional[str]]:
    m = _ATTUNE_RE.match(cat)
    if not m:
        return _result()
    base, last = m.group(1), m.group(2)

    if base == "1506" or comp == "tibial":
        return _result(size=str(int(last[-1])))

    if base == "1504" or comp == "femoral":
        side = "Left" if last[-2] == "1" else "Right" if last[-2] == "2" else None
        return _result(size=str(int(last[-1])), side=side)

    if base == "1517" or comp == "insert":
        return _result(
            size=str(int(last[0])),
            thickness=str(int(last[1:])),  # strip leading zero e.g. "08" → "8"
        )

    return _result()


# ===========================================================================
# DePuy – Sigma (non-PFC)
# ===========================================================================
# Tibia:   1581-XX-X[size]XX   → 5th digit (1-indexed) of digit-only string = size
# Femoral: 940XYZ              → Z = size, Y = side (1=L, 2=R); starts with 940

_SIGMA_TIB_RE = re.compile(r"^1581-\d{2}-\d{3}$")
_SIGMA_FEM_RE = re.compile(r"^940\d{3}$")


def _depuy_sigma(cat: str, comp: str) -> Dict[str, Optional[str]]:
    digits = cat.replace("-", "")

    if comp == "tibial" and _SIGMA_TIB_RE.match(cat):
        if len(digits) >= 5:
            return _result(size=digits[4])  # 5th digit (0-indexed pos 4)

    if comp == "femoral" and _SIGMA_FEM_RE.match(cat):
        side = "Left" if digits[-2] == "1" else "Right" if digits[-2] == "2" else None
        return _result(size=digits[-1], side=side)

    return _result()


# ===========================================================================
# DePuy – PFC Sigma / Nexgen PFC
# ===========================================================================
# Tibia:   1294-XX-XSX   → second-last digit of last group = size
# Femoral: 9600XX        → last 2 digits look up size + side from table

_PFC_TIB_RE = re.compile(r"^1294-\d{2}-(\d{3})$")
_PFC_FEM_RE = re.compile(r"^9600?(\d{2})$")  # 960084 or 96084

_PFC_FEM_LOOKUP = {
    "81": ("2", "Left"),
    "82": ("3", "Left"),
    "83": ("4", "Left"),
    "84": ("5", "Left"),
    "85": ("1.5", "Left"),
    "86": ("2.5", "Left"),
    "87": ("2", "Right"),
    "88": ("3", "Right"),
    "89": ("4", "Right"),
    "90": ("5", "Right"),
    "91": ("1.5", "Right"),
    "92": ("2.5", "Right"),
}


def _depuy_pfc_sigma(cat: str, comp: str) -> Dict[str, Optional[str]]:
    if comp == "tibial":
        m = _PFC_TIB_RE.match(cat)
        if m:
            last = m.group(1)  # e.g. "140"
            return _result(size=last[-2])  # second-last digit

    if comp == "femoral":
        m = _PFC_FEM_RE.match(cat)
        if m:
            code = m.group(1)  # last 2 digits
            entry = _PFC_FEM_LOOKUP.get(code)
            if entry:
                return _result(size=entry[0], side=entry[1])

    return _result()


# ===========================================================================
# Smith & Nephew
# ===========================================================================
# Femoral: 8-digit number, last digit = size

_SNP_FEM_RE = re.compile(r"^\d{8}$")


def _smith_nephew(cat: str, comp: str) -> Dict[str, Optional[str]]:
    if comp == "femoral" and _SNP_FEM_RE.match(cat):
        return _result(size=cat[-1])
    return _result()


# ===========================================================================
# Public entry point
# ===========================================================================


def extract_from_catalogue(
    catalogue_num: str,
    manufacturer: str,
    component_type: str,
    brand_name: str = "",
) -> Dict[str, Optional[str]]:
    """
    Extract size, side, thickness, and stability from a catalogue number.

    Parameters
    ----------
    catalogue_num  : raw catalogue number string
    manufacturer   : CJRR canonical manufacturer name (or raw company name)
    component_type : component type string (e.g. "Femoral", "Insert", "Tibial")
    brand_name     : product brand / system name for sub-model routing
                     (e.g. "Persona", "NexGen", "ATTUNE")

    Returns
    -------
    dict with keys: size, side, thickness, stability  (all str | None)
    Returns all-None dict if catalogue number does not match any known pattern.
    """
    if not catalogue_num or not manufacturer:
        return _result()

    cat = str(catalogue_num).strip()
    mfr = _norm_mfr(manufacturer)
    comp = _norm_comp(component_type)
    brand = (brand_name or "").lower()

    try:
        # ---- Zimmer / Biomet / Sulzer / Centerpulse ----
        if any(k in mfr for k in ("zimmer", "biomet", "sulzer", "centerpulse")):
            if "persona" in brand:
                return _zimmer_persona(cat, comp)
            if "nexgen" in brand or "nex gen" in brand or "nex-gen" in brand:
                return _zimmer_nexgen(cat, comp)
            # Fallback: try each by pattern
            r = _zimmer_persona(cat, comp)
            if any(r.values()):
                return r
            return _zimmer_nexgen(cat, comp)

        # ---- Stryker / Osteonics / Howmedica ----
        if any(k in mfr for k in ("stryker", "osteonics", "howmedica")):
            return _stryker(cat, comp)

        # ---- MicroPort / Wright ----
        if any(k in mfr for k in ("microport", "wright")):
            return _microport(cat, comp)

        # ---- DePuy / Johnson / Finsbury ----
        if any(k in mfr for k in ("depuy", "johnson", "j & j", "j&j", "finsbury")):
            if "attune" in brand:
                return _depuy_attune(cat, comp)
            if "pfc" in brand or ("sigma" in brand and "pfc" in brand):
                return _depuy_pfc_sigma(cat, comp)
            if "sigma" in brand:
                return _depuy_sigma(cat, comp)
            # Fallback: try all DePuy patterns
            for fn in (_depuy_attune, _depuy_sigma, _depuy_pfc_sigma):
                r = fn(cat, comp)
                if any(r.values()):
                    return r

        # ---- Smith & Nephew ----
        if "smith" in mfr and "nephew" in mfr:
            return _smith_nephew(cat, comp)

    except Exception as exc:
        logger.debug(
            "catalogue_num_sizer error for %r (%s / %s): %s",
            catalogue_num,
            manufacturer,
            component_type,
            exc,
        )

    return _result()
