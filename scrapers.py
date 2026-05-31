"""
scrapers.py — Web scrapers for NSE and SEBI data.

Sources:
  1. NSE FII/DII API       — daily institutional cash flow figures
  2. NSE Corporate Filings — company-level announcements & regulatory filings
  3. SEBI Press Releases   — broad regulatory announcements from SEBI

IMPORTANT: NSE blocks plain HTTP requests without browser-like headers and
session cookies. The NSESession class handles this by visiting the NSE
homepage first to obtain valid cookies before hitting the API endpoints.
"""

import logging
import requests
from bs4 import BeautifulSoup

from config import (
    NSE_BASE_URL,
    NSE_FII_DII_URL,
    NSE_ANNOUNCEMENTS_URL,
    SEBI_PRESS_RELEASES_URL,
)

logger = logging.getLogger(__name__)

# Browser-like headers required by NSE — without these, requests return 403
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class NSESession:
    """
    A persistent HTTP session tuned for NSE's anti-scraping measures.

    NSE requires:
      1. A valid browser User-Agent
      2. Session cookies obtained by visiting the homepage first
      3. The 'Referer' header set to the NSE homepage on every API call

    Usage:
        nse = NSESession()
        data = nse.get("https://www.nseindia.com/api/...")
    """

    def __init__(self):
        self.session = requests.Session()
        self._refresh_cookies()

    def _refresh_cookies(self) -> None:
        """Visit the NSE homepage to receive valid session cookies."""
        try:
            html_headers = {**NSE_HEADERS, "Accept": "text/html,application/xhtml+xml"}
            self.session.get(NSE_BASE_URL, headers=html_headers, timeout=20)
            logger.debug("NSE session cookies refreshed.")
        except requests.RequestException as e:
            logger.warning(f"Could not initialize NSE session cookies: {e}")

    def get(self, url: str, **kwargs) -> requests.Response | None:
        """
        Make a GET request to an NSE endpoint.
        Automatically refreshes cookies and retries once on a 403 response.

        Returns the response object, or None if the request fails.
        """
        try:
            resp = self.session.get(
                url, headers=NSE_HEADERS, timeout=20, **kwargs
            )

            # NSE returns 403 when session cookies expire — refresh and retry
            if resp.status_code == 403:
                logger.warning("NSE returned 403 — refreshing session cookies and retrying.")
                self._refresh_cookies()
                resp = self.session.get(url, headers=NSE_HEADERS, timeout=20, **kwargs)

            resp.raise_for_status()
            return resp

        except requests.exceptions.Timeout:
            logger.error(f"NSE request timed out: {url}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"NSE request failed for {url}: {e}")
            return None


# Module-level NSE session — shared across all scraper calls to reuse cookies
_nse = NSESession()


# ─── FII/DII Data ─────────────────────────────────────────────────────────────

def get_fii_dii_data() -> list[dict]:
    """
    Fetch today's FII (Foreign Institutional Investor) and DII
    (Domestic Institutional Investor) cash market activity from NSE.

    Returns a list of dicts, each with:
        category  — "FII/FPI" or "DII"
        buyValue  — total buy value in crores (₹)
        sellValue — total sell value in crores (₹)
        netValue  — net flow in crores (positive = inflow, negative = outflow)
        date      — trade date string

    Returns [] if data cannot be fetched.
    """
    resp = _nse.get(NSE_FII_DII_URL)
    if resp is None:
        logger.error("Could not fetch FII/DII data from NSE.")
        return []

    try:
        data = resp.json()

        # NSE returns either a direct list or a dict with a "data" key
        if isinstance(data, list):
            logger.info(f"FII/DII data fetched: {len(data)} entries.")
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]

        logger.warning(f"Unexpected FII/DII response format: {type(data)}")
        return []

    except ValueError as e:
        logger.error(f"Failed to parse FII/DII JSON response: {e}")
        return []


# ─── NSE Corporate Announcements ──────────────────────────────────────────────

def get_corporate_announcements(symbol: str) -> list[dict]:
    """
    Fetch recent corporate announcements for a single stock from NSE.

    These include earnings releases, board meeting outcomes, shareholding
    changes, regulatory disclosures, and other company filings.

    Args:
        symbol: NSE stock symbol, e.g. "RELIANCE"

    Returns a list of dicts, each with:
        id       — unique identifier for the announcement (for dedup)
        symbol   — stock symbol
        subject  — short description of the announcement
        datetime — when the announcement was posted
    """
    url  = NSE_ANNOUNCEMENTS_URL.format(symbol=symbol.upper())
    resp = _nse.get(url)

    if resp is None:
        logger.warning(f"Could not fetch announcements for {symbol}.")
        return []

    try:
        raw   = resp.json()
        items = []

        # NSE can return a list or a dict with a 'data' key
        if isinstance(raw, dict):
            raw = raw.get("data", [])

        for entry in raw:
            # Use 'an_no' or 'dTime' as a stable unique ID for deduplication
            filing_id = str(
                entry.get("an_no") or entry.get("dTime") or entry.get("dt") or ""
            )
            items.append({
                "id":       filing_id,
                "symbol":   entry.get("symbol", symbol).upper(),
                "subject":  entry.get("subject", entry.get("desc", "No subject")),
                "datetime": entry.get("dt", entry.get("an_dt", "")),
            })

        logger.debug(f"Fetched {len(items)} announcements for {symbol}.")
        return items

    except (ValueError, AttributeError, TypeError) as e:
        logger.error(f"Failed to parse announcements for {symbol}: {e}")
        return []


def get_all_announcements(symbols: list[str]) -> list[dict]:
    """
    Fetch and combine corporate announcements for all given symbols.

    Args:
        symbols: List of NSE stock symbols to check

    Returns:
        Combined list of announcement dicts from all symbols.
    """
    all_announcements: list[dict] = []

    for symbol in symbols:
        try:
            items = get_corporate_announcements(symbol)
            all_announcements.extend(items)
        except Exception as e:
            logger.error(f"Error fetching announcements for {symbol}: {e}")

    logger.info(
        f"Fetched {len(all_announcements)} total announcements for {len(symbols)} symbols."
    )
    return all_announcements


# ─── SEBI Press Releases ──────────────────────────────────────────────────────

def get_sebi_press_releases() -> list[dict]:
    """
    Scrape the SEBI press releases page for the latest regulatory announcements.

    These are broader market-level updates (policy changes, circulars, orders)
    rather than company-specific filings. We check if any release mentions
    stocks in our holdings or watchlist.

    Returns list of dicts with: id, title, date, url
    Returns [] on error or if parsing fails.
    """
    try:
        resp = requests.get(
            SEBI_PRESS_RELEASES_URL,
            headers={
                "User-Agent": NSE_HEADERS["User-Agent"],
                "Accept":     "text/html,application/xhtml+xml",
            },
            timeout=20,
        )
        resp.raise_for_status()

    except requests.RequestException as e:
        logger.error(f"Failed to fetch SEBI press releases: {e}")
        return []

    try:
        soup    = BeautifulSoup(resp.text, "lxml")
        results = []

        # SEBI's press releases page lists items in a table
        # Try table rows first, then fall back to list items
        rows = (
            soup.select("table.table tr")
            or soup.select(".content-area li")
            or soup.select(".listing a")
        )

        for row in rows[:20]:  # Only look at the 20 most recent items
            link_tag = row.find("a", href=True)
            if not link_tag:
                continue

            title = link_tag.get_text(strip=True)
            url   = link_tag["href"]

            # Make relative URLs absolute
            if url.startswith("/"):
                url = f"https://www.sebi.gov.in{url}"
            elif not url.startswith("http"):
                continue

            # Try to extract a date from the row text
            date_text = ""
            cells = row.find_all("td")
            if cells:
                date_text = cells[0].get_text(strip=True)

            results.append({
                "id":    url,      # Use the URL as a stable unique identifier
                "title": title,
                "date":  date_text,
                "url":   url,
            })

        logger.info(f"Fetched {len(results)} SEBI press releases.")
        return results

    except Exception as e:
        logger.error(f"Failed to parse SEBI press releases page: {e}")
        return []
