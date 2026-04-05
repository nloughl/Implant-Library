"""
Optimized FDA GUDID Product Code Downloader
Uses the GUDID custom query download to get all devices for specific product codes.

Download endpoint (GET, returns ZIP containing pipe-delimited .txt):
  https://accessgudid.nlm.nih.gov/download/query.zip
  Parameters:
    option=device.productCodes.fdaProductCode.productCode
    value=JWH

One request per product code — no pagination needed.
"""

import io
import json
import zipfile
import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            f'gudid_bulk_download_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class ProductCodeBulkDownloader:
    """
    Download devices from FDA GUDID using product code bulk download feature

    Much faster than individual device lookups:
    - Single request per product code
    - Gets ALL devices for that code
    - No pagination needed
    """

    DOWNLOAD_URL = "https://accessgudid.nlm.nih.gov/download/query.zip"
    QUERY_PAGE_URL = "https://accessgudid.nlm.nih.gov/download/query"

    # Knee implant product codes
    KNEE_PRODUCT_CODES = {
        "HRY": "Femorotibial, Semi-Constrained, Cemented",
        "KRR": "Patello/Femoral, Semi-Constrained, Cemented",
        "HSX": "Femorotibial, Non-Constrained, Cemented",
        "JWH": "Patellofemorotibial, Semi-Constrained, Cemented",
        "MBH": "Patello/Femorotibial, Semi-Constrained, Uncemented",
        "NPJ": "Patellofemorotibial, Partial, Semi-Constrained",
        "NJL": "Patellofemorotibial, Semi-Constrained, Metal/Polymer",
        "KWN": "Femorotibial, Non-Constrained, Uncemented",
        "KRO": "Femorotibial, Constrained, Cemented",
        "KRP": "Patello/Femorotibial, Constrained, Cemented",
        "KRQ": "Patello/Femoral, Constrained, Cemented",
    }

    def __init__(self, output_dir: str = "gudid_downloads"):
        """Initialize downloader"""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = requests.Session()

    def download_product_code(self, product_code: str) -> pd.DataFrame:
        """
        Download all devices for a specific product code.

        Issues one GET request to the GUDID query download endpoint which returns
        a ZIP containing a pipe-delimited text file with all matching devices.

        Args:
            product_code: FDA product code (e.g., 'JWH')

        Returns:
            DataFrame with all devices for that code
        """
        logger.info(f"\nDownloading product code: {product_code}")
        logger.info(
            f"  Description: {self.KNEE_PRODUCT_CODES.get(product_code, 'Unknown')}"
        )

        params = {
            "option": "device.productCodes.fdaProductCode.productCode",
            "value": product_code,
        }

        try:
            response = self.session.get(
                self.DOWNLOAD_URL,
                params=params,
                headers={"Referer": self.QUERY_PAGE_URL},
                timeout=120,
            )
            response.raise_for_status()

            # Response is a ZIP containing a pipe-delimited .txt file
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                txt_name = zf.namelist()[0]
                with zf.open(txt_name) as f:
                    content = f.read().decode("utf-8-sig")  # strip BOM if present

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 503:
                logger.warning(
                    f"Bulk endpoint returned 503 for {product_code} "
                    f"(< 10 000 devices) — falling back to browser download"
                )
                return self._download_product_code_browser(product_code)
            logger.error(f"Failed to download {product_code}: {e}")
            return pd.DataFrame()
        except requests.RequestException as e:
            logger.error(f"Failed to download {product_code}: {e}")
            return pd.DataFrame()
        except (zipfile.BadZipFile, KeyError) as e:
            logger.error(f"Failed to parse ZIP for {product_code}: {e}")
            return pd.DataFrame()

        lines = content.strip().splitlines()
        if len(lines) < 2:
            logger.warning(f"No data returned for {product_code}")
            return pd.DataFrame()

        header = [h.strip('"').strip() for h in lines[0].split("|")]
        records = []
        for line in lines[1:]:
            if not line.strip():
                continue
            values = [v.strip().strip('"') for v in line.split("|")]
            if len(values) == len(header):
                records.append(dict(zip(header, values)))

        df = pd.DataFrame(records)
        logger.info(f"[OK] Downloaded {len(df):,} devices for {product_code}")

        # Save raw pipe-delimited file
        output_file = self.output_dir / f"product_code_{product_code}.txt"
        output_file.write_text(content, encoding="utf-8")
        logger.info(f"  Saved to: {output_file}")

        return df

    def _download_product_code_browser(self, product_code: str) -> pd.DataFrame:
        """
        Fallback for codes that fail the bulk ZIP endpoint (503 = < 10 000 devices).

        Uses a headless Playwright browser to:
          1. Navigate to the GUDID search page (establishes session cookie)
          2. Read the data-se-url from #export-worker-link — this is the
             Elasticsearch export endpoint with the product-code filter baked in
          3. Paginate through all pages of the ES JSON response
          4. Map _source fields to pipeline ALL_CAPS column names
          5. Populate the description cache so the enricher skips these devices

        The es_export endpoint returns Elasticsearch JSON:
          {"hits": {"total": {"value": N}, "hits": [{"_source": {...}}, ...]}}

        Requires:
          pip install playwright
          playwright install chromium
        """
        try:
            from playwright.sync_api import (
                sync_playwright,
                TimeoutError as PlaywrightTimeout,
            )
        except ImportError:
            logger.error(
                "playwright not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
            return pd.DataFrame()

        def _set_page(url: str, pg: int) -> str:
            """Return url with page= query param set to pg."""
            parsed = urlparse(url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params["page"] = [str(pg)]
            return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

        search_url = (
            "https://accessgudid.nlm.nih.gov/devices/search"
            f"?filters%5BFDA+Product+Code%5D%5B%5D={product_code}"
            f"&page=1&query={product_code}"
        )

        logger.info(f"  Browser download for {product_code}: {search_url}")

        all_records: list = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            try:
                page.goto(search_url, wait_until="networkidle", timeout=60_000)
            except Exception as exc:
                logger.warning(f"  Page load failed for {product_code}: {exc}")
                browser.close()
                return pd.DataFrame()

            # state="attached" — only needs to exist in DOM (hidden inside dropdown)
            try:
                page.wait_for_selector(
                    "#export-worker-link", state="attached", timeout=30_000
                )
            except PlaywrightTimeout as exc:
                logger.warning(
                    f"  Search results did not load for {product_code}: {exc}"
                )
                browser.close()
                return pd.DataFrame()

            se_url = page.get_attribute("#export-worker-link", "data-se-url") or ""
            if not se_url:
                logger.warning(f"  data-se-url attribute empty for {product_code}")
                browser.close()
                return pd.DataFrame()
            if se_url.startswith("/"):
                se_url = "https://accessgudid.nlm.nih.gov" + se_url

            # Inject page_size=100 so we get 100 hits/page instead of 10
            parsed_se = urlparse(se_url)
            se_params = parse_qs(parsed_se.query, keep_blank_values=True)
            se_params["page_size"] = ["100"]
            se_url = urlunparse(
                parsed_se._replace(query=urlencode(se_params, doseq=True))
            )
            logger.info(f"  Export URL (base): {se_url}")

            # ── Paginate through all ES pages ──────────────────────────────
            pg = 1
            total_expected = None

            while True:
                url = _set_page(se_url, pg)
                try:
                    resp = context.request.get(
                        url,
                        headers={"Referer": search_url},
                        timeout=120_000,
                    )
                except Exception as exc:
                    logger.warning(
                        f"  Page {pg} request failed for {product_code}: {exc}"
                    )
                    break

                if not resp.ok:
                    logger.warning(
                        f"  Page {pg}: HTTP {resp.status} for {product_code}"
                    )
                    break

                try:
                    data = json.loads(resp.body())
                except Exception as exc:
                    body_preview = resp.body()[:200]
                    logger.warning(
                        f"  Page {pg}: JSON parse error for {product_code}: {exc}. "
                        f"Preview: {body_preview!r}"
                    )
                    break

                hits_wrapper = data.get("hits", {})
                if total_expected is None:
                    total_expected = hits_wrapper.get("total", {}).get("value", 0)
                hits = hits_wrapper.get("hits", [])

                if not hits:
                    break

                for hit in hits:
                    all_records.append(
                        self._parse_es_source(hit.get("_source", {}), product_code)
                    )

                logger.info(
                    f"  {product_code}: page {pg} — {len(hits)} hits "
                    f"({len(all_records)} / {total_expected} total)"
                )

                if len(all_records) >= (total_expected or 0):
                    break

                pg += 1
                time.sleep(0.3)

            browser.close()

        if not all_records:
            logger.warning(f"  No records fetched for {product_code}")
            return pd.DataFrame()

        df = pd.DataFrame(all_records).fillna("")
        logger.info(f"[OK] ES export: {len(df):,} devices for {product_code}")

        # ── Populate description cache ─────────────────────────────────────
        cache_path = self.output_dir / "device_descriptions_cache.json"
        cache = {}
        if cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass
        added = 0
        for _, row in df.iterrows():
            di = row.get("DEVICE_ID", "")
            desc = row.get("DEVICE_DESCRIPTION", "")
            sizes = row.get("DEVICE_SIZES_TEXT", "")
            if di and (desc or sizes) and di not in cache:
                cache[di] = {"device_description": desc, "device_sizes_text": sizes}
                added += 1
        if added:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            logger.info(f"  Description cache: added {added} entries -> {cache_path}")

        flat_path = self.output_dir / f"product_code_{product_code}.csv"
        df.to_csv(flat_path, index=False)
        logger.info(f"  Saved to: {flat_path}")

        return df

    def _parse_es_source(self, src: dict, fallback_product_code: str) -> dict:
        """
        Map one _source entry from the es_export Elasticsearch response to
        the pipeline's ALL_CAPS column convention.
        """
        # deviceId is nested: identifiers.identifier (dict or list of dicts)
        _idents = (src.get("identifiers") or {}).get("identifier") or {}
        if isinstance(_idents, list):
            # Multiple identifiers — use the Primary one
            _primary = next(
                (
                    i
                    for i in _idents
                    if isinstance(i, dict) and i.get("deviceIdType") == "Primary"
                ),
                _idents[0] if _idents else {},
            )
            di = (_primary or {}).get("deviceId", "")
        elif isinstance(_idents, dict):
            di = _idents.get("deviceId", "")
        else:
            di = src.get("PrimaryDI") or src.get("deviceId") or ""

        # Product codes (nested dict or list depending on ES mapping)
        pc_raw = src.get("productCodes") or []
        if isinstance(pc_raw, dict):
            codes = pc_raw.get("fdaProductCode", [])
        elif isinstance(pc_raw, list):
            codes = pc_raw
        else:
            codes = []
        pc_str = " | ".join(
            c.get("productCode", "")
            for c in codes
            if isinstance(c, dict) and c.get("productCode")
        )
        pc_desc = " | ".join(
            c.get("productCodeName", "")
            for c in codes
            if isinstance(c, dict) and c.get("productCodeName")
        )

        # GMDN terms — gmdn may be a single dict or a list of dicts
        gmdn_inner = (src.get("gmdnTerms") or {}).get("gmdn") or []
        if isinstance(gmdn_inner, dict):
            gmdn_list = [gmdn_inner]
        elif isinstance(gmdn_inner, list):
            gmdn_list = gmdn_inner
        else:
            gmdn_list = []
        gmdn_str = " | ".join(
            g.get("gmdnPTName", "")
            for g in gmdn_list
            if isinstance(g, dict) and g.get("gmdnPTName")
        )

        # Device sizes — deviceSize may be a single dict or a list of dicts
        size_inner = (src.get("deviceSizes") or {}).get("deviceSize") or []
        if isinstance(size_inner, dict):
            size_list = [size_inner]
        elif isinstance(size_inner, list):
            size_list = size_inner
        else:
            size_list = []
        size_parts = []
        for s in size_list:
            if not isinstance(s, dict):
                continue
            stype = s.get("sizeType", "")
            stext = s.get("sizeText", "")
            nested = s.get("size") or {}
            sval = nested.get("value", "")
            sunit = nested.get("unit", "")
            if stext:
                size_parts.append(f"{stype}: {stext}".strip(": "))
            elif sval:
                size_parts.append(
                    f"{stype}: {sval}{' ' + sunit if sunit else ''}".strip(": ")
                )

        return {
            "DEVICE_ID": di,
            "BRAND_NAME": src.get("brandName", ""),
            "VERSION_MODEL_NUMBER": src.get("versionModelNumber", ""),
            "CATALOG_NUMBER": src.get("catalogNumber", ""),
            "COMPANY_NAME": src.get("companyName", ""),
            "DEVICE_DESCRIPTION": src.get("deviceDescription", ""),
            "MRI_SAFETY_STATUS": src.get("MRISafetyStatus", ""),
            "LABELED_CONTAINS_NRL": src.get("labeledContainsNRL", ""),
            "GMDN_TERMS": gmdn_str,
            "DEVICE_SIZES_TEXT": " | ".join(size_parts),
            "product_code": pc_str or fallback_product_code,
            "product_code_description": pc_desc
            or self.KNEE_PRODUCT_CODES.get(fallback_product_code, ""),
        }

    @staticmethod
    def parse_webpage_download_folder(
        folder_path: str,
        description_cache_path: str = "gudid_downloads/device_descriptions_cache.json",
    ) -> pd.DataFrame:
        """
        Parse a GUDID webpage download folder (the ZIP extracted from
        accessgudid.nlm.nih.gov -> Search -> Export -> Delimited .TXT Files).

        The folder must contain at minimum device.txt and productCodes.txt.
        gmdnTerms.txt, identifiers.txt, and deviceSizes.txt are used when present.

        Returns a DataFrame with the same column conventions as the bulk
        download (ALL_CAPS) so it drops in as a replacement in the pipeline.

        Also writes deviceDescription values into the description cache so
        the GUDID API enrichment step skips these devices.
        """
        folder = Path(folder_path)

        # ── device.txt (required) ──────────────────────────────────────────
        dev = pd.read_csv(folder / "device.txt", sep="|", dtype=str).fillna("")

        # ── productCodes.txt -> aggregate all codes per PrimaryDI ──────────
        pc = pd.read_csv(folder / "productCodes.txt", sep="|", dtype=str).fillna("")
        codes_agg = (
            pc.groupby("PrimaryDI")
            .apply(
                lambda g: pd.Series(
                    {
                        "product_code": " | ".join(g["productCode"].unique()),
                        "product_code_description": " | ".join(
                            g["productCodeName"].unique()
                        ),
                    }
                ),
                include_groups=False,
            )
            .reset_index()
        )

        # ── gmdnTerms.txt -> aggregate term names per PrimaryDI ────────────
        gmdn_col = pd.Series(dtype=str)
        gmdn_path = folder / "gmdnTerms.txt"
        if gmdn_path.exists():
            gmdn = pd.read_csv(gmdn_path, sep="|", dtype=str).fillna("")
            gmdn_col = (
                gmdn.groupby("PrimaryDI")["gmdnPTName"]
                .apply(lambda x: " | ".join(x.unique()))
                .rename("GMDN_TERMS")
                .reset_index()
            )
        else:
            gmdn_col = pd.DataFrame(columns=["PrimaryDI", "GMDN_TERMS"])

        # ── identifiers.txt -> GTIN (deviceIdIssuingAgency == GS1) ─────────
        gtin_col = pd.DataFrame(columns=["PrimaryDI", "DEVICE_ID_ISSUING_AGENCY"])
        id_path = folder / "identifiers.txt"
        if id_path.exists():
            idents = pd.read_csv(id_path, sep="|", dtype=str).fillna("")
            primary = idents[idents["deviceIdType"] == "Primary"].copy()
            gtin_col = primary[["PrimaryDI", "deviceIdIssuingAgency"]].rename(
                columns={"deviceIdIssuingAgency": "DEVICE_ID_ISSUING_AGENCY"}
            )

        # ── deviceSizes.txt -> aggregate size text per PrimaryDI ───────────
        sizes_col = pd.DataFrame(columns=["PrimaryDI", "DEVICE_SIZES_TEXT"])
        sz_path = folder / "deviceSizes.txt"
        if sz_path.exists():
            sz = pd.read_csv(sz_path, sep="|", dtype=str).fillna("")

            def _fmt_size_row(row):
                stype = row.get("sizeType", "")
                stext = row.get("sizeText", "")
                sval = row.get("size (Value)", "")
                sunit = row.get("size (Unit)", "")
                if stext:
                    return f"{stype}: {stext}".strip(": ")
                if sval:
                    return f"{stype}: {sval}{' ' + sunit if sunit else ''}".strip(": ")
                return ""

            sz["_part"] = sz.apply(_fmt_size_row, axis=1)
            sizes_col = (
                sz[sz["_part"] != ""]
                .groupby("PrimaryDI")["_part"]
                .apply(lambda x: " | ".join(x))
                .rename("DEVICE_SIZES_TEXT")
                .reset_index()
            )

        # ── Join everything ────────────────────────────────────────────────
        df = (
            dev.merge(codes_agg, on="PrimaryDI", how="left")
            .merge(gmdn_col, on="PrimaryDI", how="left")
            .merge(gtin_col, on="PrimaryDI", how="left")
            .merge(sizes_col, on="PrimaryDI", how="left")
        )

        # ── Rename to pipeline ALL_CAPS convention ─────────────────────────
        df = df.rename(
            columns={
                "PrimaryDI": "DEVICE_ID",
                "brandName": "BRAND_NAME",
                "versionModelNumber": "VERSION_MODEL_NUMBER",
                "catalogNumber": "CATALOG_NUMBER",
                "companyName": "COMPANY_NAME",
                "deviceDescription": "DEVICE_DESCRIPTION",
                "MRISafetyStatus": "MRI_SAFETY_STATUS",
                "labeledContainsNRL": "LABELED_CONTAINS_NRL",
            }
        )
        df = df.fillna("")

        logger.info(
            "Parsed webpage download: %d devices, %d with product codes, "
            "%d with GMDN, %d with sizes",
            len(df),
            (df["product_code"] != "").sum(),
            (df.get("GMDN_TERMS", pd.Series(dtype=str)) != "").sum(),
            (df.get("DEVICE_SIZES_TEXT", pd.Series(dtype=str)) != "").sum(),
        )

        # ── Populate description cache so the API enrichment skips these ──
        if description_cache_path:
            cache_path = Path(description_cache_path)
            if cache_path.exists():
                import json

                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
            else:
                cache = {}

            added = 0
            for _, row in df.iterrows():
                di = row.get("DEVICE_ID", "")
                desc = row.get("DEVICE_DESCRIPTION", "")
                sizes = row.get("DEVICE_SIZES_TEXT", "")
                if di and (desc or sizes) and di not in cache:
                    cache[di] = {
                        "device_description": desc,
                        "device_sizes_text": sizes,
                    }
                    added += 1

            if added:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                logger.info(
                    "Description cache: added %d new entries from webpage download "
                    "-> %s",
                    added,
                    cache_path,
                )

        return df

    def download_all_knee_codes(self) -> pd.DataFrame:
        """
        Download all knee implant product codes and combine

        Returns:
            Combined DataFrame with all knee devices
        """
        logger.info("\n" + "=" * 80)
        logger.info("FDA GUDID BULK DOWNLOAD - KNEE IMPLANT PRODUCT CODES")
        logger.info("=" * 80)
        logger.info(f"\nDownloading {len(self.KNEE_PRODUCT_CODES)} product codes...")

        all_devices = []
        stats = {}

        for product_code in self.KNEE_PRODUCT_CODES.keys():
            df = self.download_product_code(product_code)

            if len(df) > 0:
                # Search-export fallback already sets product_code / product_code_description
                # (and may carry ALL_CAPS columns).  Only overwrite when missing.
                if "product_code" not in df.columns or df["product_code"].eq("").all():
                    df["product_code"] = product_code
                if (
                    "product_code_description" not in df.columns
                    or df["product_code_description"].eq("").all()
                ):
                    df["product_code_description"] = self.KNEE_PRODUCT_CODES[
                        product_code
                    ]
                all_devices.append(df)
                stats[product_code] = len(df)

            # Be respectful - small delay between requests
            time.sleep(1)

        # Combine all — deliberately keep one row per product-code per device.
        # The same device may appear under multiple codes (e.g. JWH and NJL);
        # each row carries that code's description and will be enriched
        # individually by the pipeline.  _merge_catalogue_group() in
        # implant_lib_v3 does the final merge after enrichment.
        if all_devices:
            combined_df = pd.concat(all_devices, ignore_index=True)
        else:
            logger.error("No devices downloaded!")
            return pd.DataFrame()

        # Save combined file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = self.output_dir / f"knee_devices_bulk_{timestamp}.csv"
        combined_df.to_csv(output_file, index=False)

        logger.info("\n" + "=" * 80)
        logger.info("DOWNLOAD COMPLETE")
        logger.info("=" * 80)
        logger.info(f"\n[OK] Total devices: {len(combined_df):,}")
        logger.info(f"[OK] Output file: {output_file}")

        # Statistics
        logger.info("\nDevices per product code:")
        for code, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            desc = self.KNEE_PRODUCT_CODES[code]
            logger.info(f"  {code}: {count:6,} - {desc}")

        return combined_df

    def apply_gmdn_filtering(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply GMDN filtering to remove trials, instruments, etc.

        Args:
            df: DataFrame with GMDN_TERMS column

        Returns:
            Filtered DataFrame
        """
        logger.info("\n" + "=" * 80)
        logger.info("GMDN FILTERING")
        logger.info("=" * 80)

        initial_count = len(df)

        # Exclusion keywords (trials, instruments, etc.)
        EXCLUDE_KEYWORDS = [
            "trial",
            "instrument",
            "screw",
            "pin",
            "wire",
            "drill",
            "saw",
            "rasp",
            "reamer",
            "guide",
            "template",
            "impactor",
            "extractor",
            "inserter",
            "positioning",
            "alignment",
            "cutting",
            "removal",
            "extraction",
            "wedge",
            "bone awl",
            "hand-held",
            "analyser",
            "reusable",
            "chisel",
            "aiming",
            "rongeur",
            "craniofacial",
            "retractor",
            "saliva",
            "pliers",
            "guage",
            "mould",
            "wrench",
        ]

        # Create exclusion mask
        exclude_mask = pd.Series([False] * len(df))

        for keyword in EXCLUDE_KEYWORDS:
            keyword_mask = df["GMDN_TERMS"].str.contains(keyword, case=False, na=False)
            exclude_mask |= keyword_mask

            excluded_count = keyword_mask.sum()
            if excluded_count > 0:
                logger.info(f"  Excluding '{keyword}': {excluded_count:,} devices")

        # Filter out excluded devices
        filtered_df = df[~exclude_mask].copy()

        excluded_total = initial_count - len(filtered_df)
        logger.info(f"\n[OK] Filtered out {excluded_total:,} non-implant devices")
        logger.info(f"[OK] Remaining: {len(filtered_df):,} actual implants")

        return filtered_df


def compare_methods():
    """
    Compare old vs new download methods
    """

    print("\nMETHOD (GUDID query.zip bulk download):")
    print("  - URL: /download/query.zip?option=...productCode&value=JWH")
    print("  - 11 requests total, one per product code, returns full ZIP")
    print("  - No pagination, no rate limit concerns")
    print("  TIME: ~1-2 minutes")
    print("  BENEFIT: One request per code, pipe-delimited CSV in ZIP")

    print("\nIMPROVEMENT:")
    print("  - Data volume: only relevant devices")
    print("  - Requests: 75,967 -> 11 (one per product code)")
    print("  - Simpler pipeline: no pre-filtering step needed")


def main():
    """Main execution"""

    print("\n" + "=" * 80)
    print("OPTIMIZED FDA GUDID BULK DOWNLOADER")
    print("=" * 80)
    print("\nThis method uses FDA's product code bulk download feature.")
    print("Much faster than individual device API calls!")
    print("=" * 80)

    # Show comparison
    compare_methods()

    # Run bulk download
    downloader = ProductCodeBulkDownloader(output_dir="gudid_downloads")

    logger.info("\n\nStarting bulk download...")
    df = downloader.download_all_knee_codes()

    if len(df) == 0:
        logger.error("Download failed!")
        return

    # Apply GMDN filtering to remove trials/instruments
    filtered_df = downloader.apply_gmdn_filtering(df)

    # Save filtered version
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path("gudid_downloads") / f"knee_implants_filtered_{timestamp}.csv"
    filtered_df.to_csv(output_file, index=False)

    logger.info("\n" + "=" * 80)
    logger.info("FINAL RESULTS")
    logger.info("=" * 80)
    logger.info(f"[OK] Total devices downloaded: {len(df):,}")
    logger.info(f"[OK] After GMDN filtering: {len(filtered_df):,}")
    logger.info(f"[OK] Output file: {output_file}")

    # Column info
    logger.info("\nAvailable columns:")
    for col in filtered_df.columns:
        logger.info(f"  - {col}")

    return filtered_df


if __name__ == "__main__":
    df = main()
