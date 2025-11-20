import re
from datetime import datetime, timedelta
from typing import Optional

import aiohttp


# ============================================================
#  MOCK PREZZI (stile Keepa)
# ============================================================

def mock_prices_from_asin(asin: str):
    """
    Generatore di prezzi fittizi stabile, utile finché non usi Keepa.
    """
    base = sum(ord(c) for c in asin)

    # prezzo attuale
    price_now = 19.9 + (base % 280) + ((base % 9) * 0.1)
    price_now = round(price_now, 2)

    # minimo 90 giorni
    pct = 0.05 + (base % 26) / 100.0  # tra 5% e 30%
    lowest_90 = round(price_now * (1 - pct), 2)

    # data del minimo
    days_ago = (base % 90) + 1
    min_date = datetime.now() - timedelta(days=days_ago)

    # previsione 7 giorni
    delta_pct = ((base % 15) - 7) / 100.0
    forecast = round(price_now * (1 + delta_pct), 2)
    lo = round(forecast * 0.95, 2)
    hi = round(forecast * 1.05, 2)

    likely_days = 1 + (base % 7)

    return price_now, lowest_90, forecast, lo, hi, min_date, likely_days


# ============================================================
#  AFFILIATE LINK
# ============================================================

def affiliate_link_it(asin: str, tag: str = "tuo-tag-21"):
    return f"https://www.amazon.it/dp/{asin}?tag={tag}"


# ============================================================
#  NOME AUTOMATICO DAL LINK AMAZON
# ============================================================

def clean_text(name: str) -> str:
    """
    Pulisce e accorcia il testo in modo sicuro.
    """
    name = re.sub(r"[-_/]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 60:
        name = name[:60].rstrip() + "…"
    return name


def auto_short_name_from_url(url: str, asin: str) -> str:
    """
    Estrarre un nome umano dal link Amazon.
    Funziona molto meglio del tuo precedente sistema.
    """
    try:
        # caso 1: /dp/qualcosa/slug
        m = re.search(r"/dp/[^/]+/([^/?#]+)", url, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1)).title()

        # caso 2: path prima di /dp/
        m = re.search(r"amazon\.[^/]+/([^/]+)/dp/", url, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1)).title()

        # caso 3: keywords=xxx
        m = re.search(r"[\?&]keywords=([^&]+)", url, flags=re.IGNORECASE)
        if m:
            kw = m.group(1).replace("+", " ").strip()
            return clean_text(kw).title()

    except Exception:
        pass

    # fallback sicuro
    return f"Prodotto"


# ============================================================
#  EXPAND LINK CORTI AMAZON
# ============================================================

async def expand_amazon_url(text: str) -> str:
    """
    Segue redirect dei link corti amzn.to, amzn.eu, amzn.*.
    Restituisce il link finale.
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


# ============================================================
#  SOGLIE CONSIGLIATE
# ============================================================

def suggest_thresholds(asin: str):
    price_now, lowest_90, *_ = mock_prices_from_asin(asin)

    s1 = round(price_now * 0.95, 2)
    s2 = round(price_now * 0.90, 2)
    s3 = round(max(lowest_90, price_now * 0.88), 2)

    return [s1, s2, s3]
