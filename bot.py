import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from handlers import router
from watcher import run_price_check_iteration

# ================= LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN mancante")

WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = f"/webhook/{TOKEN}"
PORT = int(os.getenv("PORT", "8080"))

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
dp.include_router(router)


# ================= HEALTH ======================
async def health(request: web.Request):
    return web.Response(text="OK")


# ========== ENDPOINT PER IL WATCHER (cron esterno) ==========
async def watcher_tick_handler(request: web.Request):
    logger.info("Watcher tick chiamato")
    try:
        await run_price_check_iteration(bot)
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.exception("Errore watcher:", e)
        return web.json_response({"status": "error", "detail": str(e)}, status=500)


# ================= STARTUP ======================
async def on_startup(app: web.Application):
    if WEBHOOK_BASE_URL:
        url = WEBHOOK_BASE_URL + WEBHOOK_PATH
        logger.info(f"Imposto webhook: {url}")
        try:
            await bot.set_webhook(url)
        except Exception as e:
            logger.exception("Errore set_webhook:", e)
    else:
        logger.warning("WEBHOOK_URL non impostata — il bot non riceverà aggiornamenti")


async def on_shutdown(app: web.Application):
    logger.info("Shutdown — webhook lasciato invariato")


# ================= APP FACTORY ======================
def create_app():
    app = web.Application()

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.router.add_get("/health", health)
    app.router.add_get("/watcher-tick", watcher_tick_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
