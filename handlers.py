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
    mock_prices_from_asin,
    affiliate_link_it,
    auto_short_name_from_url,
    expand_amazon_url,
    suggest_thresholds,
)

logger = logging.getLogger(__name__)

router = Router()

# STATO IN RAM PER LE AZIONI PENDENTI
PENDING_THRESHOLD: Dict[int, str] = {}  # chat_id -> asin
PENDING_RENAME: Dict[int, str] = {}     # chat_id -> asin


# ================ UI HELPERS ================
def kb_home():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Aggiungi prodotto", callback_data="home:add")
    kb.button(text="📋 I miei prodotti", callback_data="home:list")
    kb.button(text="ℹ️ Aiuto", callback_data="home:help")
    kb.adjust(1)
    return kb.as_markup()


def kb_product_actions(asin: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Imposta soglia", callback_data=f"watch:{asin}")
    kb.button(text="🎯 Soglie consigliate", callback_data=f"suggest:{asin}")
    kb.button(text="✍️ Rinomina", callback_data=f"rename:{asin}")
    kb.button(text="🛒 Apri su Amazon", url=affiliate_link_it(asin))
    kb.button(text="📋 Torna ai prodotti", callback_data="home:list")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def kb_back_home():
    kb = InlineKeyboardBuilder()
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


# ================ HANDLERS ================
@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Benvenuto! Questo bot ti aiuta a comprare al prezzo giusto su Amazon.\n\n"
        "Incolla un link prodotto (anche dall'app Amazon) per vedere prezzo attuale, minimo 90g (con data) e una previsione a 7 giorni.\n"
        "Puoi impostare una soglia realistica o usare le <b>Soglie consigliate</b>.\n\n"
        "Disclosure: prezzi possono cambiare in qualsiasi momento.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "home")
@router.callback_query(F.data == "home:help")
@router.callback_query(F.data == "home:add")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text(
        "🏠 <b>Home</b>\n\n"
        "➕ <b>Aggiungi prodotto</b>: incolla qui il link Amazon del prodotto.\n"
        "📋 <b>I miei prodotti</b>: vedi e gestisci gli alert salvati.\n\n"
        "Suggerimento: usa <b>Soglie consigliate</b> per evitare obiettivi irrealistici.",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "home:list")
async def cb_list(c: CallbackQuery):
    chat_id = c.message.chat.id
    items = get_watches_for_chat(chat_id)
    if not items:
        await c.message.edit_text("📭 Non hai prodotti salvati.", reply_markup=kb_back_home())
        await c.answer()
        return

    # Testo riepilogo
    lines = []
    for w in items:
        name = w.get("name") or f"Prodotto {w['asin']}"
        thr = w.get("threshold")
        if isinstance(thr, (int, float)):
            thr_txt = f"€{thr:.2f}"
        else:
            thr_txt = "non impostata"
        lines.append(
            f"• <b>{name}</b>\n"
            f"  ASIN: <code>{w['asin']}</code>\n"
            f"  Soglia: <b>{thr_txt}</b>\n"
        )
    txt = (
        "📋 <b>I miei prodotti</b>\n\n"
        + "\n".join(lines)
        + "\nTocca un prodotto qui sotto per gestirlo."
    )

    # Tastiera con un bottone per prodotto
    kb = InlineKeyboardBuilder()
    for w in items:
        label = w.get("name") or f"Prodotto {w['asin']}"
        kb.button(text=label, callback_data=f"manage:{w['asin']}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data.startswith("manage:"))
async def cb_manage(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    ensure_watch(c.message.chat.id, asin)
    card = format_price_card(asin, f"https://www.amazon.it/dp/{asin}")
    await c.message.edit_text(
        card, parse_mode="HTML", reply_markup=kb_product_actions(asin)
    )
    await c.answer()


@router.message()
async def handle_message(m: Message):
    text = (m.text or "").strip()
    chat_id = m.chat.id

    # Rinomina in attesa?
    if chat_id in PENDING_RENAME:
        asin = PENDING_RENAME[chat_id]
        new_name = text

        # Manteniamo l'eventuale soglia esistente
        from models import get_watch  # import locale per evitare cicli
        w = get_watch(chat_id, asin)
        existing_thr = w.get("threshold") if w else None

        set_or_update_watch(chat_id, asin, existing_thr, new_name)
        del PENDING_RENAME[chat_id]

        await m.answer(
            f"✍️ Nome aggiornato per <code>{asin}</code>: <b>{new_name}</b>",
            parse_mode="HTML",
            reply_markup=kb_home(),
        )
        return

    # Soglia in attesa?
    if chat_id in PENDING_THRESHOLD:
        asin = PENDING_THRESHOLD[chat_id]
        candidate = text.replace(",", ".")
        try:
            value = float(candidate)
        except ValueError:
            await m.answer(
                "⚠️ Inserisci un numero, per esempio 79.90", reply_markup=kb_back_home()
            )
            return
        name = find_name_for_asin(asin)
        set_or_update_watch(chat_id, asin, value, name)
        await m.answer(
            f"✅ Ok! Ti avviso quando <code>{asin}</code> scende sotto <b>€{value:.2f}</b>.",
            parse_mode="HTML",
            reply_markup=kb_home(),
        )
        del PENDING_THRESHOLD[chat_id]
        return

    # Espandi eventuale link corto (amzn.eu, amzn.to, ecc.)
    url_for_parsing = text
    if "http" in text:
        url_for_parsing = await expand_amazon_url(text)

    # Parsing link Amazon (dopo eventuale espansione)
    match = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url_for_parsing, flags=re.IGNORECASE)
    if re.search(r"amazon\.", url_for_parsing, flags=re.IGNORECASE) and match:
        asin = match.group(1).upper()
        name_existing = find_name_for_asin(asin)
        name_auto = name_existing or auto_short_name_from_url(url_for_parsing, asin)

        # Crea/aggiorna il prodotto con nome auto anche se l'utente non rinomina
        ensure_watch(chat_id, asin, name_auto)

        card = format_price_card(asin, url_for_parsing)
        await m.answer(card, parse_mode="HTML", reply_markup=kb_product_actions(asin))
        return

    # Non Amazon
    await m.answer(
        "Incolla un link Amazon del prodotto che vuoi monitorare 🙂",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_THRESHOLD[c.message.chat.id] = asin
    await c.message.answer(
        f"✍️ Inserisci la <b>soglia in euro</b> per <code>{asin}</code> (es. 79.90):",
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data.startswith("rename:"))
async def cb_rename(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    PENDING_RENAME[c.message.chat.id] = asin
    await c.message.answer(
        f"✍️ Invia il <b>nuovo nome</b> per <code>{asin}</code> (es. 'Rasoio Andis ProFoil'):",
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data.startswith("suggest:"))
async def cb_suggest(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    await c.message.answer(
        "🎯 Soglie consigliate (scegline una):", reply_markup=kb_suggest_thresholds(asin)
    )
    await c.answer()


@router.callback_query(F.data.startswith("setthr:"))
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


@router.callback_query(F.data.startswith("backprod:"))
async def cb_backprod(c: CallbackQuery):
    asin = c.data.split(":", 1)[1]
    card = format_price_card(asin, f"https://www.amazon.it/dp/{asin}")
    await c.message.edit_text(
        card, parse_mode="HTML", reply_markup=kb_product_actions(asin)
    )
    await c.answer()

