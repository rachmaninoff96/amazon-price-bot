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
    get_amazon_title,
    simple_price_decision,
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

def smart_price_decision(price_now: float, lowest_90: float, avg_90: float):
    if not price_now or not lowest_90:
        return "NORMAL", ""

    ratio = price_now / lowest_90
    spread = (avg_90 - lowest_90) / lowest_90 if lowest_90 > 0 else 0

    if ratio <= 1.15:
        if spread < 0.10:
            return "BUY", "stabile"
        else:
            return "BUY", "volatile"

    elif ratio <= 1.35:
        if spread > 0.25:
            return "NORMAL", "volatile"
        else:
            return "NORMAL", "stabile"

    else:
        return "HIGH", ""

async def format_price_card(asin: str, url: str) -> str:
    pdata = await get_price_data(asin)

    # nuova logica
    state, mode = smart_price_decision(
        pdata.price_now,
        pdata.lowest_90,
        pdata.avg_90
    )

    # nome prodotto (NON TOCCARE)
    title = await get_amazon_title(url)
    name = title or find_name_for_asin(asin) or auto_short_name_from_url(url, asin)

    # ===== MESSAGGI =====

    if state == "BUY":
        if mode == "stabile":
            header = "🟢 Ottimo prezzo"
            desc = (
                "È vicino ai minimi e il prezzo è abbastanza stabile.\n"
                "👉 Se ti serve, conviene comprarlo ora."
            )
        else:
            header = "🟢 Buon prezzo"
            desc = (
                "È vicino ai minimi recenti, ma questo prodotto oscilla nel tempo.\n"
                "👉 Potrebbe scendere ancora, ma è comunque un buon prezzo."
            )

    elif state == "NORMAL":
        if mode == "volatile":
            header = "🟡 Prezzo nella norma"
            desc = (
                "Questo prodotto varia spesso di prezzo.\n"
                "👉 Se non hai fretta, può valere la pena aspettare."
            )
        else:
            header = "🟡 Prezzo nella norma"
            desc = (
                "Non è un affare, ma nemmeno alto.\n"
                "👉 Se puoi aspettare, potrebbe scendere."
            )

    else:
        header = "🔴 Prezzo alto"
        desc = (
            f"Minimo registrato: €{pdata.lowest_90:.2f} (raro)\n"
            f"Prezzo buono tipico: ~€{float(pdata.advice) if pdata.advice else round(pdata.lowest_90 * 1.10, 2):.2f}\n\n"
            "👉 Conviene aspettare."
        )

    txt = (
        f"{header}\n\n"
        f"<b>{name}</b>\n"
        f"💶 €{pdata.price_now:.2f}\n\n"
        f"{desc}"
    )

    return txt

# ================= LIST =================

async def _render_products_list(items: List[dict]) -> Tuple[str, object]:
    lines = []

    for w in items:
        name = w.get("name") or f"Prodotto {w.get('asin')}"
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
        "🏠 <b>TrustPrice</b>\n\n"
        "👋 Incolla un link Amazon per iniziare\n\n"
        "Ti mostro subito:\n"
        "💶 Prezzo attuale\n"
        "📊 Se conviene comprare o aspettare\n\n"
        "🔔 Puoi anche impostare un prezzo e ricevere una notifica quando scende",
        reply_markup=kb_home(),
        parse_mode="HTML",
    )
    await c.answer()

@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.answer(
        "👋 Incolla un link Amazon per iniziare\n\n"
        "Ti mostro subito:\n"
        "💶 Prezzo attuale\n"
        "📊 Se conviene comprare o aspettare\n\n"
        "🔔 Imposta un prezzo e ricevi una notifica quando scende",
        reply_markup=kb_home(),
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

    # 🔥 usa soglia intelligente
    smart_threshold = pdata.advice
    thr = smart_threshold if smart_threshold else round(pdata.lowest_90 * 1.05, 2)

    # 👉 NON salviamo ancora nulla
    # 👉 mostriamo scelta

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Usa soglia consigliata", callback_data=f"setauto:{asin}:{thr}")
    kb.button(text="✏️ Inserisci soglia manuale", callback_data=f"setmanual:{asin}")
    kb.button(text="🏠 Home", callback_data="home")
    kb.adjust(1)

    await c.message.answer(
        f"🔔 Ti avviso quando il prezzo torna conveniente\n"
        f"(circa €{thr:.2f})\n\n"
        f"Puoi usare questa soglia oppure inserirne una tua.",
        reply_markup=kb.as_markup()
    )

    await c.answer()

@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(c: CallbackQuery):
    asin = c.data.split(":")[1]
    chat_id = c.message.chat.id

    WATCHES[chat_id] = [w for w in WATCHES.get(chat_id, []) if w["asin"] != asin]
    save_state()

    await c.message.answer("🗑️ Eliminato", reply_markup=kb_home())
    await c.answer()

@router.callback_query(F.data.startswith("setauto:"))
async def cb_setauto(c: CallbackQuery):
    _, asin, val = c.data.split(":")
    thr = float(val)
    chat_id = c.message.chat.id

    # 🔥 FIX BLOCCO
    PENDING_THRESHOLD.pop(chat_id, None)

    url = f"https://amazon.it/dp/{asin}"
    title = await get_amazon_title(url)

    watch = get_watch(chat_id, asin)
    name = watch.get("name") if watch else None

    if not name:
        name = title or f"Prodotto {asin}"

    set_or_update_watch(chat_id, asin, thr, name)

    await c.message.answer(
        f"🔔 Ti avviso sotto €{thr:.2f}",
        reply_markup=kb_home()
    )

    await c.answer()

@router.callback_query(F.data.startswith("setmanual:"))
async def cb_setmanual(c: CallbackQuery):
    asin = c.data.split(":")[1]

    PENDING_THRESHOLD[c.message.chat.id] = asin

    pdata = await get_price_data(asin)
    example_price = round(pdata.price_now * 0.8, 2)

    await c.message.answer(
        f"✏️ Inserisci il prezzo sotto cui vuoi essere avvisato (es. {example_price:.2f}):",
        reply_markup=kb_back_home()
    )

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

    # soglia manuale
    if chat_id in PENDING_THRESHOLD:
        asin = PENDING_THRESHOLD[chat_id]

        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await m.answer("⚠️ Inserisci un numero valido")
            return

        pdata = await get_price_data(asin)

        smart_threshold = float(pdata.advice) if pdata.advice else None
        recommended = smart_threshold if smart_threshold else pdata.lowest_90 * 1.05

        # controllo intelligente rispetto alla soglia consigliata
        if value < recommended * 0.9:
            kb = InlineKeyboardBuilder()
            kb.button(
                text="✅ Usa soglia consigliata",
                callback_data=f"setauto:{asin}:{round(recommended, 2)}"
            )
            kb.button(
                text="✏️ Inserisci nuova soglia",
                callback_data=f"setmanual:{asin}"
            )
            kb.button(text="🏠 Home", callback_data="home")
            kb.adjust(1)

            await m.answer(
                f"⚠️ Questa soglia è molto bassa rispetto allo storico.\n\n"
                f"📉 Minimo registrato: €{pdata.lowest_90:.2f} (raro)\n"
                f"📊 Prezzo realistico quando scende: ~€{recommended:.2f}\n\n"
                f"💡 Quel minimo viene raggiunto solo per brevi periodi.\n"
                f"👉 Con questa soglia potresti NON ricevere notifiche.\n\n"
                f"Vuoi comunque usarla?",
                reply_markup=kb.as_markup()
            )
            return

        url = f"https://amazon.it/dp/{asin}"
        title = await get_amazon_title(url)

        watch = get_watch(chat_id, asin)
        name = watch.get("name") if watch else None

        if not name:
            name = title or f"Prodotto {asin}"

        set_or_update_watch(chat_id, asin, value, name)

        # svuota stato SOLO quando valido
        PENDING_THRESHOLD.pop(chat_id, None)

        await m.answer(
            f"🔔 Ti avviso sotto €{value:.2f}",
            reply_markup=kb_home()
        )
        return



    # link amazon
    if "http" in text:
        url = await expand_amazon_url(text)
        m_asin = re.search(r"(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)

    if m_asin:
        asin = m_asin.group(1)

        title = await get_amazon_title(url)

        if not title:
            await m.answer(
                "❌ Non riesco a leggere questo link Amazon.\n\n"
                "👉 Controlla che sia corretto oppure prova con un altro prodotto.",
                reply_markup=kb_home()
            )
            return

        card = await format_price_card(asin, url)

        await m.answer(
            card,
            reply_markup=kb_product_actions(asin),
            parse_mode="HTML",
        )
        return

    await m.answer("Incolla un link Amazon 🙂", reply_markup=kb_home())