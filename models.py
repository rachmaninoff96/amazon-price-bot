import json
from pathlib import Path
from typing import Dict, List, Optional

# Percorso nuovo: data/watches.json
DATA_PATH = Path("data") / "watches.json"
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_state() -> Dict[int, List[dict]]:
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                # chiavi chat_id come int
                return {int(k): v for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_state(state: Optional[Dict[int, List[dict]]] = None):
    """
    Salva lo stato su file in modo atomico.
    """
    if state is None:
        state = WATCHES
    tmp = DATA_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_PATH)


# Stato in memoria (stessa struttura di prima)
WATCHES: Dict[int, List[dict]] = load_state()


def get_watches_for_chat(chat_id: int) -> List[dict]:
    return WATCHES.get(chat_id, [])


def get_watch(chat_id: int, asin: str) -> Optional[dict]:
    for w in WATCHES.get(chat_id, []):
        if w.get("asin") == asin:
            return w
    return None


def ensure_watch(chat_id: int, asin: str, name: Optional[str] = None) -> dict:
    """
    Garantisce che esista una voce per (chat_id, asin).
    Se non esiste, la crea con threshold=None.
    """
    WATCHES.setdefault(chat_id, [])
    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            if name and not w.get("name"):
                w["name"] = name
                save_state()
            return w
    w = {"asin": asin, "threshold": None, "last_notified_ts": 0, "name": name or ""}
    WATCHES[chat_id].append(w)
    save_state()
    return w


def set_or_update_watch(
    chat_id: int, asin: str, threshold: Optional[float], name: Optional[str]
):
    """
    Imposta/aggiorna soglia e/o nome per un prodotto.
    Resetta last_notified_ts (come facevi prima).
    """
    WATCHES.setdefault(chat_id, [])
    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            w["threshold"] = threshold
            if name is not None:
                w["name"] = name
            w["last_notified_ts"] = 0
            save_state()
            return

    # se non c'è, crea nuova voce
    WATCHES[chat_id].append(
        {
            "asin": asin,
            "threshold": threshold,
            "last_notified_ts": 0,
            "name": name or "",
        }
    )
    save_state()


def find_name_for_asin(asin: str) -> Optional[str]:
    for items in WATCHES.values():
        for w in items:
            if w.get("asin") == asin and w.get("name"):
                return w["name"]
    return None

