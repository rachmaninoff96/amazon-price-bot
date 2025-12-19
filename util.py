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

# ============================================================
# CONFIG
# ============================================================

USE_KEEPA: bool = os.getenv("USE_KEEPA", "0") == "1"
KEEPA_API_KEY: str = os.getenv("KEEPA_API_KEY", "").strip()
KEEPA_DOMAIN_ID: int = int(os.getenv("KEEPA_DOMAIN_ID", "8"))  # amazon.it

# Cache per evitare chiamate duplicate
_PRICE_CACHE: Dict[str, Tuple[float, "KeepaStats90"]] = {}
PRICE_CACHE_TTL_SECONDS = 300  # 5 minuti

logger.warning(
    "PRICING INIT | USE_KEEPA=%s | KEEPA_API_KEY present=%s | domainId=%s | CACHE_TTL=%ss",
    USE_KEEPA,
    bool(KEEPA_API_KEY),
    KEEPA_DOMAIN_ID,
    PRICE_CACHE_TTL_SECONDS,
)

# ============================================================
# DATA
# ============================================================

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
    # stato logico
    state: str  # "GOOD_NOW" | "MONITOR" | "RIGID"
    advice: str


@dataclass(frozen=True)
class KeepaStats90:
    # valori in EUR già convertiti
    current: float
    min90: float
    avg90: float
    max90: float
    # quale serie abbiamo scelto per coerenza
    series: str  # "AMAZON" | "NEW"


# ============================================================
# MOCK (fallback)
# ============================================================

def mock_prices_from_asin(asin: str) -> PriceData:
    base = sum(ord(c) for c in asin)

    price_now = 19.9 + (base % 280) + ((base % 9) * 0.1)
    price_now = round(price_now, 2)

    pct = 0.05 + (base % 26) / 100.0
    lowest_90 = round(price_now * (1 - pct), 2)

    # per mock inventiamo avg/max “coerenti”
    avg_90 = round((price_now + lowest_90) / 2, 2)
    max_90 = round(max(price_now, avg_90) * 1.05, 2)

    forecast = round(price_now * 0.98, 2)
    lo = round(max(lowest_90, forecast * 0.97), 2)
    hi = round(min(max_90, forecast * 1.03), 2)

    likely_days = 1 + (base % 7)

    # logica semplice
    state = "MONITOR"
    advice = "💡 Consiglio: imposta una soglia raggiungibile e lasciami monitorare."

    return PriceData(
        price_now=price_now,
        lowest_90=lowest_90,
        avg_90=avg_90,
        max_90=max_90,
        forecast_7d=forecast,
        lo_7d=lo,
        hi_7d=hi,
        likely_days=likely_days,
        state=state,
        advice=advice,
    )


# ============================================================
# KEEPA
# ============================================================

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


def _series_value(arr, idx_amazon: int, idx_new: int) -> Tuple[Optional[float], Optional[float]]:
    """
    Ritorna (amazon_value, new_value) per quello stesso campo (current/min/avg/max)
    """
    a = _keepa_price_to_eur(_safe_get(arr, idx_amazon))
    n = _keepa_price_to_eur(_safe_get(arr, idx_new))
    return a, n


async def _fetch_keepa_stats_90_raw(asin: str) -> dict:
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
                    return await resp.json()
                except Exception:
                    raise RuntimeError(f"JSON parse failed: {raw_text[:300]}")
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout while calling Keepa")


def _pick_consistent_series(stats: dict) -> KeepaStats90:
    """
    IMPORTANTISSIMO:
    - scegliamo UNA serie (AMAZON oppure NEW)
    - poi prendiamo current/min/avg/max TUTTI dalla stessa serie
    Così evitiamo assurdità tipo avg < min.
    """
    # In Keepa stats arrays: index 0 = Amazon, index 1 = New (Marketplace New)
    # (non è BuyBox “pura” – per BuyBox spesso serve buybox param che costa token extra).
    idx_amazon, idx_new = 0, 1

    cur_a, cur_n = _series_value(stats.get("current") or [], idx_amazon, idx_new)
    min_a, min_n = _series_value(stats.get("min") or [], idx_amazon, idx_new)
    avg_a, avg_n = _series_value(stats.get("avg") or [], idx_amazon, idx_new)
    max_a, max_n = _series_value(stats.get("max") or [], idx_amazon, idx_new)

    # scegliamo serie:
    # - preferisci NEW se disponibile (spesso è quello che l’utente vede davvero come “prezzo nuovo”)
    # - altrimenti AMAZON
    # - se manca tutto, errore
    if cur_n is not None:
        series = "NEW"
        cur = cur_n
        mn = min_n if min_n is not None else cur
        av = avg_n if avg_n is not None else cur
        mx = max_n if max_n is not None else max(cur, av, mn)
    elif cur_a is not None:
        series = "AMAZON"
        cur = cur_a
        mn = min_a if min_a is not None else cur
        av = avg_a if avg_a is not None else cur
        mx = max_a if max_a is not None else max(cur, av, mn)
    else:
        raise RuntimeError("No current price available (neither NEW nor AMAZON)")

    # protezioni di coerenza (mai avg < min, mai max < avg)
    mn = float(mn)
    av = float(av)
    mx = float(mx)
    cur = float(cur)

    if av < mn:
        av = mn
    if mx < av:
        mx = av
    if cur <= 0:
        raise RuntimeError("Invalid current price")

    return KeepaStats90(current=round(cur, 2), min90=round(mn, 2), avg90=round(av, 2), max90=round(mx, 2), series=series)


def _price_band_targets(cur: float) -> Tuple[float, float]:
    """
    Ritorna (min_abs_eur, min_pct)
    - min_abs_eur: risparmio minimo percepito in €
    - min_pct: risparmio minimo percepito in %
    """
    if cur <= 30:
        return 2.0, 0.06   # ~2€ o ~6%
    if cur <= 150:
        return 7.0, 0.05   # ~7€ o ~5%
    if cur <= 1000:
        return 30.0, 0.035 # ~30€ o ~3.5%
    return 100.0, 0.025    # ~100€ o ~2.5%


def _classify_and_recommend(cur: float, mn: float, av: float, mx: float) -> Tuple[str, str, float, int, float, float, float, int]:
    """
    Ritorna:
      state, advice, recommended_threshold, rec_days, saving_pct,
      forecast7, lo7, hi7, likely_days
    """
    spread = max(0.0, mx - mn)
    rel_spread = spread / cur if cur > 0 else 0.0

    # Stato A: prezzo già ottimo (entro 1% dal minimo)
    if cur <= mn * 1.01:
        state = "GOOD_NOW"
        advice = (
            "✅ Questo è già uno dei prezzi migliori recenti. "
            "Se ti serve davvero, valuta di comprarlo ora. "
            "Se vuoi tentare il colpo, puoi comunque monitorarlo."
        )
    else:
        # rigidità: spread piccolo rispetto al prezzo
        if rel_spread < 0.02:  # <2% di oscillazione negli ultimi 90gg
            state = "RIGID"
            advice = (
                "📊 Prezzo molto stabile: potrebbe offrire poche occasioni. "
                "Se non scende mai, valuta anche prodotti simili/alternativi nella stessa categoria."
            )
        else:
            state = "MONITOR"
            advice = "💡 Consiglio: imposta una soglia raggiungibile e lasciami monitorare."

    # forecast 7gg: “ritorno verso media” (soft)
    k = 0.35
    forecast = cur - k * (cur - av)

    # range: basato su spread (con minimo sensato)
    band = max(cur * 0.01, spread * 0.18, 1.0)  # min 1€ o 1% o 18% spread
    lo = max(mn, forecast - band)
    hi = min(mx, forecast + band)

    # giorni stimati (euristica)
    if state == "GOOD_NOW":
        likely_days = 7
    else:
        if cur <= av * 1.02:
            likely_days = 3
        elif cur <= av * 1.08:
            likely_days = 5
        else:
            likely_days = 7

    # soglia consigliata (fasce prezzo + raggiungibilità)
    min_abs, min_pct = _price_band_targets(cur)
    min_drop = max(min_abs, cur * min_pct)

    # target “base”:
    # - se sei alto vs media -> vai verso media
    # - se sei vicino alla media -> piccolo sconto
    if cur > av * 1.07:
        target = av
        rec_days = 5
    elif cur > av * 1.03:
        target = av * 0.99
        rec_days = 4
    else:
        target = cur - min_drop
        rec_days = 3

    # non troppo aggressivo: non sotto (quasi) minimo storico
    target = max(target, mn * 1.01)

    # deve essere sotto al current in modo percepibile
    target = min(target, cur - min_drop)

    # se rigid, rendi più “conservativo”: non promettere grandi ribassi
    if state == "RIGID":
        # per rigid suggeriamo un target più vicino (per evitare frustrazione)
        target = max(target, cur - max(min_abs, cur * 0.03))
        rec_days = 7

    # sicurezza
    if target < 0.01:
        target = 0.01
    if target >= cur:
        target = max(0.01, cur - min_abs)

    saving_pct = (cur - target) / cur * 100.0 if cur > 0 else 0.0

    return (
        state,
        advice,
        round(target, 2),
        int(rec_days),
        float(saving_pct),
        round(forecast, 2),
        round(lo, 2),
        round(hi, 2),
        int(likely_days),
    )


async def _get_keepa_stats_90(asin: str) -> KeepaStats90:
    now = time.time()

    cached = _PRICE_CACHE.get(asin)
    if cached:
        ts, stats = cached
        if now - ts <= PRICE_CACHE_TTL_SECONDS:
            return stats

    data = await _fetch_keepa_stats_90_raw(asin)
    products = (data or {}).get("products") or []
    if not products:
        raise RuntimeError("Empty products[]")

    p0 = products[0] or {}
    stats = p0.get("stats")
    if not stats:
        raise RuntimeError("No stats in product")

    parsed = _pick_consistent_series(stats)
    _PRICE_CACHE[asin] = (now, parsed)

    logger.warning(
        "KEEPA OK | asin=%s | series=%s | now=%.2f | min90=%.2f | avg90=%.2f | max90=%.2f",
        asin,
        parsed.series,
        parsed.current,
        parsed.min90,
        parsed.avg90,
        parsed.max90,
    )
    return parsed


# ============================================================
# PUBLIC API
# ============================================================

async def get_price_data(asin: str) -> PriceData:
    """
    Entry point unico: prezzi + insight + consigli.
    """
    if USE_KEEPA and KEEPA_API_KEY:
        try:
            st = await _get_keepa_stats_90(asin)

            state, advice, rec_thr, rec_days, rec_pct, forecast, lo, hi, likely_days = _classify_and_recommend(
                st.current, st.min90, st.avg90, st.max90
            )

            return PriceData(
                price_now=st.current,
                lowest_90=st.min90,
                avg_90=st.avg90,
                max_90=st.max90,
                forecast_7d=forecast,
                lo_7d=lo,
                hi_7d=hi,
                likely_days=likely_days,
                state=state,
                advice=advice,
            )
        except Exception as e:
            logger.warning("KEEPA FAILED -> fallback mock | asin=%s | reason=%s", asin, str(e))

    return mock_prices_from_asin(asin)


async def get_recommended_threshold(asin: str):
    """
    Ritorna (recommended_price, days, saving_pct, state, advice)
    """
    pdata = await get_price_data(asin)

    # per ricavare la soglia consigliata ricalcoliamo (stessa logica)
    state, advice, rec_thr, rec_days, rec_pct, *_ = _classify_and_recommend(
        pdata.price_now, pdata.lowest_90, pdata.avg_90, pdata.max_90
    )
    return rec_thr, rec_days, rec_pct, state, advice


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
# SOGLIE "CLASSICHE" (se vuoi mantenerle come prima)
# ============================================================

def suggest_thresholds(asin: str):
    pdata = mock_prices_from_asin(asin)
    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(max(lowest_90, price_now * 0.88), 2)

    return [s1, s2, s3]