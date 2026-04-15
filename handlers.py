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
    find_name_for_asin,
    WATCHES,
    save_state,
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

PENDING_THRESHOLD: Dict[int, Dict] = {}

# ================= HOME =================

def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Aggiungi prodotto", callback_data="add")
    kb.button(text="📋 I miei prodotti", callback_data="list")
    kb.button(text="ℹ️ Aiuto", callback_data="help")
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

@router.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    await c.message.edit_text("🏠 Incolla un link Amazon", reply_markup=kb_home())
    await c.answer()

@router.callback_query(F.data == "add")
async def add(c: CallbackQuery):
    await c.message.answer("📎 Incolla il link Amazon", reply_markup=kb_only_home())
    await c.answer()

@router.callback_query(F.data == "help")
async def help_cb(c: CallbackQuery):
    await c.message.answer(
        "ℹ️ Ti aiuto a capire quando comprare un prodotto.\n\n"
        "Incolla un link Amazon e ti do un consiglio.",
        reply_markup=kb_only_home(),
    )
    await c.answer()

# ================= ANALISI =================

async def format_price_card(asin: str, url: str):
    pdata = await get_price_data(asin)
    name = auto_short_name_from_url(url, asin)

    decision, _ = simple_price_decision(pdata.price_now, pdata.lowest_90)
    suggested = round(pdata.lowest_90 * 1.10, 2)

    kb = InlineKeyboardBuilder()

    if decision == "GOOD":
        text = (
            f"🟢 <b>Buon momento per comprare</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n\n"
            f"È vicino ai prezzi più bassi recenti.\n"
            f"Se ti serve, conviene prenderlo ora."
        )
        kb.button(text="🛒 Compra", url=affiliate_link_it(asin))

    elif decision == "NORMAL":
        text = (
            f"🟡 <b>Prezzo nella norma</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n\n"
            f"Non è un affare, ma potrebbe scendere."
        )
        kb.button(text="🔔 Avvisami quando scende", callback_data=f"watch:{asin}:{suggested}")
        kb.button(text="🛒 Compra", url=affiliate_link_it(asin))

    else:
        text = (
            f"🔴 <b>Non è un buon momento per comprare</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"💶 €{pdata.price_now:.2f}\n\n"
            f"Questo prodotto si trova spesso a meno.\n"
            f"Posso avvisarti quando torna conveniente."
        )
        kb.button(text="🔔 Avvisami quando scende", callback_data=f"watch:{asin}:{suggested}")
        kb.button(text="🛒 Compra comunque", url=affiliate_link_it(asin))

    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    return text, kb.as_markup()

# ================= WATCH FLOW =================

@router.callback_query(F.data.startswith("watch:"))
async def watch(c: CallbackQuery):
    _, asin, suggested = c.data.split(":")
    suggested = float(suggested)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Usa soglia consigliata", callback_data=f"setthr:{asin}:{suggested}")
    kb.button(text="✏️ Inserisci soglia personalizzata", callback_data=f"manual:{asin}:{suggested}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.answer(
        f"🔔 Ti avviso quando torna conveniente (circa €{suggested:.2f})\n\n"
        f"Puoi usare questa soglia oppure scegliere tu:",
        reply_markup=kb.as_markup()
    )
    await c.answer()

@router.callback_query(F.data.startswith("manual:"))
async def manual_input(c: CallbackQuery):
    _, asin, suggested = c.data.split(":")
    PENDING_THRESHOLD[c.message.chat.id] = {
        "asin": asin,
        "suggested": float(suggested),
    }

    await c.message.answer("✏️ Inserisci la tua soglia:", reply_markup=kb_only_home())
    await c.answer()

@router.callback_query(F.data.startswith("setthr:"))
async def setthr(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    val = float(val)

    set_or_update_watch(c.message.chat.id, asin, val)

    await c.message.answer(f"🔔 Ok, ti avviso sotto €{val:.2f}", reply_markup=kb_only_home())
    await c.answer()

# ================= INPUT SOGLIA =================

@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()
    chat_id = m.chat.id

    if chat_id in PENDING_THRESHOLD:
        data = PENDING_THRESHOLD.pop(chat_id)

        try:
            value = float(text.replace(",", "."))
        except:
            await m.answer("⚠️ Inserisci un numero valido", reply_markup=kb_only_home())
            return

        asin = data["asin"]
        suggested = data["suggested"]

        pdata = await get_price_data(asin)

        if value < pdata.lowest_90 * 0.8:
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Sì, usala", callback_data=f"confirm:{asin}:{value}")
            kb.button(text="🔄 Usa soglia consigliata", callback_data=f"setthr:{asin}:{suggested}")
            kb.button(text="✏️ Cambia soglia", callback_data=f"manual:{asin}:{suggested}")
            kb.button(text="🏠 Home", callback_data="home")
            kb.adjust(1)

            await m.answer(
                "⚠️ Questa soglia è molto difficile da raggiungere.\n\n"
                "Negli ultimi mesi non è mai sceso così tanto.\n\n"
                "Vuoi comunque usarla?",
                reply_markup=kb.as_markup()
            )
            return

        if value > pdata.price_now * 0.98:
            await m.answer(
                "🤔 Potresti essere avvisato subito con questa soglia.",
                reply_markup=kb_only_home()
            )

        set_or_update_watch(chat_id, asin, value)

        await m.answer(f"🔔 Ok, ti avviso sotto €{value:.2f}", reply_markup=kb_only_home())
        return

    # LINK AMAZON
    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

        if m_asin:
            asin = m_asin.group(1)
            text, kb = await format_price_card(asin, url)
            await m.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_only_home())

# ================= CONFIRM =================

@router.callback_query(F.data.startswith("confirm:"))
async def confirm(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    val = float(val)

    set_or_update_watch(c.message.chat.id, asin, val)

    await c.message.answer(f"🔔 Ok, ti avviso sotto €{val:.2f}", reply_markup=kb_only_home())
    await c.answer()

# ================= LIST =================

@router.callback_query(F.data == "list")
async def list_cb(c: CallbackQuery):
    items = get_watches_for_chat(c.message.chat.id)
    tracked = [w for w in items if w.get("threshold")]

    if not tracked:
        await c.message.edit_text("📭 Nessun prodotto monitorato.", reply_markup=kb_home())
        return

    text = "📋 <b>Prodotti monitorati</b>\n\n"
    kb = InlineKeyboardBuilder()

    for w in tracked:
        name = w.get("name") or "Prodotto"
        thr = w.get("threshold")
        asin = w["asin"]

        text += f"• {name} → €{thr:.2f}\n"
        kb.button(text=name, callback_data=f"manage:{asin}")

    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()

# ================= MANAGE =================

@router.callback_query(F.data.startswith("manage:"))
async def manage(c: CallbackQuery):
    asin = c.data.split(":")[1]
    pdata = await get_price_data(asin)

    name = find_name_for_asin(asin) or "Prodotto"
    thr = None

    for w in WATCHES.get(c.message.chat.id, []):
        if w["asin"] == asin:
            thr = w.get("threshold")

    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Compra", url=affiliate_link_it(asin))
    kb.button(text="🔔 Modifica avviso", callback_data=f"watch:{asin}:{thr}")
    kb.button(text="🗑️ Rimuovi", callback_data=f"delete:{asin}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.edit_text(
        f"<b>{name}</b>\n\n💶 €{pdata.price_now:.2f}\n🎯 Avviso a €{thr:.2f}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await c.answer()

# ================= DELETE =================

@router.callback_query(F.data.startswith("delete:"))
async def delete(c: CallbackQuery):
    asin = c.data.split(":")[1]
    chat_id = c.message.chat.id

    WATCHES[chat_id] = [w for w in WATCHES.get(chat_id, []) if w["asin"] != asin]
    save_state()

    await c.message.answer("🗑️ Prodotto rimosso", reply_markup=kb_only_home())
    await c.answer()