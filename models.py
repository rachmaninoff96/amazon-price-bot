import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_PATH = Path("data") / "watches.json"
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

BACKUP_PATH = DATA_PATH.with_suffix(".bak.json")


def load_state() -> Dict[int, List[dict]]:
    if not DATA_PATH.exists():
        return {}

    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    fixed: Dict[int, List[dict]] = {}

    for chat_id_str, items in (raw or {}).items():
        try:
            chat_id = int(chat_id_str)
        except:
            continue

        fixed[chat_id] = []

        for w in (items or []):
            fixed[chat_id].append(
                {
                    "asin": w.get("asin"),
                    "threshold": w.get("threshold"),
                    "last_notified_ts": w.get("last_notified_ts", 0),
                    "last_notified_price": w.get("last_notified_price"),
                    "name": w.get("name", ""),
                }
            )

    return fixed


def save_state(state: Optional[Dict[int, List[dict]]] = None):
    if state is None:
        state = WATCHES

    if DATA_PATH.exists():
        try:
            shutil.copy2(DATA_PATH, BACKUP_PATH)
        except:
            pass

    tmp = DATA_PATH.with_suffix(".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    tmp.replace(DATA_PATH)


WATCHES: Dict[int, List[dict]] = load_state()


# ================= QUERY =================

def get_watches_for_chat(chat_id: int) -> List[dict]:
    return WATCHES.get(chat_id, [])


def get_watch(chat_id: int, asin: str) -> Optional[dict]:
    for w in WATCHES.get(chat_id, []):
        if w.get("asin") == asin:
            return w
    return None


# ================= CREATE =================

def ensure_watch(chat_id: int, asin: str, name: Optional[str] = None) -> dict:
    WATCHES.setdefault(chat_id, [])

    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            # 🔥 FIX IMPORTANTE
            if name and (not w.get("name") or w.get("name") == "Prodotto"):
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
    WATCHES.setdefault(chat_id, [])

    for w in WATCHES[chat_id]:
        if w["asin"] == asin:
            w["threshold"] = threshold

            # 🔥 FIX IMPORTANTE
            if name:
                w["name"] = name

            w["last_notified_ts"] = 0
            w["last_notified_price"] = None

            save_state()
            return

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


# ================= NAME =================

def find_name_for_asin(asin: str) -> Optional[str]:
    for items in WATCHES.values():
        for w in items:
            if w.get("asin") == asin and w.get("name"):
                return w["name"]
    return None