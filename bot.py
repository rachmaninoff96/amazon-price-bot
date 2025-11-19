import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from handlers import router
from watcher import run_price_check_iteration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN mancante (env)")

# BASE URL del tuo servizio (es: https://amazon-price-bot-9fjb.onrender.com)
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL", "https://amazon-price-bot-9fjb.onrender.com")

WEBHOOK_PATH = f"/webhook/{TOKEN}"
PORT = int(os.getenv("PORT", "8080"))

bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
dp.include_router(router)


async def health(request: web.Request):
    return web.Response(text="OK")


async def watcher_tick_handler(request: web.Request):
    """
    Endpoint per eseguire una sola iterazione del watcher.
    Da chiamare via cron esterno (UptimeRobot, cron-job.org, ecc.).
    """
    logger.info("Watcher tick richiesto via HTTP")
    try:
        await run_price_check_iteration(bot)
    except Exception as e:
        logger.exception("Errore in watcher_tick: %s", e)
        return web.json_response({"status": "error", "detail": str(e)}, status=500)
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application):
    full_url = WEBHOOK_BASE_URL + WEBHOOK_PATH
    logger.info("Imposto webhook: %s", full_url)
    await bot.set_webhook(full_url)
    logger.info("Webhook impostato correttamente.")


async def on_shutdown(app: web.Application):
    logger.info("Rimuovo webhook…")
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook rimosso.")


def create_app() -> web.Application:
    app = web.Application()

    # handler webhook Telegram
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # healthcheck e watcher
    app.router.add_get("/health", health)
    app.router.add_get("/watcher-tick", watcher_tick_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)

