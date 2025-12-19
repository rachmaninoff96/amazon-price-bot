import time
import logging

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import WATCHES, save_state, find_name_for_asin
from util import get_price_data, affiliate_link_it

logger = logging.getLogger(__name__)

MAX_CHECKS_PER_TICK = 40
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


def _priority_bucket(last_price, threshold: float, now_ts: float, last_checked_ts):
    if not last_checked_ts:
        age = 10**12
    else:
        age = max(0, now_ts - float(last_checked_ts))

    if last_price is None:
        bucket = 2
        return (bucket, -age)

    gap = (float(last_price) - threshold) / threshold if threshold != 0 else 999

    if float(last_price) <= threshold or gap <= 0.01:
        bucket = 0
    elif gap <= 0.05:
        bucket = 1
    elif gap <= 0.15:
        bucket = 2
    else:
        bucket = 3

    return (bucket, -age)


async def run_price_check_iteration(bot: Bot):
    now = time.time()

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

            last_checked_price = w.get("last_checked_price")
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

    candidates.sort(key=lambda x: x[0])
    selected = candidates[:MAX_CHECKS_PER_TICK]

    logger.info(
        "Watcher tick: soglie attive=%d | selezionati=%d (max=%d)",
        total_with_threshold,
        len(selected),
        MAX_CHECKS_PER_TICK,
    )

    changed = False

    for _, chat_id, w, asin, threshold in selected:
        price_now = (await get_price_data(asin)).price_now

        # cache per la priorità
        w["last_checked_price"] = price_now
        w["last_checked_ts"] = now
        changed = True

        last_price = w.get("last_notified_price")
        last_ts = w.get("last_notified_ts", 0)

        if now - float(last_ts or 0) < NOTIFY_COOLDOWN_SECONDS:
            continue

        name = w.get("name") or find_name_for_asin(asin) or "Prodotto"

        delta = price_now - threshold

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
                changed = True
            continue

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
                changed = True
            continue

    if changed:
        save_state()

    logger.info("Watcher iteration done.")