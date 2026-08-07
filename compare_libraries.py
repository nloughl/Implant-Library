"""
compare_libraries.py
--------------------
Compares implant_library_final_SAE_May.csv against a current enriched library CSV/XLSX.
Focuses on stability, fixation, material, bearing_type, and component_type fields.

Usage:
    python compare_libraries.py
    python compare_libraries.py --sae implant_library_final_SAE_May.csv --enr outputs/knee_implant_library_enriched_20260715_164822.xlsx
    python compare_libraries.py --output comparison_report.csv
"""

import argparse
import glob
import os
from collections import Counter
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIELDS = [
    "stability",
    "fixation",
    "metal_material",
    "poly_material",
    "bearing_type",
    "component_type",
]

BLANK_SENTINELS = {"", "n/a", "nan", "none"}


def is_blank(v: str) -> bool:
    return v.strip().lower() in BLANK_SENTINELS


def diff_category(sae_val: str, enr_val: str) -> str:
    sv = sae_val.strip()
    ev = enr_val.strip()
    if sv == ev:
        return "same"
    if is_blank(sv) and is_blank(ev):
        return "both_blank"
    if not is_blank(sv) and is_blank(ev):
        return "SAE_only"
    if is_blank(sv) and not is_blank(ev):
        return "ENR_only"
    return "conflict"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_file(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    return df


def auto_detect_enr() -> str:
    matches = sorted(
        glob.glob("outputs/knee_implant_library_enriched_*.xlsx")
        + glob.glob("outputs/knee_implant_library_enriched_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError("No enriched library found in outputs/")
    return matches[0]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(sae_d: pd.DataFrame, enr_d: pd.DataFrame) -> dict:
    shared = set(sae_d.index) & set(enr_d.index)
    only_sae = set(sae_d.index) - set(enr_d.index)
    only_enr = set(enr_d.index) - set(sae_d.index)

    results = {
        "coverage": {
            "sae_total": len(sae_d),
            "enr_total": len(enr_d),
            "shared": len(shared),
            "only_sae": len(only_sae),
            "only_enr": len(only_enr),
        },
        "fields": {},
    }

    for field in FIELDS:
        sae_has = field in sae_d.columns
        enr_has = field in enr_d.columns
        if not sae_has and not enr_has:
            continue

        counts = Counter()
        conflicts = []
        sae_only_rows = []
        enr_only_rows = []

        for cat in shared:
            sv = sae_d.at[cat, field].strip() if sae_has else ""
            ev = enr_d.at[cat, field].strip() if enr_has else ""
            category = diff_category(sv, ev)
            counts[category] += 1

            if category == "conflict":
                conflicts.append({"catalogue_num": cat, "SAE": sv, "ENR": ev})
            elif category == "SAE_only":
                sae_only_rows.append({"catalogue_num": cat, "SAE": sv})
            elif category == "ENR_only":
                enr_only_rows.append({"catalogue_num": cat, "ENR": ev})

        # Conflict value-pair patterns
        conflict_patterns = Counter(
            (r["SAE"], r["ENR"]) for r in conflicts
        )

        results["fields"][field] = {
            "counts": dict(counts),
            "conflicts": conflicts,
            "conflict_patterns": conflict_patterns.most_common(30),
            "sae_only_rows": sae_only_rows,
            "enr_only_rows": enr_only_rows,
        }

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

SEP = "=" * 70


def print_report(results: dict, sae_path: str, enr_path: str) -> None:
    cov = results["coverage"]
    print(SEP)
    print("LIBRARY COMPARISON REPORT")
    print(f"  SAE file : {sae_path}")
    print(f"  ENR file : {enr_path}")
    print(SEP)
    print(f"  SAE unique catalogue nums : {cov['sae_total']:,}")
    print(f"  ENR unique catalogue nums : {cov['enr_total']:,}")
    print(f"  Shared (compared)         : {cov['shared']:,}")
    print(f"  Only in SAE               : {cov['only_sae']:,}")
    print(f"  Only in ENR               : {cov['only_enr']:,}")
    print()

    for field, data in results["fields"].items():
        c = data["counts"]
        total_compared = cov["shared"]
        n_conflict = c.get("conflict", 0)
        n_same = c.get("same", 0)
        n_sae_only = c.get("SAE_only", 0)
        n_enr_only = c.get("ENR_only", 0)
        n_both_blank = c.get("both_blank", 0)

        print(SEP)
        print(f"FIELD: {field.upper()}")
        print(f"  Same value           : {n_same:,}  ({n_same/total_compared*100:.1f}%)")
        print(f"  Both blank           : {n_both_blank:,}  ({n_both_blank/total_compared*100:.1f}%)")
        print(f"  SAE has value, ENR blank : {n_sae_only:,}")
        print(f"  ENR has value, SAE blank : {n_enr_only:,}")
        print(f"  CONFLICT (both non-blank, differ) : {n_conflict:,}  ({n_conflict/total_compared*100:.1f}%)")

        if data["conflict_patterns"]:
            print()
            print("  Top conflict patterns (SAE -> ENR):")
            for (sv, ev), cnt in data["conflict_patterns"][:20]:
                sv_disp = sv if len(sv) <= 45 else sv[:42] + "..."
                ev_disp = ev if len(ev) <= 45 else ev[:42] + "..."
                print(f"    {cnt:4d}  {sv_disp!r:50s} -> {ev_disp!r}")
        print()


def write_conflict_csv(results: dict, output_path: str) -> None:
    rows = []
    for field, data in results["fields"].items():
        for r in data["conflicts"]:
            rows.append({
                "field": field,
                "catalogue_num": r["catalogue_num"],
                "SAE_value": r["SAE"],
                "ENR_value": r["ENR"],
            })
    if rows:
        df = pd.DataFrame(rows, columns=["field", "catalogue_num", "SAE_value", "ENR_value"])
        df.sort_values(["field", "catalogue_num"], inplace=True)
        df.to_csv(output_path, index=False)
        print(f"Conflict rows written to: {output_path}  ({len(df):,} rows)")
    else:
        print("No conflicts found — no conflict CSV written.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compare SAE library vs enriched library.")
    parser.add_argument("--sae", default="implant_library_final_SAE_May.csv",
                        help="Path to SAE CSV file.")
    parser.add_argument("--enr", default=None,
                        help="Path to enriched library CSV/XLSX. Auto-detects latest if omitted.")
    parser.add_argument("--output", default="outputs/library_comparison_conflicts.csv",
                        help="Path for conflict detail CSV output.")
    args = parser.parse_args()

    sae_path = args.sae
    enr_path = args.enr or auto_detect_enr()

    print(f"Loading SAE  : {sae_path}")
    sae_raw = load_file(sae_path)

    print(f"Loading ENR  : {enr_path}")
    enr_raw = load_file(enr_path)

    # Deduplicate on catalogue_num (keep first occurrence)
    sae_d = sae_raw.drop_duplicates("catalogue_num").set_index("catalogue_num")
    enr_d = enr_raw.drop_duplicates("catalogue_num").set_index("catalogue_num")

    results = analyse(sae_d, enr_d)

    print_report(results, sae_path, enr_path)

    os.makedirs(Path(args.output).parent, exist_ok=True)
    write_conflict_csv(results, args.output)


if __name__ == "__main__":
    main()
