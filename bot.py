import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties
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

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
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
    try:
        await bot.set_webhook(full_url)
        logger.info("Webhook impostato correttamente.")
    except Exception as e:
        logger.exception("Errore impostando il webhook: %s", e)
        # Non facciamo crashare l'app: almeno /health continua a rispondere


async def on_shutdown(app: web.Application):
    # Non tocchiamo il webhook in shutdown:
    # così Telegram continua a sapere dove mandare gli update
    logger.info("Shutdown app (lascio il webhook così com'è).")


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

