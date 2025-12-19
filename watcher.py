import time
import logging

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import WATCHES, save_state, find_name_for_asin
from util import get_price_data, affiliate_link_it

logger = logging.getLogger(__name__)

# ✅ Limite massimo controlli per tick (ogni ora)
MAX_CHECKS_PER_TICK = 40

# ✅ Anti-spam notifiche (come già avevi)
NOTIFY_COOLDOWN_SECONDS = 12 * 3600


def _cb_continua(asin: str):
    return f"continue:{asin}"


def _cb_new_threshold(asin: str):
    return f"newthr:{asin}"


def _cb_delete(asin: str):
    return f"delete:{asin}"


def watcher_notification_keyboard(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Acquista su Amazon", url=affiliate_link_it(asin))
    kb.button(text="🔄 Continua a monitorare", callback_data=_cb_continua(asin))
    kb.button(text="⚙️ Imposta nuova soglia", callback_data=_cb_new_threshold(asin))
    kb.button(text="🗑️ Rimuovi", callback_data=_cb_delete(asin))
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def _priority_bucket(last_price: float | None, threshold: float, now_ts: float, last_checked_ts: float | int | None):
    """
    Calcola una priorità "semplice" SENZA chiamare Keepa.
    Usa l'ultimo prezzo già visto (cache) e quanto tempo è passato dall'ultimo check.
    Bucket più basso = più importante.

    Bucket:
      0 = sotto soglia o quasi sotto (<= +1%)
      1 = vicino (<= +5%)
      2 = medio (<= +15%) o prezzo sconosciuto
      3 = lontano (> +15%)
    """
    # se non abbiamo mai controllato, mettiamo last_checked molto vecchio per farlo entrare prima possibile
    if not last_checked_ts:
        age = 10**12
    else:
        age = max(0, now_ts - float(last_checked_ts))

    # se non conosciamo last_price, lo consideriamo "medio" ma con age alto entra comunque a rotazione
    if last_price is None:
        bucket = 2
        # score: bucket, -age (più vecchio = prima)
        return (bucket, -age)

    # gap percentuale sopra soglia
    gap = (last_price - threshold) / threshold if threshold != 0 else 999

    if last_price <= threshold or gap <= 0.01:
        bucket = 0
    elif gap <= 0.05:
        bucket = 1
    elif gap <= 0.15:
        bucket = 2
    else:
        bucket = 3

    return (bucket, -age)


async def run_price_check_iteration(bot: Bot):
    """
    UNA singola iterazione del watcher.
    Chiamata tramite GET /watcher-tick (cron esterno).

    Obiettivo: essere reattivo senza sforare token Keepa:
    - Selezioniamo massimo MAX_CHECKS_PER_TICK prodotti da controllare
    - Li scegliamo con priorità basata su cache (last_checked_price / last_checked_ts)
    - Facciamo al massimo MAX_CHECKS_PER_TICK chiamate prezzo
    """
    now = time.time()

    # 1) costruiamo lista candidati (solo soglia attiva)
    candidates = []
    total_with_threshold = 0

    for chat_id, items in WATCHES.items():
        for w in items:
            threshold = w.get("threshold")
            if not isinstance(threshold, (int, float)):
                continue
            total_with_threshold += 1

            asin = w.get("asin")
            if not asin:
                continue

            last_checked_price = w.get("last_checked_price")  # può non esistere: ok
            last_checked_ts = w.get("last_checked_ts", 0)

            score = _priority_bucket(
                last_price=last_checked_price,
                threshold=float(threshold),
                now_ts=now,
                last_checked_ts=last_checked_ts,
            )
            candidates.append((score, chat_id, w, asin, float(threshold)))

    if not candidates:
        logger.info("Watcher tick: nessun prodotto con soglia attiva.")
        return

    # 2) ordiniamo per priorità e prendiamo i primi MAX_CHECKS_PER_TICK
    candidates.sort(key=lambda x: x[0])
    selected = candidates[:MAX_CHECKS_PER_TICK]

    logger.info(
        "Watcher tick: soglie attive=%d | selezionati=%d (max=%d)",
        total_with_threshold,
        len(selected),
        MAX_CHECKS_PER_TICK,
    )

    # 3) per ciascuno dei selezionati facciamo 1 fetch prezzo e applichiamo logica notifica
    for _, chat_id, w, asin, threshold in selected:
        # fetch prezzo (mock oggi, Keepa domani)
        price_now = get_price_data(asin).price_now

        # aggiorniamo cache (serve per la priorità dei tick successivi)
        w["last_checked_price"] = price_now
        w["last_checked_ts"] = now

        last_price = w.get("last_notified_price")
        last_ts = w.get("last_notified_ts", 0)

        # Anti-spam: almeno 12 ore tra le notifiche
        if now - float(last_ts or 0) < NOTIFY_COOLDOWN_SECONDS:
            continue

        name = w.get("name") or find_name_for_asin(asin) or "Prodotto"

        delta = price_now - threshold

        # --- Caso 1: SOTTO SOGLIA ---
        if price_now <= threshold:
            if last_price is None or last_price != price_now:
                text = (
                    "🎉 <b>Prezzo sotto soglia!</b>\n"
                    f"<b>{name}</b>\n"
                    f"Prezzo attuale: <b>€{price_now:.2f}</b>\n"
                    f"Soglia: €{threshold:.2f}\n\n"
                    f"Link: {affiliate_link_it(asin)}"
                )
                await bot.send_message(
                    chat_id,
                    text,
                    reply_markup=watcher_notification_keyboard(asin),
                    parse_mode="HTML",
                )
                w["last_notified_price"] = price_now
                w["last_notified_ts"] = now
                save_state()
            continue

        # --- Caso 2: QUASI SOTTO SOGLIA (entro 1%) ---
        if 0 < delta <= price_now * 0.01:
            if last_price is None or last_price != price_now:
                text = (
                    "⚠️ <b>Prezzo quasi sotto soglia!</b>\n"
                    f"<b>{name}</b>\n"
                    f"Prezzo: <b>€{price_now:.2f}</b>\n"
                    f"Soglia: €{threshold:.2f}\n"
                    f"Differenza: €{delta:.2f}\n\n"
                    f"Link: {affiliate_link_it(asin)}"
                )
                await bot.send_message(
                    chat_id,
                    text,
                    reply_markup=watcher_notification_keyboard(asin),
                    parse_mode="HTML",
                )
                w["last_notified_price"] = price_now
                w["last_notified_ts"] = now
                save_state()
            continue

    # salviamo anche la cache last_checked_* (utile per la priorità)
    save_state()
    logger.info("Watcher iteration done.")