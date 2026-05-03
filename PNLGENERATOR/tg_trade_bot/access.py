import json
import os
from datetime import datetime, timedelta

# DATA_DIR is the directory where access.json and referrals.json are persisted.
# In production this is mounted as a Docker volume (see docker-compose.yml) so the
# files survive container restarts. Defaults to the module directory for local dev.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

ACCESS_FILE = os.path.join(DATA_DIR, "access.json")
REFERRAL_FILE = os.path.join(DATA_DIR, "referrals.json")
PROFILE_FILE = os.path.join(DATA_DIR, "profiles.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
LOGO_DIR     = os.path.join(DATA_DIR, "logos")
os.makedirs(LOGO_DIR, exist_ok=True)
HISTORY_MAX = 5

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


# =====================================================
# DAILY LIMIT (free users)
# =====================================================
_DAILY_USAGE: dict[int, dict] = {}

def check_daily_limit(user_id: int, max_free: int = 3) -> tuple[bool, int]:
    """Check if user exceeded daily free limit. Returns (can_use, remaining)."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _DAILY_USAGE.get(user_id, {})
    if usage.get("date") != today:
        usage = {"date": today, "count": 0}
        _DAILY_USAGE[user_id] = usage
    remaining = max(0, max_free - usage["count"])
    return remaining > 0, remaining

def increment_usage(user_id: int):
    """Increment daily usage counter."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _DAILY_USAGE.get(user_id, {})
    if usage.get("date") != today:
        usage = {"date": today, "count": 0}
        _DAILY_USAGE[user_id] = usage
    usage["count"] += 1
    _DAILY_USAGE[user_id] = usage


# =====================================================
# REFERRAL PROGRAM
# =====================================================
def _load_referrals() -> dict:
    if os.path.exists(REFERRAL_FILE):
        with open(REFERRAL_FILE) as f:
            return json.load(f)
    return {}

def _save_referrals(data: dict):
    with open(REFERRAL_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_referral_code(user_id: int) -> str:
    """Generate unique referral code for user."""
    return f"REF{user_id}"

def use_referral(user_id: int, referral_code: str) -> tuple[bool, str]:
    """Apply referral code. Returns (success, message)."""
    refs = _load_referrals()
    # Extract referrer ID from code
    if not referral_code.startswith("REF"):
        return False, "Неверный код"
    try:
        referrer_id = int(referral_code.replace("REF", ""))
    except ValueError:
        return False, "Неверный код"
    if referrer_id == user_id:
        return False, "Нельзя использовать свой код"
    # Check if already used
    used_by = refs.get(str(referrer_id), {}).get("used_by", [])
    if user_id in used_by:
        return False, "Вы уже использовали этот код"
    # Add referral
    if str(referrer_id) not in refs:
        refs[str(referrer_id)] = {"used_by": [], "rewarded": False}
    refs[str(referrer_id)]["used_by"].append(user_id)
    # Check if referrer earned reward (3 referrals = 7 days)
    if len(refs[str(referrer_id)]["used_by"]) >= 3 and not refs[str(referrer_id)]["rewarded"]:
        grant_access(referrer_id, days=7)
        refs[str(referrer_id)]["rewarded"] = True
        _save_referrals(refs)
        return True, "Код применён! Пригласивший получил 7 дней доступа."
    _save_referrals(refs)
    return True, "Код применён! Спасибо."

def get_referral_stats(user_id: int) -> tuple[int, int]:
    """Returns (total_referrals, needed_for_reward)."""
    refs = _load_referrals()
    data = refs.get(str(user_id), {"used_by": []})
    count = len(data.get("used_by", []))
    return count, max(0, 3 - count)


# =====================================================
# USER PROFILE (saved defaults: username / referral / favorite exchange)
# =====================================================
def get_profile(user_id: int) -> dict:
    """Returns saved profile dict (may contain username/referral/exchange) or {}."""
    if not os.path.exists(PROFILE_FILE):
        return {}
    with open(PROFILE_FILE) as f:
        data = json.load(f)
    return data.get(str(user_id), {})


def update_profile(user_id: int, **fields):
    """Merge non-None fields into saved profile.
    Pass field="" to explicitly clear a single field; pass None to leave unchanged."""
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    p = data.get(str(user_id), {})
    for k, v in fields.items():
        if v is not None:
            p[k] = v
    data[str(user_id)] = p
    with open(PROFILE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def clear_profile(user_id: int):
    """Remove user profile entirely."""
    if not os.path.exists(PROFILE_FILE):
        return
    with open(PROFILE_FILE) as f:
        data = json.load(f)
    if data.pop(str(user_id), None) is not None:
        with open(PROFILE_FILE, "w") as f:
            json.dump(data, f, indent=2)


# =====================================================
# SIGNAL HISTORY (last N rendered cards per user, for "Repeat last")
# =====================================================
def add_history(user_id: int, signal: dict):
    """Save a rendered signal (newest first, capped at HISTORY_MAX)."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    user_history = data.get(str(user_id), [])
    # Strip transient/internal keys (start with _) before persisting.
    entry = {k: v for k, v in signal.items() if not str(k).startswith("_")}
    entry["_ts"] = datetime.now().isoformat()
    user_history.insert(0, entry)
    data[str(user_id)] = user_history[:HISTORY_MAX]
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_history(user_id: int) -> list:
    """Returns user's signal history (newest first)."""
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE) as f:
        data = json.load(f)
    return data.get(str(user_id), [])


# =====================================================
# USER LOGO — optional PNG overlay applied to the bottom-right of every card
# =====================================================
def get_user_logo_path(user_id: int) -> str | None:
    """Returns absolute path to the user's saved logo, or None if not set."""
    p = os.path.join(LOGO_DIR, f"{user_id}.png")
    return p if os.path.exists(p) else None


def has_user_logo(user_id: int) -> bool:
    return os.path.exists(os.path.join(LOGO_DIR, f"{user_id}.png"))


def clear_user_logo(user_id: int):
    p = os.path.join(LOGO_DIR, f"{user_id}.png")
    if os.path.exists(p):
        os.remove(p)
