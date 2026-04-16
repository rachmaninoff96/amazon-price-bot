import re
import logging
from typing import Dict

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models import (
    get_watches_for_chat,
    set_or_update_watch,
    ensure_watch,
    get_watch,
)

from util import (
    get_price_data,
    affiliate_link_it,
    expand_amazon_url,
    simple_price_decision,
    get_amazon_title,
)

logger = logging.getLogger(__name__)
router = Router()

PENDING_THRESHOLD: Dict[int, str] = {}
PENDING_RENAME: Dict[int, str] = {}

# ================= UI =================

def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Aggiungi prodotto", callback_data="add")
    kb.button(text="📋 I miei prodotti", callback_data="list")
    kb.adjust(1)
    return kb.as_markup()

def kb_only_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()

# ================= START =================

@router.message(CommandStart())
async def start(m: Message):
    await m.answer("👋 Incolla un link Amazon", reply_markup=kb_home())

# ================= ANALISI =================

async def format_price_card(asin: str, url: str):
    pdata = await get_price_data(asin)

    title = await get_amazon_title(url)
    name = title if title else f"Prodotto {asin}"

    decision, _ = simple_price_decision(pdata.price_now, pdata.lowest_90)
    suggested = round(pdata.lowest_90 * 1.10, 2)

    kb = InlineKeyboardBuilder()

    if decision == "GOOD":
        text = f"🟢 Ottimo momento\n\n{name}\n€{pdata.price_now:.2f}"
        kb.button(text="🛒 Compra", url=affiliate_link_it(asin))

    else:
        text = f"🔴 Prezzo alto\n\n{name}\n€{pdata.price_now:.2f}"
        kb.button(text="🔔 Avvisami quando scende", callback_data=f"watch:{asin}:{suggested}")

    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    return text, kb.as_markup(), name

# ================= WATCH =================

@router.callback_query(F.data.startswith("watch:"))
async def watch(c: CallbackQuery):
    _, asin, suggested = c.data.split(":")
    suggested = float(suggested)

    name = f"Prodotto {asin}"

    ensure_watch(c.message.chat.id, asin, name)
    set_or_update_watch(c.message.chat.id, asin, suggested, name)

    await c.message.answer(f"🔔 Ti avviso sotto €{suggested:.2f}", reply_markup=kb_only_home())
    await c.answer()

# ================= MESSAGE =================

@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()

    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

        if m_asin:
            asin = m_asin.group(1)

            text, kb, name = await format_price_card(asin, url)

            ensure_watch(m.chat.id, asin, name)

            await m.answer(text, reply_markup=kb)
            return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_only_home())