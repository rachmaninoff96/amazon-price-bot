import json
from pathlib import Path
from typing import Dict, List, Optional

# Percorso del file JSON
DATA_PATH = Path("data") / "watches.json"
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)


# ====================== CARICAMENTO ======================

def load_state() -> Dict[int, List[dict]]:
    """
    Carica lo stato da watches.json.
    Se manca o è corrotto, restituisce un dizionario vuoto.
    """
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Convertiamo le chiavi chat_id in int
                return {int(k): v for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_state(state: Optional[Dict[int, List[dict]]] = None):
    """
    Salvataggio atomico del file JSON.
    """
    if state is None:
        state = WATCHES

    tmp = DATA_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_PATH)


# ====================== STATO IN MEMORIA ======================

WATCHES: Dict[int, List[dict]] = load_state()


# ====================== FUNZIONI UTILI ======================

def get_watches_for_chat(chat_id: int) -> List[dict]:
    return WATCHES.get(chat_id, [])


def get_watch(chat_id: int, asin: str) -> Optional[dict]:
    for w in WATCHES.get(chat_id, []):
        if w.get("asin") == asin:
            return w
    return None


def delete_watch(chat_id: int, asin: str):
    """
    Rimuove un prodotto dalla lista dell'utente.
    Usato dal tasto “🗑️ Elimina prodotto”.
    """
    items = WATCHES.get(chat_id, [])
    new_items = [w for w in items if w.get("asin") != asin]
    WATCHES[chat_id] = new_items
    save_state()


def ensure_watch(chat_id: int, asin: str, name: Optional[str] = None) -> dict:
    """
    Garantisce che esista un prodotto in WATCHES.
    Se non esiste, lo crea.
    Ha già il nuovo campo `last_notified_price`.
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
        "name": name or "",
        "threshold": None,
        "last_notified_ts": 0,
        "last_notified_price": None,  # nuovo campo
    }
    WATCHES[chat_id].append(w)
    save_state()
    return w


def set_or_update_watch(
    chat_id: int,
    asin: str,
    threshold: Optional[float],
    name: Optional[str]
):
    """
    Imposta o aggiorna soglia e nome.
    Resetta il sistema notifiche intelligente quando la soglia viene aggiornata.
    """
    WATCHES.setdefault(chat_id, [])

    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            if name is not None:
                w["name"] = name

            w["threshold"] = threshold

            # Reset comunicazioni
            w["last_notified_ts"] = 0
            w["last_notified_price"] = None

            save_state()
            return

    # Se non esiste ancora, lo creiamo
    WATCHES[chat_id].append(
        {
            "asin": asin,
            "name": name or "",
            "threshold": threshold,
            "last_notified_ts": 0,
            "last_notified_price": None,
        }
    )
    save_state()


def find_name_for_asin(asin: str) -> Optional[str]:
    """
    Cerca il nome di un ASIN in tutto il database.
    Usato per UI e notifiche.
    """
    for items in WATCHES.values():
        for w in items:
            if w.get("asin") == asin and w.get("name"):
                return w["name"]
    return None
