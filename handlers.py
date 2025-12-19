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
    get_recommended_threshold,
    affiliate_link_it,
    auto_short_name_from_url,
    expand_amazon_url,
    suggest_thresholds,
)

logger = logging.getLogger(__name__)
router = Router()

# Stati temporanei
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


def kb_product_actions(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Acquista su Amazon", url=affiliate_link_it(asin))
    kb.button(text="➕ Imposta soglia", callback_data=f"watch:{asin}")
    kb.button(text="🎯 Soglie consigliate", callback_data=f"suggest:{asin}")
    kb.button(text="✍️ Rinomina", callback_data=f"rename:{asin}")
    kb.button(text="🗑️ Elimina", callback_data=f"delete:{asin}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def kb_suggest_thresholds(asin: str):
    s1, s2, s3 = suggest_thresholds(asin)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"−5% → €{s1}", callback_data=f"setthr:{asin}:{s1}")
    kb.button(text=f"−10% → €{s2}", callback_data=f"setthr:{asin}:{s2}")
    kb.button(text=f"Vicino min → €{s3}", callback_data=f"setthr:{asin}:{s3}")
    kb.button(text="⬅️ Indietro", callback_data=f"backprod:{asin}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def kb_quick_threshold(asin: str, rec_price: float):
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Imposta consigliata: €{rec_price:.2f}",
        callback_data=f"setthr:{asin}:{rec_price:.2f}",
    )
    kb.button(text="🎯 Altre soglie", callback_data=f"suggest:{asin}")
    kb.button(text="✍️ Soglia manuale", callback_data=f"watch:{asin}")
    kb.button(text="🛒 Acquista su Amazon", url=affiliate_link_it(asin))
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


# ========== FORMATTER ==========

async def format_price_card(asin: str, url: str) -> str:
    pdata = await get_price_data(asin)

    price_now = pdata.price_now
    lowest_90 = pdata.lowest_90

    # campi legacy (restano “placeholder” finché non li implementiamo davvero con Keepa)
    forecast = pdata.forecast
    lo = pdata.lo
    hi = pdata.hi
    min_date = pdata.min_date
    likely_days = pdata.likely_days

    min_date_str = min_date.strftime("%d/%m/%Y")
    name = find_name_for_asin(asin) or auto_short_name_from_url(url, asin)

    rec_price, rec_days, rec_pct = await get_recommended_threshold(asin)

    txt = (
        f"🛒 <b>{name}</b>\n\n"
        f"💶 Prezzo attuale: <b>€{price_now:.2f}</b>\n"
        f"📉 Minimo 90 giorni: <b>€{lowest_90:.2f}</b> <i>({min_date_str})</i>\n"
        f"📈 Previsione 7gg: <b>€{forecast:.2f}</b> (±5% → {lo:.2f}–{hi:.2f})\n\n"
        f"💡 <b>Consiglio</b>: potrebbe arrivare a <b>€{rec_price:.2f}</b> entro <b>~{rec_days} giorni</b> "
        f"(risparmio ~<b>{rec_pct:.0f}%</b>).\n"
    )
    return txt


async def _render_products_list(items: List[dict], title: str = "📋 <b>I miei prodotti</b>") -> Tuple[str, object]:
    lines = []
    for w in items:
        asin = w["asin"]
        name = w.get("name") or "Prodotto"
        thr = w.get("threshold")

        price_now = (await get_price_data(asin)).price_now

        thr_txt = f"€{thr:.2f}" if isinstance(thr, (int, float)) else "—"
        lines.append(
            f"• <b>{name}</b>\n"
            f"  Prezzo attuale: <b>€{price_now:.2f}</b>\n"
            f"  Soglia: {thr_txt}\n"
        )

    txt = f"{title}\n\n" + ("\n".join(lines) if lines else "—")

    kb = InlineKeyboardBuilder()
    for w in items:
        kb.button(text=w.get("name") or "Prodotto", callback_data=f"manage:{w['asin']}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    return txt, kb.as_markup()


# ========== HANDLERS ==========

@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Benvenuto! Incolla un link Amazon per monitorare un prezzo,\n"
        "oppure usa i pulsanti qui sotto.",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 <b>Home</b>\n\n"
        "Incolla un link Amazon del prodotto che vuoi monitorare,\n"
        "oppure usa i pulsanti.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>Come funziona</b>\n\n"
        "1️⃣ Incolla un link Amazon\n"
        "2️⃣ Imposta una soglia\n"
        "3️⃣ Il bot ti avvisa quando il prezzo scende\n\n"
        "Puoi anche:\n"
        "• Rinominare il prodotto\n"
        "• Eliminare dalla lista\n"
        "• Vedere i prezzi attuali nella lista",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery):
    await c.message.answer(
        "📎 Invia il link Amazon del prodotto che vuoi monitorare.",
        reply_markup=kb_back_home(),
    )
    await c.answer()


@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    chat_id = c.message.chat.id
    items = get_watches_for_chat(chat_id)

    if not items:
        await c.message.edit_text("📭 Non hai prodotti salvati.", reply_markup=kb_home())
        await c.answer()
        return

    txt, kb = await _render_products_list(items)
    await c.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data.startswith("manage:"))
async def cb_manage(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    ensure_watch(c.message.chat.id, asin)
    url = affiliate_link_it(asin)
    card = await format_price_card(asin, url)
    await c.message.edit_text(card, reply_markup=kb_product_actions(asin), parse_mode="HTML")
    await c.answer()


# ========== RINOMINA ==========

@router.callback_query(F.data.startswith("rename:"))
async def cb_rename(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_RENAME[c.message.chat.id] = asin
    await c.message.answer("✍️ Invia il nuovo nome del prodotto:", reply_markup=kb_back_home())
    await c.answer()


# ========== SOGLIA ==========

@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin
    await c.message.answer("✍️ Inserisci la soglia in euro (es. 79.90):", reply_markup=kb_back_home())
    await c.answer()


@router.callback_query(F.data.startswith("suggest:"))
async def cb_suggest(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    await c.message.answer("🎯 Scegli una soglia consigliata:", reply_markup=kb_suggest_thresholds(asin))
    await c.answer()


@router.callback_query(F.data.startswith("setthr:"))
async def cb_setthr(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    thr = float(val)
    name = find_name_for_asin(asin)
    set_or_update_watch(c.message.chat.id, asin, thr, name)
    await c.message.answer(
        f"✅ Soglia impostata a <b>€{thr:.2f}</b>.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


# ========== ELIMINA ==========

@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    chat_id = c.message.chat.id

    WATCHES[chat_id] = [w for w in WATCHES.get(chat_id, []) if w["asin"] != asin]
    save_state()

    await c.message.answer("🗑️ Prodotto eliminato.", reply_markup=kb_home())
    await c.answer()


# ========== CALLBACK DALLA NOTIFICA ==========

@router.callback_query(F.data.startswith("continue:"))
async def cb_continue(c: CallbackQuery):
    await c.answer("Continuo a monitorare 👍", show_alert=False)


@router.callback_query(F.data.startswith("newthr:"))
async def cb_newthr(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin
    await c.message.answer("✍️ Inserisci una nuova soglia:", reply_markup=kb_back_home())
    await c.answer()


# ========== BUGFIX backprod ==========

@router.callback_query(F.data.startswith("backprod:"))
async def cb_backprod(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    ensure_watch(c.message.chat.id, asin)
    url = affiliate_link_it(asin)
    card = await format_price_card(asin, url)
    await c.message.edit_text(card, reply_markup=kb_product_actions(asin), parse_mode="HTML")
    await c.answer()


# ========== MESSAGGI GENERICI ==========

@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()
    chat_id = m.chat.id

    # Rinomina
    if chat_id in PENDING_RENAME:
        asin = PENDING_RENAME.pop(chat_id)
        name = text
        item = get_watch(chat_id, asin)
        thr = item.get("threshold") if item else None
        set_or_update_watch(chat_id, asin, thr, name)
        await m.answer(f"✍️ Nome aggiornato a <b>{name}</b>.", reply_markup=kb_home(), parse_mode="HTML")
        return

    # Soglia manuale
    if chat_id in PENDING_THRESHOLD:
        asin = PENDING_THRESHOLD.pop(chat_id)
        candidate = text.replace(",", ".")
        try:
            value = float(candidate)
        except ValueError:
            await m.answer("⚠️ Inserisci un numero valido (es. 79.90).", reply_markup=kb_back_home())
            return
        name = find_name_for_asin(asin)
        set_or_update_watch(chat_id, asin, value, name)
        await m.answer(
            f"🔔 Ti avviso quando scende sotto <b>€{value:.2f}</b>.",
            reply_markup=kb_home(),
            parse_mode="HTML",
        )
        return

    # Link Amazon
    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, flags=re.I)
        if m_asin:
            asin = m_asin.group(1)
            name = find_name_for_asin(asin) or auto_short_name_from_url(url, asin)
            ensure_watch(chat_id, asin, name)

            card = await format_price_card(asin, url)
            rec_price, _, _ = await get_recommended_threshold(asin)

            await m.answer(card, reply_markup=kb_quick_threshold(asin, rec_price), parse_mode="HTML")
            return

        await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_home())
        return

    # Ricerca base (case-insensitive sui nomi)
    query = text.lower().strip()
    if query:
        all_items = get_watches_for_chat(chat_id)
        matches = []
        for w in all_items:
            name = (w.get("name") or "").lower()
            if query in name:
                matches.append(w)

        if matches:
            txt, kb = await _render_products_list(matches, title="🔍 <b>Risultati trovati</b>")
            await m.answer(txt, reply_markup=kb, parse_mode="HTML")
            return

        await m.answer("Nessun prodotto trovato con questo nome.", reply_markup=kb_home())
        return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_home())