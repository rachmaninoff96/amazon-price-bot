import os
import re
import time
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

import aiohttp

logger = logging.getLogger(__name__)

USE_KEEPA: bool = os.getenv("USE_KEEPA", "0") == "1"
KEEPA_API_KEY: str = os.getenv("KEEPA_API_KEY", "").strip()
KEEPA_DOMAIN_ID: int = int(os.getenv("KEEPA_DOMAIN_ID", "8"))  # amazon.it

_PRICE_CACHE: Dict[str, Tuple[float, "PriceData"]] = {}
PRICE_CACHE_TTL_SECONDS = 300  # 5 minuti

logger.warning(
    "PRICING INIT | USE_KEEPA=%s | KEEPA_API_KEY present=%s | domainId=%s | CACHE_TTL=%ss",
    USE_KEEPA,
    bool(KEEPA_API_KEY),
    KEEPA_DOMAIN_ID,
    PRICE_CACHE_TTL_SECONDS,
)


@dataclass(frozen=True)
class PriceData:
    price_now: float
    lowest_90: float
    forecast: float
    lo: float
    hi: float
    min_date: datetime
    likely_days: int


# ---------------- MOCK (fallback) ----------------

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


# ---------------- Keepa helpers ----------------

def _keepa_price_to_eur(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    try:
        v = int(value)
    except Exception:
        return None
    if v <= 0:
        return None
    return round(v / 100.0, 2)


def _safe_get(arr, idx):
    try:
        return arr[idx]
    except Exception:
        return None


def _pick_price(arr) -> Optional[float]:
    # prefer AMAZON=0, fallback NEW=1
    v = _keepa_price_to_eur(_safe_get(arr, 0))
    if v is not None:
        return v
    return _keepa_price_to_eur(_safe_get(arr, 1))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


async def _fetch_keepa_stats_90(asin: str) -> Tuple[float, float, float, float]:
    """
    returns: (current, min90, avg90, max90) in EUR
    """
    if not KEEPA_API_KEY:
        raise RuntimeError("KEEPA_API_KEY missing")

    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": KEEPA_DOMAIN_ID,
        "asin": asin,
        "stats": 90,
        "history": 0,
    }

    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, params=params) as resp:
                status = resp.status
                raw_text = await resp.text()
                if status != 200:
                    raise RuntimeError(f"HTTP {status}: {raw_text[:300]}")
                try:
                    data = await resp.json()
                except Exception:
                    raise RuntimeError(f"JSON parse failed: {raw_text[:300]}")
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout while calling Keepa")
        except Exception as e:
            raise RuntimeError(f"Request failed: {e}")

    products = (data or {}).get("products") or []
    if not products:
        raise RuntimeError("Empty products[]")

    stats = (products[0] or {}).get("stats")
    if not stats:
        raise RuntimeError("No stats in product")

    cur = _pick_price(stats.get("current") or [])
    mn = _pick_price(stats.get("min") or [])
    av = _pick_price(stats.get("avg") or [])
    mx = _pick_price(stats.get("max") or [])

    if cur is None:
        raise RuntimeError(f"No current price (current={(stats.get('current') or [])[:6]})")

    if mn is None:
        mn = cur
    if av is None:
        av = cur
    if mx is None:
        mx = max(cur, av, mn)

    return float(cur), float(mn), float(av), float(mx)


def _compute_forecast_and_band(cur: float, mn: float, av: float, mx: float) -> Tuple[float, float, float, int]:
    """
    Forecast 7gg semplice:
    - ritorno verso la media (av)
    - banda basata su volatilità (mx-mn)
    """
    vol = max(0.01, mx - mn)

    # ritorno verso media (non “magico”, ma sensato)
    k = 0.35
    forecast = cur - k * (cur - av)
    forecast = _clamp(forecast, mn, mx)

    # banda: 18% della volatilità, minimo 0.5€
    band = max(0.5, 0.18 * vol)
    lo = max(mn, forecast - band)
    hi = min(mx, forecast + band)

    # giorni stimati (solo euristica)
    if cur <= av * 1.02:
        likely_days = 3
    elif cur <= av * 1.08:
        likely_days = 5
    else:
        likely_days = 7

    return round(forecast, 2), round(lo, 2), round(hi, 2), likely_days


def _compute_recommended_threshold(cur: float, mn: float, av: float, mx: float) -> Tuple[float, int, float]:
    """
    Soglia consigliata: raggiungibile ma con senso.
    - se prezzo è alto: target verso media
    - se già basso: target leggero sconto
    - mai sotto il minimo 90gg (o quasi)
    """
    if cur > av * 1.08:
        target = av  # rientro in media
        days = 5
    elif cur > av * 1.03:
        target = av * 0.99
        days = 4
    else:
        target = cur * 0.98
        days = 3

    # protezioni
    target = max(target, mn * 1.01)        # non troppo aggressivo
    target = min(target, cur * 0.995)      # deve essere sotto l’attuale

    target = round(target, 2)
    saving_pct = (cur - target) / cur * 100.0 if cur > 0 else 0.0
    return target, days, saving_pct


# ---------------- Public API ----------------

async def get_price_data(asin: str) -> PriceData:
    now = time.time()

    cached = _PRICE_CACHE.get(asin)
    if cached:
        ts, pdata = cached
        if now - ts <= PRICE_CACHE_TTL_SECONDS:
            return pdata

    if USE_KEEPA and KEEPA_API_KEY:
        try:
            cur, mn, av, mx = await _fetch_keepa_stats_90(asin)
            forecast, lo, hi, likely_days = _compute_forecast_and_band(cur, mn, av, mx)

            pdata = PriceData(
                price_now=round(cur, 2),
                lowest_90=round(mn, 2),
                forecast=forecast,
                lo=lo,
                hi=hi,
                # stats non include “data del minimo”: placeholder, UI non deve mostrarla come data reale
                min_date=datetime.now(),
                likely_days=likely_days,
            )
            _PRICE_CACHE[asin] = (now, pdata)
            logger.warning("KEEPA OK | asin=%s | now=%.2f | min90=%.2f | avg90=%.2f | max90=%.2f",
                           asin, cur, mn, av, mx)
            return pdata
        except Exception as e:
            logger.warning("KEEPA FAILED -> fallback mock | asin=%s | reason=%s", asin, str(e))

    pdata = mock_prices_from_asin(asin)
    _PRICE_CACHE[asin] = (now, pdata)
    return pdata


async def get_recommended_threshold(asin: str):
    """
    Ritorna: (recommended_price, days, saving_pct)
    """
    if USE_KEEPA and KEEPA_API_KEY:
        try:
            cur, mn, av, mx = await _fetch_keepa_stats_90(asin)
            return _compute_recommended_threshold(cur, mn, av, mx)
        except Exception as e:
            logger.warning("KEEPA REC FAILED -> fallback simple | asin=%s | reason=%s", asin, str(e))

    # fallback semplice (mock o keepa fail)
    pdata = await get_price_data(asin)
    cur = float(pdata.price_now)
    target = round(cur * 0.95, 2)
    days = int(pdata.likely_days)
    saving_pct = (cur - target) / cur * 100.0 if cur > 0 else 0.0
    return target, days, saving_pct


def affiliate_link_it(asin: str, tag: str = "amztracker0c-21"):
    return f"https://www.amazon.it/dp/{asin}?tag={tag}"


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


def suggest_thresholds(asin: str):
    # legacy: lascio mock come prima
    pdata = mock_prices_from_asin(asin)
    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(max(lowest_90, price_now * 0.88), 2)
    return [s1, s2, s3]