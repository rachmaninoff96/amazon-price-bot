import time
import logging

from aiogram import Bot

from models import WATCHES, save_state, find_name_for_asin
from util import mock_prices_from_asin, affiliate_link_it

logger = logging.getLogger(__name__)


async def run_price_check_iteration(bot: Bot):
    """
    Esegue UNA sola iterazione di controllo prezzi.
    Pensato per essere chiamato da /watcher-tick via HTTP.
    """
    now = time.time()

    if not WATCHES:
        logger.info("Nessun prodotto in WATCHES.")
        return

    for chat_id, items in list(WATCHES.items()):
        for w in items:
            asin = w["asin"]
            threshold = w.get("threshold")
            if not isinstance(threshold, (int, float)):
                continue

            last_ts = w.get("last_notified_ts", 0)
            # 12 ore tra una notifica e l'altra
            if now - last_ts < 12 * 3600:
                continue

            price_now, *_ = mock_prices_from_asin(asin)
            if price_now <= threshold:
                url = affiliate_link_it(asin)
                name = w.get("name") or find_name_for_asin(asin) or f"Prodotto {asin}"
                text = (
                    "🎉 <b>Sotto soglia!</b>\n"
                    f"{name}\nASIN <code>{asin}</code> ora è <b>€{price_now:.2f}</b>\n"
                    f"➡️ {url}"
                )
                try:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                except Exception as e:
                    logger.warning("Errore inviando messaggio a %s: %s", chat_id, e)
                    continue

                # aggiorna last_notified_ts
                w["last_notified_ts"] = now
                save_state()

    logger.info("Iterazione watcher completata.")

