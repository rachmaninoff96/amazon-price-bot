import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Percorso in data/watches.json
DATA_PATH = Path("data") / "watches.json"
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

# Backup automatico
BACKUP_PATH = DATA_PATH.with_suffix(".bak.json")


# ========== LOAD & SAVE ==========

def load_state() -> Dict[int, List[dict]]:
    """
    Carica lo stato da file, convertendo le chiavi in int.
    Se mancano campi nuovi (es. last_notified_price) li aggiunge automaticamente.

    In caso di errore nel file principale, prova a caricare dal backup.
    """
    if not DATA_PATH.exists():
        logger.warning("watches.json non trovato, inizializzo stato vuoto.")
        return {}

    # 1) Provo a leggere il file principale
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        logger.info("watches.json caricato correttamente.")
    except Exception as e:
        logger.exception("Errore nel leggere watches.json, provo il backup:", exc_info=e)

        # 2) Provo il backup
        if BACKUP_PATH.exists():
            try:
                with open(BACKUP_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                logger.warning("Stato recuperato da backup watches.bak.json.")
            except Exception as e2:
                logger.exception("Errore nel leggere anche il backup, stato inizializzato vuoto:", exc_info=e2)
                return {}
        else:
            logger.error("Nessun backup disponibile, stato inizializzato vuoto.")
            return {}

    # Normalizzazione struttura
    fixed: Dict[int, List[dict]] = {}
    for chat_id_str, items in raw.items():
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            logger.warning(f"Chiave chat_id non valida in watches.json: {chat_id_str!r}")
            continue

        fixed[chat_id] = []

        for w in items:
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

    logger.info("Stato WATCHES inizializzato con %d chat.", len(fixed))
    return fixed


def save_state(state: Optional[Dict[int, List[dict]]] = None):
    """
    Salvataggio atomico dello stato su file, con backup.
    - Se esiste un file watches.json precedente, viene copiato in watches.bak.json
      prima di scrivere il nuovo.
    """
    if state is None:
        state = WATCHES

    # Backup del file esistente
    if DATA_PATH.exists():
        try:
            shutil.copy2(DATA_PATH, BACKUP_PATH)
            logger.info("Backup creato: %s", BACKUP_PATH)
        except Exception as e:
            logger.exception("Impossibile creare il backup di watches.json:", exc_info=e)

    tmp = DATA_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(DATA_PATH)
        logger.info("Stato salvato su %s (chat totali: %d).", DATA_PATH, len(state))
    except Exception as e:
        logger.exception("Errore nel salvataggio dello stato su file:", exc_info=e)


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