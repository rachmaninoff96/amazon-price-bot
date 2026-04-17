import os
import re
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Tuple, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ================= CONFIG =================

USE_KEEPA: bool = os.getenv("USE_KEEPA", "0") == "1"
KEEPA_API_KEY: str = os.getenv("KEEPA_API_KEY", "").strip()

_PRICE_CACHE: Dict[str, Tuple[float, "KeepaStats90"]] = {}
PRICE_CACHE_TTL_SECONDS = 300

# ================= DATA =================

@dataclass(frozen=True)
class PriceData:
    price_now: float
    lowest_90: float
    avg_90: float
    max_90: float
    forecast_7d: float
    lo_7d: float
    hi_7d: float
    likely_days: int
    state: str
    advice: str


@dataclass(frozen=True)
class KeepaStats90:
    current: float
    min90: float
    avg90: float
    max90: float
    series: str

# ================= KEEPA =================

async def get_price_data(asin: str) -> PriceData:
    now = time.time()

    # cache
    if asin in _PRICE_CACHE:
        ts, data = _PRICE_CACHE[asin]
        if now - ts < PRICE_CACHE_TTL_SECONDS:
            return data

    if USE_KEEPA and KEEPA_API_KEY:
        try:
            logger.warning("TRYING REAL KEEPA...")

            url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain=8&asin={asin}&stats=90&history=1"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()

            products = data.get("products", [])
            if not products:
                raise Exception("No product returned")

            product = products[0]

            # 🔥 PREZZO ATTUALE + SERIE
            csv = product.get("csv", [])
            if not csv or len(csv) < 2:
                raise Exception("No CSV data")

            price_series = csv[1]  # Amazon price history
            if not price_series:
                raise Exception("Empty price history")

            # ultimi prezzi (formato: [time, price, time, price...])
            prices = price_series[1::2]
            prices = [p for p in prices if p > 0]

            if not prices:
                raise Exception("No valid prices")

            # 🔥 SOGLIA STATISTICA
            smart_threshold = None
            try:
                smart_threshold = suggest_threshold_statistical(price_series)
                logger.warning(f"SMART THRESHOLD: {smart_threshold}")
            except Exception as e:
                logger.warning(f"THRESHOLD ERROR: {e}")

            # 🔥 STATISTICHE BASE
            current = prices[-1]
            min90 = min(prices[-100:])
            max90 = max(prices[-100:])
            avg90 = sum(prices[-100:]) / len(prices[-100:])

            # 🔥 RESULT
            result = PriceData(
                price_now=current / 100,
                lowest_90=min90 / 100,
                avg_90=avg90 / 100,
                max_90=max90 / 100,
                forecast_7d=current / 100,
                lo_7d=min90 / 100,
                hi_7d=max90 / 100,
                likely_days=3,
                state="REAL",
                advice=str(smart_threshold) if smart_threshold is not None else "",
            )

            _PRICE_CACHE[asin] = (now, result)

            logger.warning(f"KEEPA OK: {result.price_now}€")
            return result

        except Exception as e:
            logger.warning(f"KEEPA ERROR: {e}")

    # fallback
    result = mock_prices_from_asin(asin)
    _PRICE_CACHE[asin] = (now, result)
    logger.warning("USING MOCK DATA")
    return result

# ================= TITLE AMAZON =================

async def get_amazon_title(url: str) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "it-IT,it;q=0.9"
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                html = await resp.text()

                m = re.search(r'<span id="productTitle">(.*?)</span>', html, re.S)
                if m:
                    title = m.group(1)
                    title = re.sub(r"\s+", " ", title).strip()
                    return title[:80]

                m2 = re.search(r'<title>(.*?)</title>', html, re.S)
                if m2:
                    title = m2.group(1)
                    title = title.replace("Amazon.it", "").strip()
                    return title[:80]

    except Exception as e:
        logger.warning(f"TITLE ERROR: {e}")

    return ""

# ================= MOCK =================

def mock_prices_from_asin(asin: str) -> PriceData:
    base = sum(ord(c) for c in asin)

    price_now = round(50 + (base % 100), 2)
    lowest_90 = round(price_now * 0.85, 2)
    avg_90 = round((price_now + lowest_90) / 2, 2)

    return PriceData(
        price_now=price_now,
        lowest_90=lowest_90,
        avg_90=avg_90,
        max_90=price_now,
        forecast_7d=price_now,
        lo_7d=price_now,
        hi_7d=price_now,
        likely_days=3,
        state="MOCK",
        advice="",
    )



# ================= AFFILIATE =================

def affiliate_link_it(asin: str, tag: str = "amztracker0c-21"):
    return f"https://www.amazon.it/dp/{asin}?tag={tag}"

# ================= URL =================

async def expand_amazon_url(text: str) -> str:
    m = re.search(r"(https?://\S+)", text)
    if not m:
        return text

    url = m.group(1)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                return str(resp.url)
    except:
        return text

# ================= DECISION =================

def simple_price_decision(price_now: float, min_90: float):
    if not price_now or not min_90:
        return "NORMAL", None

    ratio = price_now / min_90

    if ratio <= 1.05:
        return "GOOD", ratio
    elif ratio <= 1.25:
        return "NORMAL", ratio
    else:
        return "HIGH", ratio

# ================= FALLBACK NAME =================

def auto_short_name_from_url(url: str, asin: str) -> str:
    try:
        m = re.search(r"/dp/[^/]+/([^/?#]+)", url, flags=re.IGNORECASE)
        if m:
            name = m.group(1)
            name = re.sub(r"[-_/]+", " ", name)
            name = re.sub(r"\s+", " ", name).strip()
            return name.title()[:60]

    except Exception:
        pass

    return f"Prodotto {asin}"

# ================= SUGGEST THRESHOLDS =================

def suggest_threshold_statistical(price_series):
    """
    price_series: lista Keepa [time, price, time, price...]
    """


    # separa
    times = price_series[0::2]
    prices = price_series[1::2]

    # filtra prezzi validi
    data = [(p, t) for p, t in zip(prices, times) if p > 0]

    if not data:
        return None

    # ---- FILTRO OUTLIER ----
    values = sorted([p for p, _ in data])
    n = len(values)

    p5 = values[int(n * 0.05)]
    p95 = values[int(n * 0.95)]

    filtered = [(p, t) for p, t in data if p5 <= p <= p95]

    values = sorted([p for p, _ in filtered])
    n = len(values)

    if n < 10:
        return None

    # ---- PERCENTILI ----
    p20 = values[int(n * 0.2)]
    p30 = values[int(n * 0.3)]

    # ---- RECENCY CHECK ----
    threshold_base = p20

    last_seen_days = None

    for p, t in reversed(filtered):
        if abs(p - threshold_base) / threshold_base < 0.02:

            keepa_epoch = 21564000
            t_unix = (t + keepa_epoch) * 60

            last_seen_days = (time.time() - t_unix) / 86400
            break

    if last_seen_days is None:
        last_seen_days = 999

    # ---- DECISIONE ----
    if last_seen_days <= 45:
        threshold = p20
    else:
        threshold = p30

    return round(threshold / 100, 2)

def suggest_thresholds(asin: str):
    pdata = mock_prices_from_asin(asin)
    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(lowest_90 * 1.02, 2)

    return [s1, s2, s3]