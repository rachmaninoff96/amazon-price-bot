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

async def get_keepa_data(asin: str) -> Optional[KeepaStats90]:
    if not KEEPA_API_KEY:
        return None

    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain=8&asin={asin}&stats=90"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()

                products = data.get("products")
                if not products:
                    return None

                p = products[0]
                stats = p.get("stats", {})

                current = stats.get("current", [None])[1]
                avg90 = stats.get("avg90", [None])[1]
                min90 = stats.get("min90", [None])[1]
                max90 = stats.get("max90", [None])[1]

                def conv(x):
                    return round(x / 100, 2) if x else None

                return KeepaStats90(
                    current=conv(current),
                    min90=conv(min90),
                    avg90=conv(avg90),
                    max90=conv(max90),
                    series=""
                )

    except Exception as e:
        logger.warning(f"KEEPA ERROR: {e}")
        return None

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

# ================= PUBLIC =================

async def get_price_data(asin: str) -> PriceData:
    if USE_KEEPA and KEEPA_API_KEY:
        k = await get_keepa_data(asin)

        if k and k.current and k.min90 and k.avg90:
            return PriceData(
                price_now=k.current,
                lowest_90=k.min90,
                avg_90=k.avg90,
                max_90=k.max90 or k.current,
                forecast_7d=k.current,
                lo_7d=k.current,
                hi_7d=k.current,
                likely_days=3,
                state="LIVE",
                advice="",
            )

    # fallback
    return mock_prices_from_asin(asin)

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

def suggest_thresholds(asin: str):
    pdata = mock_prices_from_asin(asin)
    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(lowest_90 * 1.02, 2)

    return [s1, s2, s3]