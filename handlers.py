import re
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
    get_amazon_title,  # 👈 IMPORTANTE
)

logger = logging.getLogger(__name__)
router = Router()

PENDING_THRESHOLD: Dict[int, str] = {}
PENDING_RENAME: Dict[int, str] = {}

# ================= UI =================

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

def kb_product_actions(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Acquista", url=affiliate_link_it(asin))
    kb.button(text="🔔 Avvisami", callback_data=f"watch:{asin}")
    kb.button(text="✏️ Rinomina", callback_data=f"rename:{asin}")
    kb.button(text="🗑️ Elimina", callback_data=f"delete:{asin}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()

# ================= FORMATTER =================

async def format_price_card(asin: str, url: str) -> str:
    pdata = await get_price_data(asin)

    # 🔥 FIX NOME
    title = await get_amazon_title(url)
    name = title or find_name_for_asin(asin) or auto_short_name_from_url(url, asin)

    txt = (
        f"🛒 <b>{name}</b>\n\n"
        f"💶 Prezzo attuale: <b>€{pdata.price_now:.2f}</b>\n"
        f"📉 Minimo 90 giorni: <b>€{pdata.lowest_90:.2f}</b>\n\n"
    )

    return txt

# ================= LIST =================

async def _render_products_list(items: List[dict]) -> Tuple[str, object]:
    lines = []

    for w in items:
        name = w.get("name") or "Prodotto"
        thr = w.get("threshold")

        thr_txt = f"€{thr:.2f}" if isinstance(thr, (int, float)) else "—"

        lines.append(
            f"• <b>{name}</b>\n"
            f"  Soglia: {thr_txt}\n"
        )

    txt = "📋 <b>I miei prodotti</b>\n\n" + ("\n".join(lines) if lines else "—")

    kb = InlineKeyboardBuilder()
    for w in items:
        kb.button(text=w.get("name") or "Prodotto", callback_data=f"manage:{w['asin']}")

    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    return txt, kb.as_markup()

# ================= HANDLERS =================

@router.message(CommandStart())
async def start(m: Message):
    await m.answer("👋 Incolla un link Amazon", reply_markup=kb_home())

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 <b>Home</b>\n\nIncolla un link Amazon",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()

@router.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery):
    await c.message.answer("📎 Invia link Amazon", reply_markup=kb_back_home())
    await c.answer()

@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    items = get_watches_for_chat(c.message.chat.id)

    if not items:
        await c.message.edit_text("📭 Nessun prodotto.", reply_markup=kb_home())
        await c.answer()
        return

    txt, kb = await _render_products_list(items)
    await c.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@router.callback_query(F.data.startswith("manage:"))
async def cb_manage(c: CallbackQuery):
    asin = c.data.split(":")[1]

    card = await format_price_card(asin, f"https://amazon.it/dp/{asin}")

    await c.message.edit_text(
        card,
        reply_markup=kb_product_actions(asin),
        parse_mode="HTML",
    )
    await c.answer()

@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":")[1]

    pdata = await get_price_data(asin)
    thr = round(pdata.lowest_90 * 1.10, 2)

    title = await get_amazon_title(f"https://amazon.it/dp/{asin}")
    name = title or f"Prodotto {asin}"

    ensure_watch(c.message.chat.id, asin, name)
    set_or_update_watch(c.message.chat.id, asin, thr, name)

    await c.message.answer(f"🔔 Ti avviso sotto €{thr:.2f}", reply_markup=kb_home())
    await c.answer()

@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(c: CallbackQuery):
    asin = c.data.split(":")[1]
    chat_id = c.message.chat.id

    WATCHES[chat_id] = [w for w in WATCHES.get(chat_id, []) if w["asin"] != asin]
    save_state()

    await c.message.answer("🗑️ Eliminato", reply_markup=kb_home())
    await c.answer()

@router.callback_query(F.data.startswith("rename:"))
async def cb_rename(c: CallbackQuery):
    asin = c.data.split(":")[1]
    PENDING_RENAME[c.message.chat.id] = asin

    await c.message.answer("✏️ Nuovo nome:", reply_markup=kb_back_home())
    await c.answer()

@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()
    chat_id = m.chat.id

    # rename
    if chat_id in PENDING_RENAME:
        asin = PENDING_RENAME.pop(chat_id)

        watch = get_watch(chat_id, asin)
        thr = watch.get("threshold") if watch else None

        set_or_update_watch(chat_id, asin, thr, text)

        await m.answer("✅ Nome aggiornato", reply_markup=kb_home())
        return

    # link
    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

        if m_asin:
            asin = m_asin.group(1)

            card = await format_price_card(asin, url)

            await m.answer(
                card,
                reply_markup=kb_product_actions(asin),
                parse_mode="HTML",
            )
            return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_home())