import logging
import time
from aiogram import Bot

from models import WATCHES, save_state, find_name_for_asin
from util import mock_prices_from_asin, affiliate_link_it

logger = logging.getLogger(__name__)


async def run_price_check_iteration(bot: Bot):
    """
    Esegue una singola iterazione del watcher.
    Da invocare tramite endpoint /watcher-tick (cron esterno).
    Implementa il sistema intelligente:
        - notifica ingresso sotto soglia
        - notifica se il prezzo scende ancora di più
        - reset quando il prezzo risale sopra la soglia
    """
    now = time.time()

    if not WATCHES:
        logger.info("Nessun prodotto da controllare (WATCHES vuoto).")
        return

    for chat_id, items in list(WATCHES.items()):
        for w in items:
            asin = w["asin"]
            threshold = w.get("threshold")

            if not isinstance(threshold, (int, float)):
                continue

            # ============================ PREZZO CORRENTE ============================
            price_now, *_ = mock_prices_from_asin(asin)

            last_notified_price = w.get("last_notified_price")
            # può essere None o float

            # ============================ RESET SE RISALITO ============================
            if price_now > threshold:
                # prezzo tornato sopra soglia → reset
                if last_notified_price is not None:
                    w["last_notified_price"] = None
                    w["last_notified_ts"] = 0
                    save_state()
                continue

            # ============================ PREZZO SOTTO SOGLIA ============================
            if last_notified_price is None:
                # prima volta sotto soglia
                await send_threshold_notice(bot, chat_id, w, price_now)
                w["last_notified_price"] = price_now
                w["last_notified_ts"] = now
                save_state()
                continue

            # ============================ NUOVO MINIMO ============================
            if price_now < last_notified_price:
                # prezzo sceso ulteriormente → nuova notifica
                await send_threshold_notice(bot, chat_id, w, price_now)
                w["last_notified_price"] = price_now
                w["last_notified_ts"] = now
                save_state()
                continue

            # ============================ PREZZO STABILE / RIALZATO MA NON SOPRA ============================
            # nessuna notifica
            continue

    logger.info("Iterazione watcher completata.")


# ============================ FUNZIONE INVIO NOTIFICA ============================

async def send_threshold_notice(bot: Bot, chat_id: int, w: dict, price_now: float):
    asin = w["asin"]
    name = w.get("name") or find_name_for_asin(asin) or "Prodotto"
    url = affiliate_link_it(asin)

    text = (
        f"🎉 <b>Prezzo sotto soglia!</b>\n"
        f"<b>{name}</b>\n"
        f"💶 Ora a <b>€{price_now:.2f}</b>\n"
        f"➡️ {url}"
    )

    try:
        await bot.send_message(chat_id, text)
        logger.info(f"Notifica inviata per {asin} ({name}) a {chat_id}")
    except Exception as e:
        logger.warning(f"Errore inviando notifica a {chat_id}: {e}")
