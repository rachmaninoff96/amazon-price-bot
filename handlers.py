import re
import logging
from typing import Dict

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import (
    get_watches_for_chat,
    ensure_watch,
    set_or_update_watch,
    find_name_for_asin,
)
from util import (
    get_price_data,
    affiliate_link_it,
    auto_short_name_from_url,
    expand_amazon_url,
    simple_price_decision,
)

logger = logging.getLogger(__name__)
router = Router()

PENDING_THRESHOLD: Dict[int, str] = {}

# ================= UI =================

def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Home", callback_data="home")
    kb.button(text="➕ Aggiungi prodotto", callback_data="add")
    kb.button(text="📋 I miei prodotti", callback_data="list")
    kb.button(text="ℹ️ Aiuto", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()


# ================= FORMAT =================

async def format_price_card(asin: str, url: str):
    pdata = await get_price_data(asin)
    name = find_name_for_asin(asin) or auto_short_name_from_url(url, asin)

    decision, _ = simple_price_decision(pdata.price_now, pdata.lowest_90)

    # 🟢 OTTIMO
    if decision == "GOOD":
        text = (
            f"🟢 <b>Ottimo prezzo</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n"
            f"📉 Min: €{pdata.lowest_90:.2f}\n\n"
            f"È vicino ai livelli più bassi.\n"
            f"Se ti serve, compra."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 Acquista", url=affiliate_link_it(asin))
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()

    # 🟡 NORMALE
    elif decision == "NORMAL":
        text = (
            f"🟡 <b>Prezzo nella norma</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n"
            f"📉 Min: €{pdata.lowest_90:.2f}\n\n"
            f"Può scendere."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🔔 Avvisami", callback_data=f"watch:{asin}")
        kb.button(text="🛒 Compra", url=affiliate_link_it(asin))
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()

    # 🔴 ALTO
    else:
        suggested = round(pdata.lowest_90 * 1.10, 2)

        text = (
            f"🔴 <b>Prezzo alto</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n"
            f"📉 Min: €{pdata.lowest_90:.2f}\n\n"
            f"Conviene aspettare.\n\n"
            f"🎯 Soglia: €{suggested:.2f}"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🔔 Avvisami", callback_data=f"setthr:{asin}:{suggested}")
        kb.button(text="⚙️ Soglia manuale", callback_data=f"watch:{asin}")
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()


# ================= HANDLERS =================

@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Incolla un link Amazon",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 Incolla un link Amazon",
        reply_markup=kb_home(),
    )
    await c.answer()


@router.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery):
    await c.message.answer("📎 Incolla il link Amazon", reply_markup=kb_home())
    await c.answer()


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.answer(
        "ℹ️ Incolla un link Amazon e ti dico se conviene comprare o aspettare.",
        reply_markup=kb_home(),
    )
    await c.answer()


# ================= LISTA =================

@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    items = get_watches_for_chat(c.message.chat.id)

    tracked = [w for w in items if w.get("threshold")]

    if not tracked:
        await c.message.edit_text(
            "📭 Nessun prodotto tracciato.",
            reply_markup=kb_home(),
        )
        return

    kb = InlineKeyboardBuilder()
    text = "📋 <b>Prodotti tracciati</b>\n\n"

    for w in tracked:
        name = w.get("name") or "Prodotto"
        asin = w["asin"]

        text += f"• {name}\n"
        kb.button(text=name, callback_data=f"manage:{asin}")

    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()


# ================= GESTIONE =================

@router.callback_query(F.data.startswith("manage:"))
async def cb_manage(c: CallbackQuery):
    asin = c.data.split(":")[1]

    text, kb = await format_price_card(asin, f"https://amazon.it/dp/{asin}")

    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()


# ================= SOGLIA =================

@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":")[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin

    await c.message.answer("Inserisci soglia prezzo:", reply_markup=kb_home())
    await c.answer()


@router.callback_query(F.data.startswith("setthr:"))
async def cb_setthr(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    thr = float(val)

    set_or_update_watch(c.message.chat.id, asin, thr)

    await c.message.answer(f"🔔 Ti avviso sotto €{thr:.2f}", reply_markup=kb_home())
    await c.answer()


# ================= INPUT =================

@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()
    chat_id = m.chat.id

    # SOGLIA
    if chat_id in PENDING_THRESHOLD:
        asin = PENDING_THRESHOLD.pop(chat_id)

        try:
            value = float(text.replace(",", "."))
        except:
            await m.answer("Numero non valido", reply_markup=kb_home())
            return

        set_or_update_watch(chat_id, asin, value)

        await m.answer(f"🔔 Ti avviso sotto €{value:.2f}", reply_markup=kb_home())
        return

    # LINK
    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

        if m_asin:
            asin = m_asin.group(1)

            name = auto_short_name_from_url(url, asin)
            ensure_watch(chat_id, asin, name)

            text, kb = await format_price_card(asin, url)

            await m.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_home())