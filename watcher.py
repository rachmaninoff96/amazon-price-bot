import time
import logging

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import WATCHES, save_state, find_name_for_asin
from util import mock_prices_from_asin, affiliate_link_it

logger = logging.getLogger(__name__)


def _cb_continua(asin: str):
    return f"continue:{asin}"


def _cb_new_threshold(asin: str):
    return f"newthr:{asin}"


def _cb_delete(asin: str):
    return f"delete:{asin}"


def watcher_notification_keyboard(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Continua a monitorare", callback_data=_cb_continua(asin))
    kb.button(text="⚙️ Imposta nuova soglia", callback_data=_cb_new_threshold(asin))
    kb.button(text="🗑️ Rimuovi", callback_data=_cb_delete(asin))
    kb.adjust(1)
    return kb.as_markup()


async def run_price_check_iteration(bot: Bot):
    """
    UNA singola iterazione del watcher.
    Chiamata tramite GET /watcher-tick (cron esterno).
    """
    now = time.time()

    for chat_id, items in list(WATCHES.items()):
        for w in items:
            asin = w["asin"]
            threshold = w.get("threshold")
            if not isinstance(threshold, (int, float)):
                continue

            price_now, lowest_90, *_ = mock_prices_from_asin(asin)
            last_price = w.get("last_notified_price")
            last_ts = w.get("last_notified_ts", 0)

            # Anti-spam: almeno 12 ore tra le notifiche
            if now - last_ts < 12 * 3600:
                continue

            name = w.get("name") or find_name_for_asin(asin) or f"Prodotto"

            # Calcolo differenza soglia
            delta = price_now - threshold
            ratio = delta / price_now if price_now != 0 else 1

            # --- Caso 1: SOTTO SOGLIA ---
            if price_now <= threshold:
                # notifica solo se il prezzo è cambiato oppure mai notificato
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

    logger.info("Watcher iteration done.")