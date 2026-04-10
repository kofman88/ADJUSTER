import json
import os
from datetime import datetime, timedelta

ACCESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.json")

TRIAL_DAYS = 2
FULL_DAYS = 30

def _load() -> dict:
    if not os.path.exists(ACCESS_FILE):
        return {}
    with open(ACCESS_FILE, "r") as f:
        return json.load(f)

def _save(data: dict):
    with open(ACCESS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user(user_id: int) -> dict | None:
    return _load().get(str(user_id))

def activate_trial(user_id: int) -> bool:
    """Активирует пробный период. Возвращает False если уже был."""
    data = _load()
    key = str(user_id)
    if key in data:
        return False
    data[key] = {
        "type": "trial",
        "expires": (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
    }
    _save(data)
    return True

def grant_access(user_id: int, days: int = FULL_DAYS):
    """Выдаёт полный доступ на N дней (вызывается тобой через /grant)."""
    data = _load()
    key = str(user_id)
    # если уже есть активный доступ — продлеваем от текущей даты истечения
    existing = data.get(key)
    if existing and existing["type"] == "full":
        base = datetime.fromisoformat(existing["expires"])
        if base > datetime.now():
            new_expires = base + timedelta(days=days)
        else:
            new_expires = datetime.now() + timedelta(days=days)
    else:
        new_expires = datetime.now() + timedelta(days=days)
    data[key] = {
        "type": "full",
        "expires": new_expires.isoformat()
    }
    _save(data)

def revoke_access(user_id: int):
    """Отзывает доступ."""
    data = _load()
    data.pop(str(user_id), None)
    _save(data)

def check_access(user_id: int) -> tuple[bool, str]:
    """
    Возвращает (имеет_доступ, причина).
    причина: 'ok', 'trial', 'expired', 'no_access'
    """
    user = get_user(user_id)
    if not user:
        return False, "no_access"
    expires = datetime.fromisoformat(user["expires"])
    if datetime.now() > expires:
        return False, "expired"
    return True, user["type"]

def days_left(user_id: int) -> int:
    user = get_user(user_id)
    if not user:
        return 0
    expires = datetime.fromisoformat(user["expires"])
    delta = expires - datetime.now()
    return max(0, delta.days)
