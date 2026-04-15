import re
import asyncio
import logging
from typing import Dict, List, Tuple

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import (
    get_watches_for_chat,
    ensure_watch,
    set_or_update_watch,
    find_name_for_asin,
    get_watch,
    WATCHES,
    save_state,
)
from util import (
    get_price_data,
    affiliate_link_it,
    auto_short_name_from_url,
    expand_amazon_url,
    suggest_thresholds,
    simple_price_decision,
)

logger = logging.getLogger(__name__)
router = Router()

PENDING_THRESHOLD: Dict[int, str] = {}
PENDING_RENAME: Dict[int, str] = {}


# ========== UI ==========

def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Home", callback_data="home")
    kb.button(text="➕ Aggiungi prodotto", callback_data="add")
    kb.button(text="📋 I miei prodotti", callback_data="list")
    kb.button(text="ℹ️ Aiuto", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()


def kb_back_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


# ========== FORMATTER NUOVO (CORE TEST) ==========

async def format_price_card(asin: str, url: str):
    pdata = await get_price_data(asin)
    name = find_name_for_asin(asin) or auto_short_name_from_url(url, asin)

    decision, ratio = simple_price_decision(pdata.price_now, pdata.lowest_90)

    # fallback sicurezza
    if decision == "UNKNOWN":
        text = (
            f"🛒 <b>{name}</b>\n\n"
            f"💶 Prezzo attuale: <b>€{pdata.price_now:.2f}</b>\n"
            f"📉 Minimo 90gg: <b>€{pdata.lowest_90:.2f}</b>\n\n"
            f"⚠️ Dati insufficienti."
        )
        return text, kb_home()

    # 🟢 OTTIMO
    if decision == "GOOD":
        text = (
            f"🟢 <b>Ottimo prezzo</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 Prezzo attuale: <b>€{pdata.price_now:.2f}</b>\n"
            f"📉 Minimo 90gg: €{pdata.lowest_90:.2f}\n\n"
            f"È vicino ai livelli più bassi recenti.\n\n"
            f"Se ti serve, è un buon momento per acquistare."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 Acquista su Amazon", url=affiliate_link_it(asin))
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()

    # 🟡 NORMALE
    elif decision == "NORMAL":
        text = (
            f"🟡 <b>Prezzo nella norma</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 Prezzo attuale: <b>€{pdata.price_now:.2f}</b>\n"
            f"📉 Minimo 90gg: €{pdata.lowest_90:.2f}\n\n"
            f"Non è un affare, ma nemmeno alto.\n\n"
            f"Se non hai fretta, potrebbe scendere un po’."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🔔 Avvisami quando scende", callback_data=f"watch:{asin}")
        kb.button(text="🛒 Acquista comunque", url=affiliate_link_it(asin))
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()

    # 🔴 ALTO
    else:
        suggested = round(pdata.lowest_90 * 1.10, 2)

        text = (
            f"🔴 <b>Prezzo alto</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 Prezzo attuale: <b>€{pdata.price_now:.2f}</b>\n"
            f"📉 Minimo 90gg: €{pdata.lowest_90:.2f}\n\n"
            f"Questo prodotto si trova spesso a un prezzo più basso.\n\n"
            f"Ti conviene aspettare.\n\n"
            f"🎯 <b>Soglia consigliata:</b> €{suggested:.2f}"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🔔 Avvisami alla soglia consigliata", callback_data=f"setthr:{asin}:{suggested}")
        kb.button(text="⚙️ Imposta una tua soglia", callback_data=f"watch:{asin}")
        kb.button(text="🏠 Home", callback_data="home")
        kb.adjust(1)

        return text, kb.as_markup()


# ========== HANDLERS ==========

@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Incolla un link Amazon per analizzare il prezzo.",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 Incolla un link Amazon.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    items = get_watches_for_chat(c.message.chat.id)

    if not items:
        await c.message.edit_text("📭 Nessun prodotto.", reply_markup=kb_home())
        return

    text = "📋 <b>I tuoi prodotti</b>\n\n"
    for w in items:
        text += f"• {w.get('name') or 'Prodotto'}\n"

    await c.message.edit_text(text, reply_markup=kb_home(), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":")[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin

    await c.message.answer("Inserisci soglia prezzo:")
    await c.answer()


@router.callback_query(F.data.startswith("setthr:"))
async def cb_setthr(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    thr = float(val)

    set_or_update_watch(c.message.chat.id, asin, thr)
    await c.message.answer(f"✅ Soglia impostata a €{thr:.2f}")
    await c.answer()


@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()

    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

        if m_asin:
            asin = m_asin.group(1)

            ensure_watch(m.chat.id, asin)
            text, kb = await format_price_card(asin, url)

            await m.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    await m.answer("Incolla un link Amazon 🙂")