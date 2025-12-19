import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

import aiohttp


# ============================================================
# CONFIG
# ============================================================

USE_KEEPA: bool = os.getenv("USE_KEEPA", "1") == "1"  # ora lo vuoi ON
KEEPA_API_KEY: str = os.getenv("KEEPA_API_KEY", "").strip()

# Cache per evitare chiamate duplicate
# asin -> (ts, PriceData)
_PRICE_CACHE: Dict[str, Tuple[float, "PriceData"]] = {}
PRICE_CACHE_TTL_SECONDS = 300  # 5 minuti


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

def mock_prices_from_asin(asin: str):
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
    Keepa ritorna spesso prezzi in centesimi (int).
    Valori <= 0 o None significano "non disponibile".
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


async def _fetch_keepa_stats_90(asin: str) -> Optional[Tuple[float, float]]:
    """
    Ritorna (price_now, lowest_90) oppure None se non riesce.
    Usa stats=90 e history=0 per essere leggero.
    """
    # Se non c'è la key, non possiamo fare nulla
    if not KEEPA_API_KEY:
        return None

    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": "IT",
        "asin": asin,
        "stats": 90,
        "history": 0,
    }

    timeout = aiohttp.ClientTimeout(total=12)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    # Keepa tipicamente usa "products": [ ... ]
    products = data.get("products") or []
    if not products:
        return None

    p0 = products[0]
    stats = p0.get("stats")
    if not stats:
        return None

    # stats.current / stats.min sono array (varie "serie prezzi": AMAZON, NEW, ecc.)
    # Senza buybox (che costa più token) prendiamo:
    # - prima AMAZON (index 0)
    # - fallback NEW (index 1)
    current_arr = stats.get("current") or []
    min_arr = stats.get("min") or []

    def _safe_get(arr, idx):
        try:
            return arr[idx]
        except Exception:
            return None

    # TENTATIVO 1: AMAZON
    cur_amz = _keepa_price_to_eur(_safe_get(current_arr, 0))
    min_amz = _keepa_price_to_eur(_safe_get(min_arr, 0))

    # TENTATIVO 2: NEW marketplace
    cur_new = _keepa_price_to_eur(_safe_get(current_arr, 1))
    min_new = _keepa_price_to_eur(_safe_get(min_arr, 1))

    price_now = cur_amz or cur_new
    lowest_90 = min_amz or min_new

    if price_now is None:
        return None

    if lowest_90 is None:
        lowest_90 = price_now

    return price_now, lowest_90


# ============================================================
# SINGLE ENTRY POINT (used by handlers + watcher)
# ============================================================

async def get_price_data(asin: str) -> PriceData:
    """
    Unico punto prezzi:
    - prova Keepa (se USE_KEEPA e key presente)
    - fallback mock se Keepa fallisce
    - cache TTL per evitare chiamate duplicate
    """
    now = time.time()

    # cache
    cached = _PRICE_CACHE.get(asin)
    if cached:
        ts, pdata = cached
        if now - ts <= PRICE_CACHE_TTL_SECONDS:
            return pdata

    # Keepa
    if USE_KEEPA and KEEPA_API_KEY:
        try:
            res = await _fetch_keepa_stats_90(asin)
            if res:
                price_now, lowest_90 = res
                pdata = PriceData(
                    price_now=price_now,
                    lowest_90=lowest_90,
                    forecast=price_now,  # per ora non lo usiamo davvero
                    lo=price_now,
                    hi=price_now,
                    min_date=datetime.now(),  # placeholder
                    likely_days=3,            # placeholder
                )
                _PRICE_CACHE[asin] = (now, pdata)
                return pdata
        except Exception:
            # fallback sotto
            pass

    # fallback mock
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
# OLD SUGGEST THRESHOLDS (lasciamo)
# ============================================================

def suggest_thresholds(asin: str):
    # usa mock per le 3 soglie legacy (non ti cambia UX per ora)
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