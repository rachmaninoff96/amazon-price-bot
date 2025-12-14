import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Percorso in data/watches.json
DATA_PATH = Path("data") / "watches.json"
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

# Backup automatico
BACKUP_PATH = DATA_PATH.with_suffix(".bak.json")


def _state_counts(state: Dict[int, List[dict]]) -> tuple[int, int]:
    """Ritorna (numero_chat, numero_prodotti_totali)."""
    chats = len(state)
    items = sum(len(v) for v in state.values()) if state else 0
    return chats, items


# ========== LOAD & SAVE ==========

def load_state() -> Dict[int, List[dict]]:
    """
    Carica lo stato da file, convertendo le chiavi in int.
    Se mancano campi nuovi li aggiunge automaticamente.
    In caso di errore nel file principale, prova a caricare dal backup.
    """
    # Log ambiente e path reali (diagnostica)
    try:
        logger.warning("MODELS BOOT | CWD=%s", os.getcwd())
        logger.warning("MODELS BOOT | DATA_PATH=%s", str(DATA_PATH.resolve()))
        logger.warning("MODELS BOOT | BACKUP_PATH=%s", str(BACKUP_PATH.resolve()))
    except Exception:
        # se qualcosa va storto nel resolve, non blocchiamo il boot
        pass

    if not DATA_PATH.exists():
        logger.warning("LOAD | watches.json non trovato -> stato vuoto.")
        return {}

    # 1) Provo a leggere il file principale
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        logger.info("LOAD | watches.json letto correttamente.")
    except Exception as e:
        logger.exception("LOAD | Errore lettura/parse watches.json -> provo backup.", exc_info=e)

        # 2) Provo il backup
        if BACKUP_PATH.exists():
            try:
                with open(BACKUP_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                logger.warning("LOAD | Stato recuperato da backup watches.bak.json.")
            except Exception as e2:
                logger.exception("LOAD | Errore anche sul backup -> stato vuoto.", exc_info=e2)
                return {}
        else:
            logger.error("LOAD | Nessun backup disponibile -> stato vuoto.")
            return {}

    # Normalizzazione struttura
    fixed: Dict[int, List[dict]] = {}
    for chat_id_str, items in (raw or {}).items():
        try:
            chat_id = int(chat_id_str)
        except Exception:
            logger.warning("LOAD | chiave chat_id non valida: %r", chat_id_str)
            continue

        fixed[chat_id] = []
        for w in (items or []):
            # fix automatico per nuove chiavi
            if "last_notified_price" not in w:
                w["last_notified_price"] = None
            if "last_notified_ts" not in w:
                w["last_notified_ts"] = 0
            if "threshold" not in w:
                w["threshold"] = None
            if "name" not in w:
                w["name"] = ""

            fixed[chat_id].append(w)

    chats, items = _state_counts(fixed)
    logger.warning("LOAD | Stato inizializzato: chat=%d prodotti=%d", chats, items)
    return fixed


def save_state(state: Optional[Dict[int, List[dict]]] = None):
    """
    Salvataggio atomico dello stato su file, con backup e logging difensivo.
    """
    if state is None:
        state = WATCHES

    chats, items = _state_counts(state)

    # WARNING forte se stiamo per salvare vuoto (diagnostico)
    if chats == 0 and items == 0:
        logger.warning("SAVE | ATTENZIONE: sto per salvare uno stato VUOTO ({}).")

    # Backup del file esistente
    if DATA_PATH.exists():
        try:
            shutil.copy2(DATA_PATH, BACKUP_PATH)
            logger.info("SAVE | Backup creato: %s", str(BACKUP_PATH.resolve()))
        except Exception as e:
            logger.exception("SAVE | Impossibile creare backup:", exc_info=e)

    tmp = DATA_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(DATA_PATH)
        logger.warning(
            "SAVE | Stato salvato OK -> %s | chat=%d prodotti=%d",
            str(DATA_PATH.resolve()),
            chats,
            items,
        )
    except Exception as e:
        logger.exception("SAVE | Errore nel salvataggio dello stato:", exc_info=e)


# Stato in memoria
WATCHES: Dict[int, List[dict]] = load_state()


# ========== QUERY FUNCTIONS ==========

def get_watches_for_chat(chat_id: int) -> List[dict]:
    return WATCHES.get(chat_id, [])


def get_watch(chat_id: int, asin: str) -> Optional[dict]:
    for w in WATCHES.get(chat_id, []):
        if w.get("asin") == asin:
            return w
    return None


# ========== CREATE / UPDATE ==========

def ensure_watch(chat_id: int, asin: str, name: Optional[str] = None) -> dict:
    """
    Garantisce l'esistenza di una voce per (chat_id, asin).
    Se esiste, aggiorna il nome se prima era vuoto.
    Se non esiste, la crea con valori default.
    """
    WATCHES.setdefault(chat_id, [])

    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            if name and not w.get("name"):
                w["name"] = name
                save_state()
            return w

    w = {
        "asin": asin,
        "threshold": None,
        "last_notified_ts": 0,
        "last_notified_price": None,
        "name": name or "",
    }
    WATCHES[chat_id].append(w)
    save_state()
    return w


def set_or_update_watch(
    chat_id: int,
    asin: str,
    threshold: Optional[float],
    name: Optional[str] = None,
):
    """
    Imposta/aggiorna soglia e nome.
    Resetta le variabili di notifica per permettere una nuova notifica.
    """
    WATCHES.setdefault(chat_id, [])

    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            w["threshold"] = threshold
            if name is not None:
                w["name"] = name

            # Reset per permettere nuova notifica
            w["last_notified_ts"] = 0
            w["last_notified_price"] = None

            save_state()
            return

    # se non c'è, crea nuovo
    WATCHES[chat_id].append(
        {
            "asin": asin,
            "threshold": threshold,
            "last_notified_ts": 0,
            "last_notified_price": None,
            "name": name or "",
        }
    )
    save_state()


# ========== NAME LOOKUP ==========

def find_name_for_asin(asin: str) -> Optional[str]:
    """
    Cerca un nome per un ASIN a prescindere dalla chat.
    Serve quando un prodotto è stato già visto/monitorato.
    """
    for items in WATCHES.values():
        for w in items:
            if w.get("asin") == asin and w.get("name"):
                return w["name"]
    return None