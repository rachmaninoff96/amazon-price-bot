import os
import re
import asyncio
import time
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

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
    except:
        pass
    return f"Prodotto {asin}"


# ================ PERSISTENZA SU FILE ================
DATA_PATH = Path("watches.json")


def load_state():
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_state(state):
    tmp = DATA_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(DATA_PATH)


# ================ STATO ================
PENDING_THRESHOLD = {}   # chat_id -> asin
PENDING_RENAME = {}      # chat_id -> asin
WATCHES = load_state()   # chat_id -> list[{asin, threshold, last_notified_ts, name}]
# struttura elemento: {"asin": "...", "threshold": 99.99, "last_notified_ts": 0, "name": "..."}

# ================ BOT SETUP ================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN mancante nel file .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()

WEBHOOK_PATH = f"/webhook/{TOKEN}"              # percorso interno
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # base URL (la metteremo su Render)
PORT = int(os.getenv("PORT", "8080"))           # porta che passa Render

# ================ UI HELPERS ================
def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Aggiungi prodotto", callback_data="home:add")
    kb.button(text="📋 Le mie soglie", callback_data="home:list")
    kb.button(text="ℹ️ Aiuto", callback_data="home:help")
    kb.adjust(1)
    return kb.as_markup()


def kb_product_actions(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Imposta soglia", callback_data=f"watch:{asin}")
    kb.button(text="🎯 Soglie consigliate", callback_data=f"suggest:{asin}")
    kb.button(text="✍️ Rinomina", callback_data=f"rename:{asin}")
    kb.button(text="🛒 Apri su Amazon", url=affiliate_link_it(asin))
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def kb_back_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def format_price_card(asin: str, url: str):
    price_now, lowest_90, forecast, lo, hi, min_date, likely_days = mock_prices_from_asin(asin)
    min_date_str = min_date.strftime("%d/%m/%Y")
    name = find_name_for_asin(asin) or auto_short_name_from_url(url, asin)
    txt = (
        f"🛒 <b>{name}</b>\n"
        f"ASIN: <code>{asin}</code>\n\n"
        f"💶 Prezzo attuale: <b>€{price_now:.2f}</b>\n"
        f"📉 Minimo (90 gg): <b>€{lowest_90:.2f}</b> <i>({min_date_str})</i>\n"
        f"📈 Previsione (7 gg): <b>€{forecast:.2f}</b> <i>(±5% → {lo:.2f}–{hi:.2f})</i>\n\n"
        f"🧠 Stima: potrebbe toccare <b>€{forecast:.2f}</b> circa tra <b>{likely_days} giorni</b>.\n"
        f"Consiglio: imposta una soglia realistica (es. €{max(lowest_90, round(price_now*0.9,2)):.2f})."
    )
    return txt


def suggest_thresholds(asin: str):
    price_now, lowest_90, *_ = mock_prices_from_asin(asin)
    s1 = round(price_now * 0.95, 2)    # -5%
    s2 = round(price_now * 0.90, 2)    # -10%
    s3 = round(max(lowest_90, price_now * 0.88), 2)  # vicino al minimo 90gg
    return [s1, s2, s3]


def kb_suggest_thresholds(asin: str):
    s1, s2, s3 = suggest_thresholds(asin)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"−5% → €{s1}", callback_data=f"setthr:{asin}:{s1}")
    kb.button(text=f"−10% → €{s2}", callback_data=f"setthr:{asin}:{s2}")
    kb.button(text=f"Vicino min → €{s3}", callback_data=f"setthr:{asin}:{s3}")
    kb.button(text="⬅️ Indietro", callback_data=f"backprod:{asin}")
    kb.adjust(1)
    return kb.as_markup()


def find_name_for_asin(asin: str):
    for items in WATCHES.values():
        for w in items:
            if w["asin"] == asin and w.get("name"):
                return w["name"]
    return None


def set_or_update_watch(chat_id: int, asin: str, threshold: float, name: Optional[str]):
    WATCHES.setdefault(chat_id, [])
    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            w["threshold"] = threshold
            if name:
                w["name"] = name
            w["last_notified_ts"] = 0
            save_state(WATCHES)
            return
    WATCHES[chat_id].append(
        {"asin": asin, "threshold": threshold, "last_notified_ts": 0, "name": name or ""}
    )
    save_state(WATCHES)


# ================ HANDLERS ================
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Benvenuto! Questo bot ti aiuta a comprare al prezzo giusto su Amazon.\n\n"
        "Incolla un link prodotto per vedere prezzo attuale, minimo 90g (con data) e una previsione a 7 giorni.\n"
        "Puoi impostare una soglia realistica o usare le <b>Soglie consigliate</b>.\n\n"
        "Disclosure: prezzi possono cambiare in qualsiasi momento.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "home")
@dp.callback_query(F.data == "home:help")
@dp.callback_query(F.data == "home:add")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 <b>Home</b>\n\n"
        "➕ <b>Aggiungi prodotto</b>: incolla qui il link Amazon del prodotto.\n"
        "📋 <b>Le mie soglie</b>: vedi e gestisci gli alert salvati.\n\n"
        "Suggerimento: usa <b>Soglie consigliate</b> per evitare obiettivi irrealistici.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data == "home:list")
async def cb_list(c: CallbackQuery):
    items = WATCHES.get(c.message.chat.id, [])
    if not items:
        await c.message.edit_text("📭 Non hai soglie salvate.", reply_markup=kb_back_home())
        await c.answer()
        return
    lines = []
    for w in items:
        name = w.get("name") or f"Prodotto {w['asin']}"
        lines.append(
            f"• <b>{name}</b>\n"
            f"  ASIN: <code>{w['asin']}</code>\n"
            f"  Soglia: <b>€{w['threshold']:.2f}</b>\n"
        )
    txt = (
        "📋 <b>Le mie soglie</b>\n\n"
        + "\n".join(lines)
        + "\nSeleziona un prodotto inviando di nuovo il link per gestirlo."
    )
    await c.message.edit_text(txt, reply_markup=kb_back_home(), parse_mode="HTML")
    await c.answer()


@dp.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()

    # Rinomina in attesa?
    if m.chat.id in PENDING_RENAME:
        asin = PENDING_RENAME[m.chat.id]
        new_name = text
        WATCHES.setdefault(m.chat.id, [])
        found = False
        for w in WATCHES[m.chat.id]:
            if w["asin"] == asin:
                w["name"] = new_name
                found = True
                break
        if not found:
            WATCHES[m.chat.id].append(
                {"asin": asin, "threshold": 999999.0, "last_notified_ts": 0, "name": new_name}
            )
        save_state(WATCHES)
        del PENDING_RENAME[m.chat.id]
        await m.answer(
            f"✍️ Nome aggiornato per <code>{asin}</code>: <b>{new_name}</b>",
            parse_mode="HTML",
            reply_markup=kb_home(),
        )
        return

    # Soglia in attesa?
    if m.chat.id in PENDING_THRESHOLD:
        asin = PENDING_THRESHOLD[m.chat.id]
        candidate = text.replace(",", ".")
        try:
            value = float(candidate)
        except ValueError:
            await m.answer(
                "⚠️ Inserisci un numero, per esempio 79.90", reply_markup=kb_back_home()
            )
            return
        name = find_name_for_asin(asin)
        set_or_update_watch(m.chat.id, asin, value, name)
        await m.answer(
            f"✅ Ok! Ti avviso quando <code>{asin}</code> scende sotto <b>€{value:.2f}</b>.",
            parse_mode="HTML",
            reply_markup=kb_home(),
        )
        del PENDING_THRESHOLD[m.chat.id]
        return

    # Parsing link Amazon
    match = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", text, flags=re.IGNORECASE)
    if "amazon." in text.lower() and match:
        asin = match.group(1).upper()
        name_existing = find_name_for_asin(asin)
        name_auto = name_existing or auto_short_name_from_url(text, asin)

        card = format_price_card(asin, text)
        await m.answer(card, parse_mode="HTML", reply_markup=kb_product_actions(asin))
        # name_auto per ora lo usiamo solo a livello di card; la soglia salva poi
        return

    await m.answer(
        "Incolla un link Amazon del prodotto che vuoi monitorare 🙂",
        reply_markup=kb_home(),
    )


@dp.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin
    await c.message.answer(
        f"✍️ Inserisci la <b>soglia in euro</b> per <code>{asin}</code> (es. 79.90):",
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rename:"))
async def cb_rename(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_RENAME[c.message.chat.id] = asin
    await c.message.answer(
        f"✍️ Invia il <b>nuovo nome</b> per <code>{asin}</code> (es. 'Rasoio Andis ProFoil'):",
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data.startswith("suggest:"))
async def cb_suggest(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    await c.message.answer(
        "🎯 Soglie consigliate (scegline una):", reply_markup=kb_suggest_thresholds(asin)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("setthr:"))
async def cb_setthr(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    thr = float(val)
    name = find_name_for_asin(asin)
    set_or_update_watch(c.message.chat.id, asin, thr, name)
    await c.message.answer(
        f"✅ Soglia impostata per <code>{asin}</code>: <b>€{thr:.2f}</b>",
        parse_mode="HTML",
        reply_markup=kb_home(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("backprod:"))
async def cb_backprod(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    card = format_price_card(asin, f"https://www.amazon.it/dp/{asin}")
    await c.message.edit_text(
        card, parse_mode="HTML", reply_markup=kb_product_actions(asin)
    )
    await c.answer()


# ================ PRICE WATCHER (mock) ================
async def price_watcher():
    while True:
        try:
            now = time.time()
            for chat_id, items in list(WATCHES.items()):
                for w in items:
                    asin = w["asin"]
                    threshold = w["threshold"]
                    last_ts = w.get("last_notified_ts", 0)
                    if now - last_ts < 12 * 3600:
                        continue
                    price_now, *_ = mock_prices_from_asin(asin)
                    if price_now <= threshold:
                        url = affiliate_link_it(asin)
                        name = w.get("name") or f"Prodotto {asin}"
                        await bot.send_message(
                            chat_id,
                            f"🎉 <b>Sotto soglia!</b>\n"
                            f"{name}\nASIN <code>{asin}</code> ora è <b>€{price_now:.2f}</b>\n➡️ {url}",
                            parse_mode="HTML",
                        )
                        w["last_notified_ts"] = now
                        save_state(WATCHES)
        except Exception:
            pass
        await asyncio.sleep(60)


# ================ AIOHTTP APP (WEBHOOK + /health) ================
async def on_startup(app: web.Application):
    if WEBHOOK_URL:
        full_url = WEBHOOK_URL + WEBHOOK_PATH
        print(f"Imposto webhook: {full_url}")
        await bot.set_webhook(full_url)
    else:
        print("ATTENZIONE: WEBHOOK_URL non impostata, il bot non riceverà aggiornamenti.")

    asyncio.create_task(price_watcher())
    print("Bot in esecuzione (webhook)...")


async def on_shutdown(app: web.Application):
    print("Rimuovo webhook...")
    await bot.delete_webhook()


async def health(request: web.Request):
    return web.Response(text="OK")


def run_telegram_bot():
    app = web.Application()

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # endpoint /health per Render + UptimeRobot
    app.router.add_get("/health", health)

    web.run_app(app, host="0.0.0.0", port=PORT)


# ================ MAIN ================
if __name__ == "__main__":
    run_telegram_bot()
