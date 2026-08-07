# Catalogue Number Sizer — Rule Reference

Reference documentation for every manufacturer/system-specific pattern implemented in
[`catalogue_num_sizer.py`](../catalogue_num_sizer.py). This module is a **fallback**
extractor used by the enrichment pipeline (`implant_lib_v3.py`, stage 9a) when `size`,
`side`, `thickness`, or `stability` could not be determined from free-text fields
(device description, brand name, GMDN term, etc.).

All rules are pure string/regex parsing of the catalogue number — no external lookups.
Entry point: `extract_from_catalogue(catalogue_num, manufacturer, component_type, brand_name)`.

Return dict (`_EMPTY` in the module) always has these keys, `None` when not decoded by a
given rule:

| Key | Meaning |
|-----|---------|
| `size` | Numeric or letter size code (e.g. `"3"`, `"D"`, `"AB 1-2"`, `"Small"`) |
| `side` | `"Left"` \| `"Right"` \| `None` |
| `thickness` | Polyethylene/insert thickness in mm, as a plain string |
| `stability` | `"CR"` \| `"PS"` \| `"CPS"` \| `"UC"` \| `"MC"` \| `"CCK"` \| `None` (only decoded for Link and Zimmer Persona inserts) |
| `poly_material` | Only set by Zimmer Persona insert (Vivacit-E flag) |
| `antioxidant` | Only set by Zimmer Persona insert (Vivacit-E flag) |
| `diameter` | Only set by Link patella/stem |

If nothing matches, every rule function returns the all-`None` dict (`_result()`).

---

## 1. Manufacturer Routing (`extract_from_catalogue`)

`manufacturer` is lowercased and matched by substring against these keyword groups, in
order. The first group that matches wins; within DePuy, digit-length routing is tried
before brand-name routing.

| Manufacturer keywords (any substring match) | Routes to |
|---|---|
| `zimmer`, `biomet`, `sulzer`, `centerpulse` | §2 Zimmer Persona / §3 Zimmer NexGen |
| `stryker`, `osteonics`, `howmedica` | §4 Stryker |
| `microport`, `wright` | §5 MicroPort / Wright |
| `depuy`, `johnson`, `j & j`, `j&j`, `finsbury` | §6–§16 DePuy family |
| `link` | §17 Link Orthopaedics |
| `smith` **and** `nephew` | §18 Smith & Nephew |

### Zimmer brand sub-routing
1. `"persona"` in `brand_name` → `_zimmer_persona()`
2. `"nexgen"` / `"nex gen"` / `"nex-gen"` in `brand_name` → `_zimmer_nexgen()`
3. No brand match → try `_zimmer_persona()` first; if it returns any non-`None` field, use it; otherwise fall back to `_zimmer_nexgen()`

### DePuy sub-routing
Catalogue number is hyphen-stripped first (`cat.replace("-", "")`), then:
1. If stripped form is **9 digits** → try `_depuy_route9()` (§ table below); use result if any field is non-`None`
2. Else if stripped form is **6 digits** → try `_depuy_route6()`; use result if any field is non-`None`
3. Else fall back to hyphenated/branded parsers by `brand_name`:
   - `"attune"` → `_depuy_attune()`
   - `"pfc"` or (`"sigma"` **and** `"pfc"`) → `_depuy_pfc_sigma()`
   - `"sigma"` → `_depuy_sigma()`
   - No brand match → try `_depuy_attune()`, `_depuy_sigma()`, `_depuy_pfc_sigma()` in that order, use first non-empty result

`_depuy_route9()` dispatch (by prefix of the 9-digit stripped string):

| Prefix test | Handler |
|---|---|
| `cat[:6]` in `{150400, 150401, 150410}` | `_depuy_attune_femoral9` |
| `cat[:4] == "1506"` | `_depuy_attune_tibial9` |
| `cat[:4] == "1581"` | `_depuy_pfc_sigma_tibial9` |
| `cat[:4]` in `{1516, 1517}` | `_depuy_attune_insert9` |
| `cat[:4] == "1518"` and `cat[4] in "12"` | `_depuy_attune_patella9` |
| `cat[:5]` in `{12940, 12941, 12942}` and `comp == "femoral"` | `_depuy_lcs_femoral9` |
| `cat[:5] == "12943"` and `comp == "tibial"` | `_depuy_mbt_tibial9` |
| `cat == "148807000"` | hardcoded → AMK femoral, size `"4"` |

`_depuy_route6()` dispatch (by prefix of the 6-digit stripped string):

| Prefix test | Handler |
|---|---|
| starts with `"962"` | `_depuy_pfc_sigma_insert6` |
| `cat[:3]` in `{860, 866}` | `_depuy_pfc_sigma_tibial_short` |
| starts with `"9704"` | hardcoded → size `"2"` |

`comp` (normalised component type, via `_norm_comp()`) is one of: `femoral`, `tibial`,
`insert`, `patellar`, `stem`, or the lowercased raw string if none match. Side is only ever
`Left`/`Right`/`None` — no `Universal` value is produced by this module (unlike the
free-text `FieldExtractor.extract_side()`).

---

## 2. Zimmer — Persona

Format: `42-5NNN-MMM-SS` (regex `^42-5(\d{3})-(\d{3})-(\d{2})$`), three captured groups
referred to below as `grp1` (NNN), `grp2` (MMM), `grp3` (SS).

| Component | grp3 (SS) → `side` | grp2 (MMM) → `size` | grp1 (NNN) → other |
|---|---|---|---|
| Femoral | `01`→Left, `02`→Right | via `_PERSONA_FEM_SIZE` lookup | — |
| Tibial | `01`→Left, `02`→Right | via `_PERSONA_TIB_SIZE` lookup | — |
| Insert | grp1 digit 1 (laterality): `1`→Left, `2`→Right | grp2 → `_PERSONA_INS_COMPAT` (tibia/femoral compatibility string, not a plain size) | grp1 digit 2 = Vivacit-E flag; grp1 digit 3 → `_PERSONA_INS_STAB` |

**Femoral size (`_PERSONA_FEM_SIZE`, MMM):**

| MMM | Size | MMM | Size |
|---|---|---|---|
| 050 | 1 | 062 | 7 |
| 052 | 2 | 064 | 8 |
| 054 | 3 | 066 | 9 |
| 056 | 4 | 068 | 10 |
| 058 | 5 | 070 | 11 |
| 060 | 6 | 074 | 12 |

**Tibial size (`_PERSONA_TIB_SIZE`, MMM):**

| MMM | Size | MMM | Size |
|---|---|---|---|
| 058 | A | 075 | F |
| 061 | B | 079 | G |
| 064 | C | 083 | H |
| 067 | D | 088 | J |
| 071 | E | | |

**Insert thickness**: `thickness = str(int(grp3))` — strips the leading zero (e.g. `"09"` → `"9"`).

**Insert stability (`_PERSONA_INS_STAB`, grp1 digit 3):**

| Code | Stability | Code | Stability |
|---|---|---|---|
| 0 | CR | 4 | PS |
| 1 | MC | 6 | CPS |
| 2 | UC | 8 | CCK |

**Insert Vivacit-E flag** (grp1 digit 2): `"2"` → `poly_material="HXLPE+VitE"`, `antioxidant="Vitamin E"`; otherwise both `None`.

**Insert compatibility label (`_PERSONA_INS_COMPAT`, grp2)** — returned in the `size` field:

| grp2 | Label | grp2 | Label |
|---|---|---|---|
| 001 | A-B / CR Fem 1-2 | 007 | J / CR Fem 9-12 |
| 002 | A-B / CR Fem 3-6 | 008 | E-F / PS Fem 10-11 |
| 003 | C-D / CR Fem 1-2 | 009 | G-H / PS Fem 6-9 |
| 004 | C-D / CR Fem 3-9 | 010 | G-H / PS Fem 10-12 |
| 005 | E-F / CR Fem 3-11 | 011 | J / PS Fem 10-12 |
| 006 | G-H / CR Fem 7-12 | | |

---

## 3. Zimmer — NexGen

Catalogue numbers are first normalised to `NNNN-0NN-NN`:
- Strip a leading `"00-"` or `"90-"` prefix (`_NEXGEN_STRIP_PREFIX`)
- Pad a 2-digit middle group to 3 digits (`_NEXGEN_PAD_MIDDLE`, e.g. `5964-12-01` → `5964-012-01`)

Then matched against `^(\d{4})-(\d{3})-(\d{2})$` → `base`, `mid`, `last`.

| Component | Required `base` | `side` (from `last`) | `size` (from `mid`) | `thickness` |
|---|---|---|---|---|
| Femoral | `5964` | `01`→Left, `02`→Right | `_NEXGEN_FEM_SIZE[mid]` | — |
| Tibial | `5981` | — | `_NEXGEN_TIB_SIZE[(mid, last)]` | — |
| Insert | `5964` | — | `_NEXGEN_INS_SIZE[mid]` | `str(int(last))` (strip leading zero) |

**Femoral size (`_NEXGEN_FEM_SIZE`, mid):**

| mid | 014 | 015 | 016 | 017 | 018 |
|---|---|---|---|---|---|
| Size | D | E | F | G | H |

**Tibial size (`_NEXGEN_TIB_SIZE`, keyed on (mid, last)):**

| mid | last=01 | last=02 |
|---|---|---|
| 037 | 3 | 4 |
| 047 | 5 | 6 |
| 057 | 7 | 8 |

**Insert size label (`_NEXGEN_INS_SIZE`, mid):**

| mid | Label | mid | Label |
|---|---|---|---|
| 020 | AB 1-2 | 040 | EF 5-6 |
| 022 | CD 1-2 | 041 | CD 5-6 |
| 030 | CD 3-4 | 042 | GH 5-6 |
| 031 | AB 3-4 | 050 | GH 7-10 |
| 032 | EF 3-4 | 051 | EF 7-10 |

---

## 4. Stryker

Format: `XXXX-[FGPBX]-NNN` (regex `^(\d{4})-([FGPB])-(\d{3})$`, case-insensitive). The 3-digit
suffix splits as `[size][remainder(2 digits)]`.

| Letter | Component | `size` | Field decoded from `remainder` |
|---|---|---|---|
| `F` | Femoral | suffix digit 1 | `01`→Left, `02`→Right → `side` |
| `B` | Tibial | suffix digit 1 | not decoded |
| `G`, `P`, `X`* | Insert | suffix digit 1 | `str(int(remainder))` → `thickness` (strips leading zero) |

\* `X` is accepted by the letter→component map (`_STRYKER_LETTER_COMP`) for soft
validation logging but the regex itself only captures `F`, `G`, `P`, `B` — `X` never
actually reaches the letter-dispatch `if` chain since it isn't in `(F|G|P|B)`; effectively
only F/G/P/B are decoded.

If the letter's expected component (via `_STRYKER_LETTER_COMP`) disagrees with the passed
`component_type`, a debug log is emitted but the catalogue letter still wins — extraction
proceeds using the letter, not the passed `comp`.

---

## 5. MicroPort / Wright Medical

All catalogue numbers are exactly 8 alphanumeric characters, no hyphens
(`^[A-Z0-9]{8}$`). Position 2 (0-indexed `c[1]`) identifies component: `F` = femoral,
`T` = tibial. Position 0 (`c[0]`) distinguishes two tibial sub-formats.

| Component | Format | `size` position | `side` position |
|---|---|---|---|
| Femoral (`c[1]=='F'`) | `EFSRN[size]P[L/R]` | `c[5]` (if digit) | `c[7]`: `L`→Left, `R`→Right |
| Tibial fmt 1 (`c[0]=='E'`, `c[1]=='T'`) | `ETPKN[size][S/P]R` | `c[5]` (if digit) | not decoded |
| Tibial fmt 2 (`c[0]=='K'`, `c[1]=='T'`) | `KTCCNP[size][0/1]` | `c[6]` (if digit) | not decoded |

Requires `comp` (passed component_type, normalised) to equal `"femoral"` or `"tibial"`
respectively — the position-1 letter alone is not sufficient, it must agree with the
caller-supplied component type.

---

## 6. DePuy — ATTUNE (hyphenated, legacy)

Format: `(1504|1506|1517)-XX-NNN` (regex `^(1504|1506|1517)-\d{2}-(\d{3})$`). `last` = final
3-digit group.

| Prefix (or `comp` fallback) | `size` | `side` | `thickness` |
|---|---|---|---|
| `1506` / `comp=="tibial"` | `str(int(last[-1]))` (last digit) | — | — |
| `1504` / `comp=="femoral"` | `str(int(last[-1]))` (last digit) | `last[-2]`: `1`→Left, `2`→Right | — |
| `1517` / `comp=="insert"` | `str(int(last[0]))` (first digit) | — | `str(int(last[1:]))` (last 2 digits, leading zero stripped) |

---

## 7. DePuy — Sigma (non-PFC, hyphenated)

Two independent regexes, dispatched by `comp`:

| Component | Pattern | `size` | `side` |
|---|---|---|---|
| Tibial | `^1581-\d{2}-\d{3}$` | 5th digit of the hyphen-stripped string (0-indexed pos 4) | — |
| Femoral | `^940\d{3}$` | last digit | 2nd-last digit: `1`→Left, `2`→Right |

---

## 8. DePuy — PFC Sigma / NexGen PFC (hyphenated)

| Component | Pattern | `size` | `side` |
|---|---|---|---|
| Tibial | `^1294-\d{2}-(\d{3})$` | 2nd-last digit of the captured last group | — |
| Femoral | `^9600?(\d{2})$` (matches `960084` or `96084`) | via `_PFC_FEM_LOOKUP` | via `_PFC_FEM_LOOKUP` |

**`_PFC_FEM_LOOKUP` (2-digit code → (size, side)):**

| Code | Size | Side | Code | Size | Side |
|---|---|---|---|---|---|
| 81 | 2 | Left | 87 | 2 | Right |
| 82 | 3 | Left | 88 | 3 | Right |
| 83 | 4 | Left | 89 | 4 | Right |
| 84 | 5 | Left | 90 | 5 | Right |
| 85 | 1.5 | Left | 91 | 1.5 | Right |
| 86 | 2.5 | Left | 92 | 2.5 | Right |

---

## 9. DePuy — ATTUNE Femoral, 9-digit unhyphenated

Prefix must be one of `150400` (CR), `150401` (Cementless), `150410` (PS); total length 9.
Suffix (3 digits) = `[series][last2]`.

| Condition on `n = int(last2)` | `size` |
|---|---|
| `23 <= n <= 26` | `f"{n-20}N"` (23→3N, 24→4N, 25→5N, 26→6N) |
| `series in {"1","2"}` and `1 <= n <= 10` | `str(n)` |
| otherwise | `None` |

---

## 10. DePuy — ATTUNE Tibial, 9-digit unhyphenated

Prefix `1506`, total length 9. `size = int(cat[-2:])`, covers all variants sharing the
prefix (`15064X` standard, `15066X` wide, `15067X`/`15068X` Attune S+, `150611` Cementless)
— the sub-variant is not separately decoded, only the trailing 2-digit size.

---

## 11. DePuy — PFC Sigma Tibial, 9-digit unhyphenated

Prefix `1581`, total length 9. `size = _PFC_TIB9_SIZE[cat[4:6]]`.

| Code (`cat[4:6]`) | Size |
|---|---|
| 20 | 2 |
| 25 | 2.5 |
| 30 | 3 |
| 40 | 4 |
| 50 | 5 |
| 60 | 6 |

---

## 12. DePuy — PFC Sigma Tibial, 6-digit (short form)

Pattern `^8(?:60|66)\d{3}$` (i.e. `860NNN` or `866NNN`). `size = cat[-1]` (last digit, raw).

---

## 13. DePuy — ATTUNE / PFC Insert, 9-digit unhyphenated

Prefix `1516` or `1517`, total length 9. `size = int(cat[5:7])` (positions 5–6, e.g.
`"01"`→1 … `"10"`→10).

---

## 14. DePuy — ATTUNE Patella, 9-digit unhyphenated

Prefix `1518`, digit at position 4 must be `1` or `2`, total length 9. `size = int(cat[-3:])`
— last 3 digits interpreted directly as a diameter in mm (e.g. `029`→29, `041`→41), stored
in the `size` field (not `diameter`).

---

## 15. DePuy — LCS Femoral, 9-digit unhyphenated

Prefix `12940` (CR), `12941` (PS), or `12942` (FS), total length 9. **Requires
`comp == "femoral"`** at the router level (`_depuy_route9`) to avoid false-positive matches
against PFC tibial numbers with the hyphens stripped. `size = _LCS_SIZE[cat[-2:]]`.

| Last 2 digits | Size label |
|---|---|
| 20 | Small |
| 30 | Medium |
| 40 | Standard |
| 50 | Standard+ |
| 60 | Large |
| 70 | Large+ |

---

## 16. DePuy — MBT Tibial Baseplate, 9-digit unhyphenated

Prefix `12943`, total length 9. **Requires `comp == "tibial"`** at the router level (same
false-positive-avoidance reason as LCS Femoral above). `size = _MBT_LAST2_SIZE[cat[-2:]]`.

| Last 2 digits | Size |
|---|---|
| 15 | 1.5 |
| 20 | 2 |
| 25 | 2.5 |
| 30 | 3 |
| 40 | 4 |
| 50 | 5 |
| 60 | 6 |
| 70 | 7 |

---

## 17. DePuy — PFC Sigma Insert, 6-digit (`962XXX` / `9704XX`)

**`962` series** — pattern `^962(\d)(\d)(\d)$` → `series`, `tens`, `units`; `last2 = tens+units`.

| Condition | `size` |
|---|---|
| `last2 == "21"` | `"2.5"` if `series=="1"` else `"2"` |
| `series == "3"` and `tens_int >= 4` | `str(tens_int - 1)` (offset by −1) |
| `tens_int == 0` | `None` |
| otherwise | `str(tens_int)` |

**`9704XX` series** — pattern `^9704\d{2}$` → fixed `size = "2"`.

---

## 18. DePuy — hardcoded special cases

| Catalogue number | Result |
|---|---|
| `148807000` | AMK femoral, `size = "4"` |
| `9704XX` (any) | `size = "2"` (also listed above) |

---

## 19. Link Orthopaedics

Format: `AAA-BBB/CC` (regex `^[A-Z0-9]+-(\d{3,4})/([A-Z0-9]{2})$`, case-insensitive).
- `AAA` = implant family prefix, ignored
- `BBB` = 3–4 digit component-type code — **this alone determines component class,
  overriding the caller-supplied `component_type`**
- `CC` = 2-character size/dimension code (digits, or literal `"X0"` meaning size 10)

**BBB → component class** (`_LINK_BBB_DIRECT` for exact matches, numeric ranges otherwise):

| BBB | Component class | `side` | `stability` |
|---|---|---|---|
| 010 | femoral | Right | CR |
| 011 | femoral | Left | CR |
| 030 | femoral | Right | CCK |
| 031 | femoral | Left | CCK |
| 040, 090 | tibial (monoblock) | — | — |
| 050, 100 | tibial (modular) | — | — |
| 511 | patella | — | — |
| 241–247 | insert_cr | — | CR |
| 253–259 | insert_ps | — | PS |
| 261–262 | insert_ps_plus | — | PS |
| 291–299 | insert_uc | — | UC |
| 413–419 | tibial_all_poly | — | — |
| 4-digit, starts `"29"` or `"15"` | stem | — | — |
| anything else | *(no match — returns empty result)* | | |

**CC decoding, by resolved component class:**

| Component class | Field set | Rule |
|---|---|---|
| `insert_cr`, `insert_ps`, `insert_ps_plus`, `insert_uc`, `tibial_all_poly` | `thickness` | `thickness = str(int(CC))` (if CC parses as int) |
| `insert_cr` only, additionally | `size` | Looked up from **BBB** (not CC) via `_LINK_INSERT_CR_SIZE` — see table below |
| `patella` | `diameter` | `diameter = str(int(CC))` (mm) |
| `stem` | `diameter` | `diameter = CC` (raw 2-char string, not int-parsed — preserves alphanumeric dimension codes) |
| `femoral` / `tibial` (default branch) | `size` | See size-decoding rules A–C below |

**Femoral/tibial size decoding from CC** (`cc_int = int(CC)`, falls back to `None` if CC is
non-numeric like `"X0"`):

| Rule | Condition | `size` |
|---|---|---|
| A | `CC == "X0"` | `"10"` (special-cased — size 10) |
| B | `cc_int % 10 == 0` and `cc_int > 0` | `str(cc_int // 10)` (e.g. `10`→1, `50`→5) |
| C | `cc_int % 10 == 5` | `f"{cc_int // 10}+"` (e.g. `35`→`"3+"`) |

**`insert_cr` size label from BBB (`_LINK_INSERT_CR_SIZE`):**

| BBB | Size label |
|---|---|
| 241, 242 | Size 1-2 |
| 243, 244 | Size 3-4 |
| 245, 246 | Size 5-6 |
| 247 | Size 7+ |

**Stem rule detail**: requires `len(bbb_str) == 4` — this means only 4-digit BBB codes in
the `29xx`/`15xx` ranges (2900–2999, 1500–1599) are treated as stems. A 3-digit BBB like
`299` does **not** match this rule and instead falls through to "no match" (it is an
unrecognised insert-type code, not a stem).

---

## 20. Smith & Nephew

Format: 8-digit number, `comp == "femoral"` required. `size = cat[-1]` (last digit, raw
string). No side, thickness, or other fields decoded. Only femoral is supported for this
manufacturer.

---

## Master Summary Table

| # | Manufacturer | System / Format | Component(s) | Fields decoded |
|---|---|---|---|---|
| 2 | Zimmer | Persona `42-5NNN-MMM-SS` | Femoral, Tibial, Insert | size, side, thickness (insert), stability (insert), poly_material/antioxidant (insert) |
| 3 | Zimmer | NexGen `NNNN-MMM-SS` (normalised) | Femoral, Tibial, Insert | size, side (femoral), thickness (insert) |
| 4 | Stryker | `XXXX-[FGPB]-NNN` | Femoral, Tibial, Insert | size, side (femoral), thickness (insert) |
| 5 | MicroPort / Wright | 8-char alphanumeric, 2 tibial sub-formats | Femoral, Tibial | size, side (femoral) |
| 6 | DePuy | ATTUNE hyphenated `150X-XX-NNN` | Femoral, Tibial, Insert | size, side (femoral), thickness (insert) |
| 7 | DePuy | Sigma hyphenated `1581-XX-XNXXX` / `940XYZ` | Tibial, Femoral | size, side (femoral) |
| 8 | DePuy | PFC Sigma hyphenated `1294-XX-XSX` / `9600XX` | Tibial, Femoral | size, side (femoral, via lookup) |
| 9 | DePuy | ATTUNE Femoral 9-digit `150{400,401,410}NNN` | Femoral | size |
| 10 | DePuy | ATTUNE Tibial 9-digit `1506XXXXX` | Tibial | size |
| 11 | DePuy | PFC Sigma Tibial 9-digit `1581XXXXX` | Tibial | size |
| 12 | DePuy | PFC Sigma Tibial 6-digit `86{0,6}NNN` | Tibial | size |
| 13 | DePuy | ATTUNE/PFC Insert 9-digit `151{6,7}XXXXX` | Insert | size |
| 14 | DePuy | ATTUNE Patella 9-digit `1518[12]XXXX` | Patellar | size (mm diameter, stored as size) |
| 15 | DePuy | LCS Femoral 9-digit `1294{0,1,2}XNNN` | Femoral | size (label) |
| 16 | DePuy | MBT Tibial 9-digit `12943XNNN` | Tibial | size |
| 17 | DePuy | PFC Sigma Insert 6-digit `962XXX` / `9704XX` | Insert | size |
| 18 | DePuy | Hardcoded (`148807000`, `9704XX`) | Femoral, Insert | size (fixed) |
| 19 | Link Orthopaedics | `AAA-BBB/CC` | Femoral, Tibial, Insert (CR/PS/PS+/UC), Tibial-all-poly, Patella, Stem | size, side, stability, thickness, diameter — depending on BBB-resolved component class |
| 20 | Smith & Nephew | 8-digit numeric | Femoral | size |

---

## Gotchas (from module docstrings / inline comments)

- **DePuy hyphen stripping is tried before brand routing**: a hyphenated 9-digit-when-stripped
  DePuy number (e.g. `1504-00-101` → `150400101`) is parsed by the 9-digit rules first,
  *not* the legacy hyphenated ATTUNE/Sigma/PFC parsers — brand name only matters as a
  fallback when digit-length routing produces no non-`None` fields.
- **LCS Femoral / MBT Tibial component guards**: both require the caller's `component_type`
  to match (`femoral` / `tibial` respectively) before the prefix-only regex is trusted —
  this avoids misclassifying PFC tibial numbers like `1294-XX-NNN` once hyphens are
  stripped, since their prefixes (`12940`–`12943`) overlap in shape.
- **Link stem rule requires a 4-digit BBB**: `29xx`/`15xx` must be exactly 4 digits; a
  3-digit BBB such as `299` is *not* treated as a stem and returns no match — it is an
  unrecognised insert-type code instead.
- **Link `patella`/`stem` decode into `diameter`, not `size`** — everything else in this
  module decodes into `size`/`thickness`. Callers merging this dict into a record should
  check the `diameter` key for those two component classes.
- **Stryker `X` letter is defined in the lookup table but unreachable** — the capturing
  regex only allows `[FGPB]`, so `_STRYKER_LETTER_COMP["X"]` is dead code for match
  purposes (only used if some future regex change adds `X` back in).
- **All manufacturer matching is substring-based on a lowercased string** — e.g. `"biomet"`
  routes to the Zimmer parsers (Zimmer Biomet), and `"j&j"` / `"j & j"` route to DePuy
  (Johnson & Johnson DePuy Synthes).
