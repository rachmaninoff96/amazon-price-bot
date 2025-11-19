import re
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

# ================ MOCK "stile Keepa" ================


def mock_prices_from_asin(asin: str):
    base = sum(ord(c) for c in asin)
    price_now = 19.9 + (base % 280) + ((base % 9) * 0.1)
    price_now = round(price_now, 2)

    # minimo 90 giorni: tra -5% e -30% dall'attuale
    pct = 0.05 + (base % 26) / 100.0  # 0.05 .. 0.30
    lowest_90 = round(price_now * (1 - pct), 2)

    # data del minimo: 1..90 giorni fa
    days_ago = (base % 90) + 1
    min_date = datetime.now() - timedelta(days=days_ago)

    # previsione 7 giorni: +/- 0..7% dall'attuale
    delta_pct = ((base % 15) - 7) / 100.0
    forecast = round(price_now * (1 + delta_pct), 2)
    lo = round(forecast * 0.95, 2)
    hi = round(forecast * 1.05, 2)

    # stima giorni per toccare un prezzo "intermedio"
    likely_days = 1 + (base % 7)  # 1..7 giorni
    return price_now, lowest_90, forecast, lo, hi, min_date, likely_days


def affiliate_link_it(asin: str, tag: str = "tuo-tag-21"):
    return f"https://www.amazon.it/dp/{asin}?tag={tag}"


def auto_short_name_from_url(url: str, asin: str) -> str:
    """
    Tenta di proporre un nome breve: usa lo slug prima di /dp/, oppure 'keywords=' dalla query.
    Fallback: 'Prodotto {ASIN}'.
    """
    try:
        # prendi pezzo prima di /dp/ e dopo dominio
        m = re.search(r"amazon\.[^/]+/([^?]+?)/dp/", url, flags=re.IGNORECASE)
        if m:
            slug = m.group(1)
            slug = slug.replace("-", " ").replace("/", " ").strip()
            slug = re.sub(r"\s+", " ", slug)
            # accorcia a ~40 char
            if len(slug) > 40:
                slug = slug[:40].rstrip() + "…"
            return slug.title()
        # prova keywords=
        m2 = re.search(r"[\?&]keywords=([^&]+)", url, flags=re.IGNORECASE)
        if m2:
            kw = m2.group(1)
            kw = kw.replace("+", " ")
            if len(kw) > 40:
                kw = kw[:40].rstrip() + "…"
            return kw.title()
    except Exception:
        pass
    return f"Prodotto {asin}"


async def expand_amazon_url(text: str) -> str:
    """
    Se il testo contiene un link corto amzn.* o un link Amazon,
    prova a seguirne i redirect e restituisce l'URL finale.
    Se qualcosa va storto, restituisce il testo originale.
    """
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
    price_now, lowest_90, *_ = mock_prices_from_asin(asin)
    s1 = round(price_now * 0.95, 2)  # -5%
    s2 = round(price_now * 0.90, 2)  # -10%
    s3 = round(max(lowest_90, price_now * 0.88), 2)  # vicino al minimo 90gg
    return [s1, s2, s3]

