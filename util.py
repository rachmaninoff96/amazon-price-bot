import os
import re
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================

USE_KEEPA: bool = os.getenv("USE_KEEPA", "0") == "1"
KEEPA_API_KEY: str = os.getenv("KEEPA_API_KEY", "").strip()

# Cache per evitare chiamate duplicate
_PRICE_CACHE: Dict[str, Tuple[float, "PriceData"]] = {}
PRICE_CACHE_TTL_SECONDS = 300  # 5 minuti

# Log “startup” (utile in Render)
logger.info(
    "Pricing init | USE_KEEPA=%s | KEEPA_API_KEY present=%s | cache_ttl=%ss",
    USE_KEEPA,
    bool(KEEPA_API_KEY),
    PRICE_CACHE_TTL_SECONDS,
)

# ============================================================
# DATA STRUCTURE
# ============================================================

@dataclass(frozen=True)
class PriceData:
    price_now: float
    lowest_90: float
    forecast: float
    lo: float
    hi: float
    min_date: datetime
    likely_days: int


# ============================================================
# MOCK (fallback)
# ============================================================

def mock_prices_from_asin(asin: str) -> PriceData:
    base = sum(ord(c) for c in asin)

    price_now = 19.9 + (base % 280) + ((base % 9) * 0.1)
    price_now = round(price_now, 2)

    pct = 0.05 + (base % 26) / 100.0
    lowest_90 = round(price_now * (1 - pct), 2)

    days_ago = (base % 90) + 1
    min_date = datetime.now() - timedelta(days=days_ago)

    delta_pct = ((base % 15) - 7) / 100.0
    forecast = round(price_now * (1 + delta_pct), 2)
    lo = round(forecast * 0.95, 2)
    hi = round(forecast * 1.05, 2)

    likely_days = 1 + (base % 7)

    return PriceData(price_now, lowest_90, forecast, lo, hi, min_date, likely_days)


# ============================================================
# KEEPA (real)
# ============================================================

def _keepa_price_to_eur(value: Optional[int]) -> Optional[float]:
    """
    Keepa spesso ritorna prezzi come interi in centesimi.
    -1 / 0 / None -> non disponibile.
    """
    if value is None:
        return None
    try:
        v = int(value)
    except Exception:
        return None
    if v <= 0:
        return None
    return round(v / 100.0, 2)


async def _fetch_keepa_stats_90(asin: str) -> Tuple[float, float]:
    """
    Ritorna (price_now, lowest_90).
    Se fallisce, solleva eccezione con motivo (così logghiamo bene).
    """
    if not KEEPA_API_KEY:
        raise RuntimeError("KEEPA_API_KEY missing")

    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": "IT",   # Keepa accetta IT come domain string  [oai_citation:0‡keepaapi.readthedocs.io](https://keepaapi.readthedocs.io/en/stable/api_methods.html?utm_source=chatgpt.com)
        "asin": asin,
        "stats": 90,
        "history": 0,
    }

    timeout = aiohttp.ClientTimeout(total=12)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            status = resp.status
            text = await resp.text()
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {text[:200]}")

            try:
                data = await resp.json()
            except Exception:
                raise RuntimeError(f"JSON parse failed: {text[:200]}")

    # Keepa può anche rispondere con errori “logici”
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(f"Keepa error: {data.get('error')}")
        if data.get("products") is None:
            raise RuntimeError(f"No 'products' in response (keys={list(data.keys())})")

    products = data.get("products") or []
    if not products:
        raise RuntimeError("Empty products[]")

    p0 = products[0]
    stats = p0.get("stats")
    if not stats:
        raise RuntimeError("No stats in product")

    current_arr = stats.get("current") or []
    min_arr = stats.get("min") or []

    def safe_get(arr, idx):
        try:
            return arr[idx]
        except Exception:
            return None

    # Indici standard: AMAZON=0, NEW=1 (mapping tipico Keepa)  [oai_citation:1‡keepaapi.readthedocs.io](https://keepaapi.readthedocs.io/en/stable/api_methods.html?utm_source=chatgpt.com)
    cur_amz = _keepa_price_to_eur(safe_get(current_arr, 0))
    min_amz = _keepa_price_to_eur(safe_get(min_arr, 0))

    cur_new = _keepa_price_to_eur(safe_get(current_arr, 1))
    min_new = _keepa_price_to_eur(safe_get(min_arr, 1))

    price_now = cur_amz or cur_new
    lowest_90 = min_amz or min_new

    if price_now is None:
        raise RuntimeError(f"No current price available (current={current_arr[:5]})")

    if lowest_90 is None:
        lowest_90 = price_now

    return price_now, lowest_90


# ============================================================
# SINGLE ENTRY POINT
# ============================================================

async def get_price_data(asin: str) -> PriceData:
    """
    - Keepa se abilitato
    - fallback mock se Keepa fallisce
    - cache TTL per evitare chiamate duplicate
    """
    now = time.time()

    cached = _PRICE_CACHE.get(asin)
    if cached:
        ts, pdata = cached
        if now - ts <= PRICE_CACHE_TTL_SECONDS:
            return pdata

    if USE_KEEPA:
        try:
            price_now, lowest_90 = await _fetch_keepa_stats_90(asin)
            pdata = PriceData(
                price_now=price_now,
                lowest_90=lowest_90,
                forecast=price_now,
                lo=price_now,
                hi=price_now,
                min_date=datetime.now(),
                likely_days=3,
            )
            _PRICE_CACHE[asin] = (now, pdata)
            logger.info("Keepa OK | asin=%s | now=%.2f | low90=%.2f", asin, price_now, lowest_90)
            return pdata

        except Exception as e:
            logger.warning("Keepa failed -> fallback mock | asin=%s | reason=%s", asin, str(e))

    pdata = mock_prices_from_asin(asin)
    _PRICE_CACHE[asin] = (now, pdata)
    return pdata


# ============================================================
# AFFILIATE LINK
# ============================================================

def affiliate_link_it(asin: str, tag: str = "amztracker0c-21"):
    return f"https://www.amazon.it/dp/{asin}?tag={tag}"


# ============================================================
# NAME FROM URL
# ============================================================

def clean_text(name: str) -> str:
    name = re.sub(r"[-_/]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 60:
        name = name[:60].rstrip() + "…"
    return name


def auto_short_name_from_url(url: str, asin: str) -> str:
    try:
        m = re.search(r"/dp/[^/]+/([^/?#]+)", url, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1)).title()

        m = re.search(r"amazon\.[^/]+/([^/]+)/dp/", url, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1)).title()

        m = re.search(r"[\?&]keywords=([^&]+)", url, flags=re.IGNORECASE)
        if m:
            kw = m.group(1).replace("+", " ").strip()
            return clean_text(kw).title()
    except Exception:
        pass

    return "Prodotto"


# ============================================================
# EXPAND SHORT URL
# ============================================================

async def expand_amazon_url(text: str) -> str:
    m = re.search(r"(https?://\S+)", text)
    if not m:
        return text

    url = m.group(1)

    if not re.search(r"(amzn\.|amazon\.)", url, flags=re.IGNORECASE):
        return text

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True, timeout=10) as resp:
                return str(resp.url)
    except Exception:
        return text


# ============================================================
# THRESHOLDS (legacy + recommended)
# ============================================================

def suggest_thresholds(asin: str):
    pdata = mock_prices_from_asin(asin)
    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(max(lowest_90, price_now * 0.88), 2)
    return [s1, s2, s3]


async def get_recommended_threshold(asin: str):
    pdata = await get_price_data(asin)
    current = float(pdata.price_now)
    target = round(current * 0.95, 2)
    days = int(pdata.likely_days)
    saving_pct = (current - target) / current * 100.0 if current > 0 else 0.0
    return target, days, saving_pct