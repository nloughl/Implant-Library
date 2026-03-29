"""
eIFU Downloader
===============

Downloads electronic Instructions for Use (eIFU) PDFs from manufacturer
labeling portals for knee implant systems.

Supported manufacturers
-----------------------
  Stryker     https://labeling.stryker.com/hcp/CA         [VERIFIED]
  Zimmer      https://docs.zimmerbiomet.com/hcp/zimmer/CA/all  [needs selector check]

  DePuy: only 16 eIFUs total — download manually from https://www.e-ifu.com/

File naming convention
----------------------
  {manufacturer}_{system_name}_{doc_id}.pdf

  Examples:
    stryker_triathlon_QIN4396EIFU.pdf     <- main Triathlon eIFU
    stryker_triathlon_QIN4397EIFU.pdf     <- second eIFU for same system (cementless, etc.)
    zimmer_persona_eIFU.pdf

  Rationale:
    - Manufacturer prefix routes to the correct parser in eifu_material_extractor.py
    - System name identifies the implant family (one PDF covers many catalogue numbers,
      e.g. all Triathlon femoral sizes)
    - doc_id (QIN / document reference) allows multiple eIFUs per system and
      deduplication on re-runs

Multiple results per system
---------------------------
  Searching "Triathlon" returns several documents:
    QIN 4396-EIFU   <- download this  (Instructions for Use)
    QIN 4396-ST     <- skip           (Surgical Technique)
    QIN 4396-PLAN   <- skip           (Planning Guide)

  We collect ALL document identifiers from the results page, filter to keep only
  eIFU-type documents (using NON_EIFU_PATTERNS below), then download each one.
  Each gets its own canonical filename using the QIN/doc-id as the suffix so
  multiple eIFUs for the same system coexist on disk.

  The eifu_material_extractor.py handles "one PDF covers many catalogue numbers"
  by extracting all catalogue families from the document.

Pipeline integration
--------------------
  Used by implant_lib_v3.py to fill gaps in material data:

    from eifu_downloader import EIFUDownloadOrchestrator
    orch = EIFUDownloadOrchestrator(eifu_dir="implant_eIFUs")
    orch.download_missing_for_dataframe(cjrr_df)

Usage (standalone)
------------------
  python eifu_downloader.py                              # all supported manufacturers
  python eifu_downloader.py --manufacturer Stryker
  python eifu_downloader.py --manufacturer Zimmer
  python eifu_downloader.py --manufacturer Stryker --search "5510-F-101"  # cat number
  python eifu_downloader.py --headless                  # no visible browser
"""

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(
            f'eifu_download_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Document type filtering
# Documents whose titles/QIN suffixes match these patterns are NOT eIFUs and
# should be skipped even if they appear in eIFU search results.
# ---------------------------------------------------------------------------

NON_EIFU_PATTERNS = [
    r'-ST\b',               # QIN 4396-ST  (Surgical Technique)
    r'-OT\b',               # Operative Technique
    r'-PLAN\b',             # Planning Guide
    r'-SG\b',               # Sizing Guide
    r'-QR\b',               # Quick Reference
    r'-ADD\b',              # Addendum
    r'-SUPP\b',             # Supplement
    r'\btechnique\b',       # Surgical / Operative / Cementing Technique, etc.
    r'planning\s+guide',
    r'sizing\s+guide',
    r'quick\s+reference',
    r'surgeon\s+guide',
    r'instrument',
    r'addendum',
    r'supplement',
    r'pre-?op\b',
    r'pre-?operative',
]

_NON_EIFU_RE = re.compile('|'.join(NON_EIFU_PATTERNS), re.IGNORECASE)


def _is_eifu_document(title: str) -> bool:
    """Return True if document title looks like an eIFU (not a surgical technique, etc.)."""
    return not bool(_NON_EIFU_RE.search(title))


# ---------------------------------------------------------------------------
# Knee implant systems by manufacturer
# Maps   search_term -> canonical_system_name (used in filename)
# ---------------------------------------------------------------------------

STRYKER_SYSTEMS: Dict[str, str] = {
    # Keys are catalogue numbers (portal only accepts cat numbers, not system names)
    # Values are canonical system names used in filenames
    '5510-F-101': 'triathlon',   # Triathlon CR Femoral Component (size 1)
    '72-26-0308': 'scorpio',     # Scorpio X3 CR Tibial Insert
    '6478-6-600': 'duracon',     # Duracon Fluted Stem
    '6476-8-250': 'kinemax',     # Kinemax Plus Baseplate Stem Extender
}

ZIMMER_SYSTEMS: Dict[str, str] = {
    'Persona':          'persona',
    'NexGen':           'nexgen',
    'Vanguard':         'vanguard',
    'Gender Solutions': 'gender_solutions',
    'AGC':              'agc',
}


# ---------------------------------------------------------------------------
# File naming helpers
# ---------------------------------------------------------------------------

def eifu_filename(manufacturer: str, system: str, doc_id: str = '') -> str:
    """
    Build a canonical eIFU filename.

    Parameters
    ----------
    manufacturer : 'stryker' | 'zimmer'
    system       : normalised system name, e.g. 'triathlon'
    doc_id       : optional document identifier (QIN number, etc.)

    Returns e.g. 'stryker_triathlon_QIN4396EIFU.pdf'
                 'zimmer_persona_eIFU.pdf'
    """
    mfr = re.sub(r'[^a-z0-9]', '', manufacturer.lower())
    sys = re.sub(r'[^a-z0-9]', '_', system.lower()).strip('_')
    if doc_id:
        did = re.sub(r'[^A-Za-z0-9]', '', doc_id)
        return f'{mfr}_{sys}_{did}.pdf'
    return f'{mfr}_{sys}_eIFU.pdf'


def parse_eifu_filename(path: Path) -> Tuple[str, str]:
    """
    Extract (manufacturer, system) from a filename produced by `eifu_filename`.
    Returns ('', '') if the name does not match the convention.
    """
    stem = path.stem  # e.g. 'stryker_triathlon_QIN4396EIFU'
    parts = stem.split('_', 2)
    if len(parts) >= 2:
        return parts[0], parts[1]
    return '', ''


# ---------------------------------------------------------------------------
# Data class for results
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    manufacturer: str
    system: str
    search_term: str
    filename: str
    path: Optional[Path] = None
    success: bool = False
    error: str = ''
    doc_id: str = ''


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class BaseEIFUScraper:
    """Shared Selenium infrastructure for all manufacturer scrapers."""

    def __init__(self, eifu_dir: str = 'implant_eIFUs', headless: bool = False):
        self.eifu_dir = Path(eifu_dir)
        self.eifu_dir.mkdir(exist_ok=True)
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None

    def start(self):
        opts = Options()
        if self.headless:
            opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=1700,1000')
        opts.add_experimental_option('prefs', {
            'download.default_directory': str(self.eifu_dir.absolute()),
            'download.prompt_for_download': False,
            'download.directory_upgrade': True,
            'safebrowsing.enabled': True,
            'plugins.always_open_pdf_externally': True,
        })
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=opts)
        logger.info('Chrome started (headless=%s)', self.headless)

    def stop(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _wait(self, by: By, value: str, timeout: int = 10):
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        except TimeoutException:
            return None

    def _click(self, element, retries: int = 3) -> bool:
        for _ in range(retries):
            try:
                element.click()
                return True
            except (ElementClickInterceptedException, StaleElementReferenceException):
                time.sleep(1)
        return False

    def _wait_for_new_pdf(self, before: set, timeout: int = 45) -> Optional[Path]:
        """Poll eifu_dir until a new .pdf appears (not a .crdownload partial)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = {
                p for p in self.eifu_dir.glob('*.pdf')
                if not p.name.endswith('.crdownload')
            }
            new = current - before
            if new:
                return next(iter(new))
            time.sleep(1)
        return None

    def _existing_pdfs(self) -> set:
        return set(self.eifu_dir.glob('*.pdf'))

    def already_downloaded(self, filename: str) -> bool:
        return (self.eifu_dir / filename).exists()

    # ------------------------------------------------------------------
    # Multi-result collection helpers (shared by Stryker + Zimmer)
    # ------------------------------------------------------------------

    def _collect_eifu_doc_ids(self) -> List[Tuple[str, str]]:
        """
        Scan the current search results page and return a list of
        (doc_id, display_title) tuples for all eIFU-type documents.

        Filters out surgical techniques, planning guides, etc. using
        NON_EIFU_PATTERNS.

        Strategy: look for text nodes containing 'EIFU' or 'eIFU' or
        'Instructions for Use', extract the nearest QIN-style identifier,
        and exclude any that match NON_EIFU_PATTERNS.
        """
        soup = BeautifulSoup(self.driver.page_source, 'html.parser')
        found: List[Tuple[str, str]] = []
        seen_ids: set = set()

        # Collect candidates (anything with an eIFU indicator, not a non-eIFU type)
        candidates: List[str] = []
        for el in soup.find_all(string=True):
            text = str(el).strip()
            if not text:
                continue
            if not re.search(r'EIFU|eIFU|Instructions\s+for\s+Use', text, re.IGNORECASE):
                continue
            if not _is_eifu_document(text):
                continue
            candidates.append(text)

        # Log all candidates so we can see the actual document ID format if nothing matches
        if candidates:
            logger.debug('eIFU candidates before ID extraction (%d): %s',
                         len(candidates), candidates)

        for text in candidates:
            # Try QIN format first (Stryker: "QIN 4396-EIFU")
            qin_match = re.search(r'QIN\s*[\d]+[-\w]*', text)
            if qin_match:
                doc_id = qin_match.group(0).replace(' ', '')
            else:
                # Log at INFO level so the user can see what format IS present
                logger.info('No QIN in candidate (may be different ID format): %r', text[:120])
                continue

            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)
            found.append((doc_id, text))

        logger.info('Found %d eIFU document(s) in results: %s',
                    len(found), [d for d, _ in found])
        return found

    def _download_one_accordion(
        self,
        doc_id: str,
        system_name: str,
        before_pdfs: set,
    ) -> DownloadResult:
        """
        For the Stryker Veeva Vault portal only.
        Find the accordion for doc_id, expand it, click English, confirm.

        Returns a DownloadResult.  Caller should update before_pdfs after
        a successful download so the next call doesn't pick up the same file.
        """
        filename = eifu_filename(self.MANUFACTURER, system_name, doc_id)
        result = DownloadResult(
            manufacturer=self.MANUFACTURER,
            system=system_name,
            search_term=doc_id,
            filename=filename,
            doc_id=doc_id,
        )

        if self.already_downloaded(filename):
            logger.info('[skip] %s already exists', filename)
            result.success = True
            result.path = self.eifu_dir / filename
            return result

        try:
            # ---- Find & click the specific accordion for this doc_id ----
            accordion_clicked = False
            # Target the element that contains doc_id text and is clickable
            for xpath in [
                f"//*[contains(text(),'{doc_id}')]",
                f"//*[contains(normalize-space(),'{doc_id.replace('QIN', 'QIN ')}')]",
            ]:
                try:
                    els = self.driver.find_elements(By.XPATH, xpath)
                    for el in els:
                        # Walk up to accordion container
                        for ancestor_xpath in [
                            "./ancestor::*[contains(@class,'accordion') or contains(@class,'Accordion')]",
                            "./ancestor::*[contains(@class,'document') or contains(@class,'Document')]",
                            "./ancestor::*[contains(@class,'item') or contains(@class,'result')]",
                            "./..",  # direct parent as fallback
                        ]:
                            try:
                                container = el.find_element(By.XPATH, ancestor_xpath)
                                if self._click(container):
                                    accordion_clicked = True
                                    time.sleep(3)
                                    break
                            except NoSuchElementException:
                                continue
                        if accordion_clicked:
                            break
                    if accordion_clicked:
                        break
                except Exception:
                    continue

            if not accordion_clicked:
                result.error = f'Accordion not found for {doc_id}'
                return result

            # ---- Click English language ----------------------------------
            time.sleep(2)
            english_clicked = False
            for xpath in [
                "//*[contains(text(),'English/en') and not(ancestor::*[contains(@style,'display: none')])]",
                "//*[contains(text(),'English') and contains(text(),'/en')]",
                "//*[@lang='en' or @lang='EN']",
            ]:
                try:
                    for el in self.driver.find_elements(By.XPATH, xpath):
                        if self._click(el):
                            english_clicked = True
                            time.sleep(2)
                            break
                    if english_clicked:
                        break
                except Exception:
                    continue

            if not english_clicked:
                result.error = f'English option not found for {doc_id}'
                return result

            # ---- Confirm dialog ------------------------------------------
            time.sleep(3)
            confirm_clicked = False
            for selector in [
                "button[class*='confirmButton']",
                "button.style_confirmButton__18UwC",
            ]:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if self._click(btn):
                        confirm_clicked = True
                        break
                except NoSuchElementException:
                    pass

            if not confirm_clicked:
                try:
                    btn = self.driver.find_element(
                        By.XPATH, "//button[normalize-space()='Confirm']"
                    )
                    confirm_clicked = self._click(btn)
                except NoSuchElementException:
                    pass

            if not confirm_clicked:
                result.error = f'Confirm button not found for {doc_id}'
                return result

            # ---- Wait for file, rename ----------------------------------
            downloaded = self._wait_for_new_pdf(before_pdfs)
            if downloaded:
                canonical = self.eifu_dir / filename
                downloaded.rename(canonical)
                result.path = canonical
                result.filename = canonical.name
                result.success = True
                logger.info('[OK] %s', canonical.name)
            else:
                result.error = f'Download timed out for {doc_id}'

        except Exception as exc:
            result.error = str(exc)
            logger.error('Error downloading %s: %s', doc_id, exc)

        return result


# ---------------------------------------------------------------------------
# Stryker scraper  (VERIFIED against https://labeling.stryker.com/hcp/CA)
# ---------------------------------------------------------------------------

class StrykerEIFUScraper(BaseEIFUScraper):
    """
    Downloads English eIFUs from Stryker's labeling portal.

    Workflow (confirmed via test_stryker_eifu_download.py)
    -------------------------------------------------------
    1. Navigate to https://labeling.stryker.com/hcp/CA
    2. Search by catalogue number (e.g. '5510-F-101') — portal does not accept
       system names, only catalogue numbers
    3. Wait for documentAccordion elements to appear in the search results page
       (documents are shown directly in results, no click-through to detail page)
    4. Scan for QIN numbers using _collect_eifu_doc_ids(); filter non-eIFUs
    5. For each eIFU: click its documentAccordion to expand, find English option,
       click English, click Confirm button
    6. Rename: stryker_{system}_{doc_id}.pdf
    """

    SEARCH_URL = 'https://labeling.stryker.com/hcp/CA'
    MANUFACTURER = 'stryker'

    def download_system(
        self,
        search_term: str,
        system_name: str,
    ) -> List[DownloadResult]:
        """
        Download all English eIFUs for one Stryker implant system.
        Returns a list of DownloadResult (one per document downloaded).
        """
        if not self.driver:
            self.start()

        results: List[DownloadResult] = []

        try:
            logger.info('Stryker: searching "%s"', search_term)
            self.driver.get(self.SEARCH_URL)
            time.sleep(3)

            # ---- Search -------------------------------------------------------
            search_box = self._wait(By.NAME, 'cross-field-search', timeout=15)
            if not search_box:
                search_box = self._wait(
                    By.CSS_SELECTOR,
                    "input[type='search'], input[type='text']",
                    timeout=5,
                )
            if not search_box:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='Search box not found',
                )]

            search_box.clear()
            search_box.send_keys(search_term)
            time.sleep(1)
            try:
                self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            except NoSuchElementException:
                search_box.send_keys(Keys.RETURN)

            # Wait for document accordions to appear in the results
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[class*='documentAccordion']")
                    )
                )
                time.sleep(2)
            except TimeoutException:
                logger.warning('Stryker: document results did not appear within 30 s')
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='Search results did not load',
                )]

            # ---- Collect eIFU QIN numbers from results page ------------------
            doc_ids = self._collect_eifu_doc_ids()
            if not doc_ids:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='No eIFU documents found in search results',
                )]

            # ---- Download each eIFU ------------------------------------------
            for doc_id, _ in doc_ids:
                filename = eifu_filename(self.MANUFACTURER, system_name, doc_id)
                r = DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename=filename, doc_id=doc_id,
                )

                if self.already_downloaded(filename):
                    logger.info('[skip] %s already exists', filename)
                    r.success = True
                    r.path = self.eifu_dir / filename
                    results.append(r)
                    continue

                before = self._existing_pdfs()

                try:
                    # Find the element containing the QIN text and click its
                    # documentAccordion ancestor to expand language options
                    qin_el = self.driver.find_element(
                        By.XPATH,
                        f"//*[contains(text(),'{doc_id}') or "
                        f"contains(text(),'{doc_id.replace('QIN', 'QIN ')}')]"
                    )
                    accordion = qin_el.find_element(
                        By.XPATH,
                        "./ancestor::*[contains(@class,'documentAccordion')]"
                    )
                    self._click(accordion)
                    time.sleep(3)

                    # ---- Click English language ------------------------------
                    english_clicked = False
                    for xpath in [
                        "//*[contains(text(),'English/en') and not(ancestor::*[contains(@style,'display: none')])]",
                        "//*[contains(text(),'English') and contains(text(),'/en')]",
                    ]:
                        try:
                            for el in self.driver.find_elements(By.XPATH, xpath):
                                if self._click(el):
                                    english_clicked = True
                                    time.sleep(2)
                                    break
                            if english_clicked:
                                break
                        except Exception:
                            continue

                    if not english_clicked:
                        r.error = f'English option not found for {doc_id}'
                        results.append(r)
                        continue

                    # ---- Confirm dialog --------------------------------------
                    time.sleep(3)
                    for selector in [
                        "button[class*='confirmButton']",
                        "button.style_confirmButton__18UwC",
                    ]:
                        try:
                            btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                            self._click(btn)
                            break
                        except NoSuchElementException:
                            pass
                    try:
                        btn = self.driver.find_element(
                            By.XPATH, "//button[normalize-space()='Confirm']"
                        )
                        self._click(btn)
                    except NoSuchElementException:
                        pass

                    # ---- Wait for file, rename ------------------------------
                    downloaded = self._wait_for_new_pdf(before)
                    if downloaded:
                        canonical = self.eifu_dir / filename
                        downloaded.rename(canonical)
                        r.path = canonical
                        r.filename = canonical.name
                        r.success = True
                        logger.info('[OK] %s', canonical.name)
                    else:
                        r.error = f'Download timed out for {doc_id}'

                except NoSuchElementException as exc:
                    r.error = f'Element not found for {doc_id}: {exc}'
                    logger.error('Stryker: element not found for %s: %s', doc_id, exc)
                except Exception as exc:
                    r.error = str(exc)
                    logger.error('Stryker: error downloading %s: %s', doc_id, exc)

                results.append(r)
                time.sleep(3)

        except Exception as exc:
            logger.error('Stryker scraper error: %s', exc)
            results.append(DownloadResult(
                manufacturer=self.MANUFACTURER, system=system_name,
                search_term=search_term, filename='', error=str(exc),
            ))

        return results

    def download_all(self) -> List[DownloadResult]:
        all_results: List[DownloadResult] = []
        with self:
            for term, system in STRYKER_SYSTEMS.items():
                all_results.extend(self.download_system(term, system))
                time.sleep(2)
        return all_results


# ---------------------------------------------------------------------------
# Zimmer Biomet scraper
# Portal: https://docs.zimmerbiomet.com/hcp/zimmer/CA/all
#
# Platform: Custom Next.js e-ifu application — NOT Veeva Vault.
# Does NOT use the accordion+confirm pattern of Stryker.
#
# Observed page structure
# -----------------------
# Search results:  div[class*="asrResultItem"]  (one per catalogue number)
#   Description span: class*="leftSideHeaderTextTop"
#
# Product detail:  URL ?keycode={cat_num}_{UDI}
#   Document list:   div[class*="documentAccordion"]   (one per PDF)
#     Accordion header id:  "document-toggle-{doc_id_lowercase}"
#     Title:               class*="leftSideHeaderTextTop"  > span
#     Doc code subtitle:   class*="leftSideHeaderTextSubTitle"
#     Language button:     button#document-languages-toggle  (inside header)
#     Language links:      div#file-download-latest-{lang} > a  (after toggle click)
#     English link:        div#file-download-latest-en > a
#
# All products of the same system share the same IFU document, so we only
# need to click the first matching result to reach the product detail page.
# ---------------------------------------------------------------------------

class ZimmerBiometEIFUScraper(BaseEIFUScraper):
    """
    Downloads English eIFUs from the Zimmer Biomet labeling portal.

    Workflow
    --------
    1. Navigate to search page; search by system name (e.g. 'Persona')
    2. Wait for result cards (class*='asrResultItem') to load
    3. Click the first result → product detail page (?keycode=…)
    4. Scan document accordions on detail page:
         - title  from class*='leftSideHeaderTextTop' span
         - doc_id from accordion header id  ("document-toggle-87-6204-022-99"
                                             → "87-6204-022-99")
    5. Filter to eIFU-type docs using NON_EIFU_PATTERNS
    6. For each eIFU: click language toggle → click English → await PDF
    7. Rename: zimmer_{system}_{doc_id_normalised}.pdf
    """

    SEARCH_URL = 'https://docs.zimmerbiomet.com/hcp/zimmer/CA/all'
    MANUFACTURER = 'zimmer'

    def download_system(
        self,
        search_term: str,
        system_name: str,
    ) -> List[DownloadResult]:
        if not self.driver:
            self.start()

        results: List[DownloadResult] = []

        try:
            logger.info('Zimmer: searching "%s"', search_term)
            self.driver.get(self.SEARCH_URL)
            time.sleep(3)

            # ---- Search -------------------------------------------------------
            search_box = self._wait(By.NAME, 'cross-field-search', timeout=15)
            if not search_box:
                search_box = self._wait(
                    By.CSS_SELECTOR,
                    "input[type='search'], input[type='text']",
                    timeout=5,
                )
            if not search_box:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='Search box not found',
                )]

            search_box.clear()
            search_box.send_keys(search_term)
            time.sleep(1)
            try:
                self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            except NoSuchElementException:
                search_box.send_keys(Keys.RETURN)

            # Wait for result cards to appear
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[class*='asrResultItem']")
                    )
                )
                time.sleep(2)
            except TimeoutException:
                logger.warning('Zimmer: search results did not appear within 30 s')
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='Search results did not load',
                )]

            # ---- Click first result → product detail page --------------------
            try:
                first_result = self.driver.find_element(
                    By.CSS_SELECTOR, "[class*='asrResultItem']"
                )
                self._click(first_result)
            except NoSuchElementException:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='No result cards found',
                )]

            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[class*='documentAccordion']")
                    )
                )
                time.sleep(2)
            except TimeoutException:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='Product detail page did not load',
                )]

            # ---- Parse document list from detail page ------------------------
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            eifu_docs: List[Tuple[str, str]] = []  # (doc_id, title)

            for accordion in soup.find_all(class_=re.compile(r'documentAccordion')):
                # Doc ID from accordion header id: "document-toggle-87-6204-022-99"
                header = accordion.find(id=re.compile(r'^document-toggle-'))
                if not header:
                    continue
                raw_id = header.get('id', '').replace('document-toggle-', '')
                doc_id = raw_id.upper()  # normalise to uppercase for filename

                # Title text
                title_el = accordion.find(class_=re.compile(r'leftSideHeaderTextTop'))
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title:
                    continue

                if not _is_eifu_document(title):
                    logger.info('Zimmer: skip non-eIFU: %r (%s)', title, doc_id)
                    continue

                logger.info('Zimmer: eIFU found: %r (%s)', title, doc_id)
                eifu_docs.append((doc_id, title))

            if not eifu_docs:
                return [DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename='',
                    error='No eIFU documents on product detail page',
                )]

            # ---- Download each eIFU ------------------------------------------
            for doc_id, title in eifu_docs:
                filename = eifu_filename(self.MANUFACTURER, system_name, doc_id)
                r = DownloadResult(
                    manufacturer=self.MANUFACTURER, system=system_name,
                    search_term=search_term, filename=filename, doc_id=doc_id,
                )

                if self.already_downloaded(filename):
                    logger.info('[skip] %s already exists', filename)
                    r.success = True
                    r.path = self.eifu_dir / filename
                    results.append(r)
                    continue

                before = self._existing_pdfs()

                try:
                    # Find accordion header by its id (lowercase in DOM)
                    accordion_header = self.driver.find_element(
                        By.ID, f'document-toggle-{doc_id.lower()}'
                    )

                    # Click the language toggle button inside this header
                    lang_btn = accordion_header.find_element(
                        By.ID, 'document-languages-toggle'
                    )
                    self._click(lang_btn)

                    # Language links expand as  id="file-download-latest-{lang}"
                    # The English one is  id="file-download-latest-en"
                    english_clicked = False
                    try:
                        WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.ID, 'file-download-latest-en')
                            )
                        )
                        en_div = self.driver.find_element(
                            By.ID, 'file-download-latest-en'
                        )
                        en_link = en_div.find_element(By.TAG_NAME, 'a')
                        if self._click(en_link):
                            english_clicked = True
                            time.sleep(2)
                    except (TimeoutException, NoSuchElementException):
                        pass

                    if not english_clicked:
                        r.error = f'English option not found for {doc_id}'
                        results.append(r)
                        continue

                    # Confirm dialog if present
                    time.sleep(2)
                    for selector in [
                        "button[class*='confirmButton']",
                        "button.style_confirmButton__18UwC",
                    ]:
                        try:
                            btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                            self._click(btn)
                            break
                        except NoSuchElementException:
                            pass
                    try:
                        btn = self.driver.find_element(
                            By.XPATH, "//button[normalize-space()='Confirm']"
                        )
                        self._click(btn)
                    except NoSuchElementException:
                        pass

                    # Wait for PDF
                    downloaded = self._wait_for_new_pdf(before)
                    if downloaded:
                        canonical = self.eifu_dir / filename
                        downloaded.rename(canonical)
                        r.path = canonical
                        r.filename = canonical.name
                        r.success = True
                        logger.info('[OK] %s', canonical.name)
                    else:
                        r.error = f'Download timed out for {doc_id}'

                except NoSuchElementException as exc:
                    r.error = f'Element not found for {doc_id}: {exc}'
                    logger.error('Zimmer: element not found for %s: %s', doc_id, exc)
                except Exception as exc:
                    r.error = str(exc)
                    logger.error('Zimmer: error downloading %s: %s', doc_id, exc)

                results.append(r)
                time.sleep(2)

        except Exception as exc:
            logger.error('Zimmer scraper error: %s', exc)
            results.append(DownloadResult(
                manufacturer=self.MANUFACTURER, system=system_name,
                search_term=search_term, filename='', error=str(exc),
            ))

        return results

    def download_all(self) -> List[DownloadResult]:
        all_results: List[DownloadResult] = []
        with self:
            for term, system in ZIMMER_SYSTEMS.items():
                all_results.extend(self.download_system(term, system))
                time.sleep(2)
        return all_results



# ---------------------------------------------------------------------------
# Orchestrator — integrates with implant_lib_v3.py pipeline
# ---------------------------------------------------------------------------

_SCRAPER_MAP: Dict[str, tuple] = {
    'Stryker/Osteonics/Howmedica':      (StrykerEIFUScraper,       STRYKER_SYSTEMS),
    'Zimmer/Biomet/Sulzer/Centerpulse': (ZimmerBiometEIFUScraper,  ZIMMER_SYSTEMS),
}

def _brand_to_system(brand_name: str) -> Optional[Tuple[str, str, str]]:
    """
    Map a BRAND_NAME value to (cjrr_manufacturer, search_term, system_name).
    Matches on the system name (value), not the search term key, because
    Stryker search terms are catalogue numbers, not system names.
    Returns None if not recognised.
    """
    b = brand_name.lower()
    for mfr_key, (_, sys_dict) in _SCRAPER_MAP.items():
        for search_term, system in sys_dict.items():
            # Match system name against brand name (replace _ with space for multi-word)
            if system in b or system.replace('_', ' ') in b:
                return mfr_key, search_term, system
    return None


class EIFUDownloadOrchestrator:
    """
    Coordinates eIFU downloads across manufacturers for use in the pipeline.

    download_system() now returns List[DownloadResult] since one system
    search may yield multiple eIFU PDFs (e.g. cemented + cementless variants).

    Example
    -------
    >>> orch = EIFUDownloadOrchestrator(eifu_dir='implant_eIFUs')
    >>> results = orch.download_missing_for_dataframe(cjrr_df)
    """

    MANIFEST_FILE = 'eifu_download_manifest.json'

    def __init__(self, eifu_dir: str = 'implant_eIFUs', headless: bool = False):
        self.eifu_dir = Path(eifu_dir)
        self.eifu_dir.mkdir(exist_ok=True)
        self.headless = headless
        self._manifest: Dict[str, dict] = self._load_manifest()

    def _manifest_path(self) -> Path:
        return self.eifu_dir / self.MANIFEST_FILE

    def _load_manifest(self) -> Dict[str, dict]:
        p = self._manifest_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {}

    def _save_manifest(self):
        self._manifest_path().write_text(
            json.dumps(self._manifest, indent=2), encoding='utf-8'
        )

    def _record_results(self, system_key: str, results: List[DownloadResult]):
        self._manifest[system_key] = {
            'documents': [
                {'filename': r.filename, 'success': r.success, 'error': r.error}
                for r in results
            ],
            'downloaded_at': datetime.now().isoformat(),
        }
        self._save_manifest()

    def existing_systems(self) -> Dict[str, str]:
        """
        Return {normalised_system_name: filepath} for all eIFUs in eifu_dir.

        Handles both the canonical naming convention ({mfr}_{system}_{id}.pdf)
        and manually-downloaded files with longer descriptive names.  System
        names are normalised to lowercase with underscores so they compare
        correctly against the canonical names in STRYKER_SYSTEMS / ZIMMER_SYSTEMS.
        """
        # Keyword list mirrors the values in STRYKER_SYSTEMS / ZIMMER_SYSTEMS
        _KNOWN_SYSTEMS = [
            'triathlon', 'scorpio', 'duracon', 'kinemax', 'mrh',
            'persona', 'nexgen', 'nex_gen', 'vanguard', 'gender_solutions', 'agc',
            'attune', 'sigma', 'pfc', 's_rom',
        ]

        result = {}
        for p in self.eifu_dir.glob('*.pdf'):
            _, system = parse_eifu_filename(p)
            # Normalise: lowercase + hyphens/spaces → underscores
            norm = re.sub(r'[-\s]', '_', system.lower()) if system else ''

            if not norm or norm not in _KNOWN_SYSTEMS:
                # Fallback: keyword scan over full stem
                stem = re.sub(r'[-\s]', '_', p.stem.lower())
                for kw in _KNOWN_SYSTEMS:
                    if kw in stem:
                        norm = kw
                        break

            if norm:
                result.setdefault(norm, str(p))   # first file wins per system
        return result

    def systems_needing_eifus(self, df: pd.DataFrame) -> List[Tuple[str, str, str]]:
        """
        From a CJRR-filtered DataFrame, return (mfr, term, system) tuples for
        systems that have missing material data and no existing eIFU PDF.
        """
        existing = self.existing_systems()

        metal_col = 'metal_material' if 'metal_material' in df.columns else None
        candidates = df[df[metal_col].isna() | (df[metal_col] == '')] \
            if metal_col else df

        needed: List[Tuple[str, str, str]] = []
        seen: set = set()

        for _, row in candidates.iterrows():
            brand = str(row.get('BRAND_NAME', '') or row.get('brand_name', ''))
            mapping = _brand_to_system(brand)
            if not mapping:
                continue
            mfr_key, term, system = mapping
            if system in seen or system in existing:
                continue
            if mfr_key in _SCRAPER_MAP:
                seen.add(system)
                needed.append((mfr_key, term, system))

        logger.info(
            'eIFU download needed for %d system(s): %s',
            len(needed), [s for _, _, s in needed],
        )
        return needed

    def download_missing_for_dataframe(
        self, df: pd.DataFrame
    ) -> List[DownloadResult]:
        """
        Download eIFUs for all systems in df that have no local PDF.
        Returns flat list of all DownloadResult objects.
        """
        needed = self.systems_needing_eifus(df)
        if not needed:
            logger.info('All required eIFUs already downloaded.')
            return []

        # Group by manufacturer to open each browser once
        by_mfr: Dict[str, List[Tuple[str, str]]] = {}
        for mfr_key, term, system in needed:
            by_mfr.setdefault(mfr_key, []).append((term, system))

        all_results: List[DownloadResult] = []

        for mfr_key, systems in by_mfr.items():
            scraper_cls, _ = _SCRAPER_MAP[mfr_key]
            scraper = scraper_cls(eifu_dir=str(self.eifu_dir), headless=self.headless)
            logger.info('\n--- Downloading eIFUs for %s ---', mfr_key)

            with scraper:
                for term, system in systems:
                    results = scraper.download_system(term, system)
                    self._record_results(f'{mfr_key}/{system}', results)
                    all_results.extend(results)
                    ok = sum(1 for r in results if r.success)
                    logger.info(
                        '  %s: %d/%d documents downloaded', system, ok, len(results)
                    )
                    time.sleep(2)

        ok = sum(1 for r in all_results if r.success)
        logger.info('\neIFU summary: %d/%d succeeded', ok, len(all_results))
        return all_results

    def download_all_known(self) -> List[DownloadResult]:
        """Download eIFUs for all known systems across all manufacturers."""
        all_results: List[DownloadResult] = []
        for mfr_key, (scraper_cls, sys_dict) in _SCRAPER_MAP.items():
            scraper = scraper_cls(eifu_dir=str(self.eifu_dir), headless=self.headless)
            with scraper:
                for term, system in sys_dict.items():
                    results = scraper.download_system(term, system)
                    self._record_results(f'{mfr_key}/{system}', results)
                    all_results.extend(results)
                    time.sleep(2)
        return all_results

    def print_summary(self, results: List[DownloadResult]):
        logger.info('\n' + '=' * 65)
        logger.info('eIFU DOWNLOAD SUMMARY')
        logger.info('=' * 65)
        for r in results:
            status = '[OK]  ' if r.success else '[FAIL]'
            detail = r.filename if r.success else r.error
            logger.info('  %s  %-8s  %-18s  %s', status, r.manufacturer, r.system, detail)
        ok = sum(1 for r in results if r.success)
        logger.info('\n  %d / %d documents succeeded', ok, len(results))
        logger.info('=' * 65)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Download knee implant eIFU PDFs')
    parser.add_argument(
        '--manufacturer', default=None,
        choices=['Stryker', 'Zimmer', 'all'],
        help='Manufacturer to download (default: all)',
    )
    parser.add_argument(
        '--search', default=None,
        help='Search term for a single system (e.g. "Triathlon")',
    )
    parser.add_argument(
        '--eifu-dir', default='implant_eIFUs',
        help='Directory to save PDFs (default: implant_eIFUs)',
    )
    parser.add_argument(
        '--headless', action='store_true',
        help='Run browser in headless mode',
    )
    args = parser.parse_args()

    orch = EIFUDownloadOrchestrator(eifu_dir=args.eifu_dir, headless=args.headless)

    if args.search:
        mfr = args.manufacturer or 'Stryker'
        cls_map = {
            'Stryker': (StrykerEIFUScraper,      STRYKER_SYSTEMS),
            'Zimmer':  (ZimmerBiometEIFUScraper,  ZIMMER_SYSTEMS),
        }
        scraper_cls, sys_dict = cls_map[mfr]
        system = sys_dict.get(args.search,
                              re.sub(r'[^a-z0-9]', '_', args.search.lower()))
        scraper = scraper_cls(eifu_dir=args.eifu_dir, headless=args.headless)
        with scraper:
            results = scraper.download_system(args.search, system)
        orch.print_summary(results)

    elif args.manufacturer and args.manufacturer != 'all':
        cls_map = {
            'Stryker': StrykerEIFUScraper,
            'Zimmer':  ZimmerBiometEIFUScraper,
        }
        scraper = cls_map[args.manufacturer](
            eifu_dir=args.eifu_dir, headless=args.headless
        )
        results = scraper.download_all()
        orch.print_summary(results)

    else:
        results = orch.download_all_known()
        orch.print_summary(results)


if __name__ == '__main__':
    main()
