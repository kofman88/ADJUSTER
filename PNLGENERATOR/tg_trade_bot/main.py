import asyncio
import os
import time
import uuid
import functools
from concurrent.futures import ThreadPoolExecutor
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from cachetools import TTLCache
import aiohttp
from access import (
    activate_trial, grant_access, revoke_access, check_access, days_left,
    check_daily_limit, increment_usage,
    get_referral_code, get_referral_stats, use_referral,
    get_profile, update_profile, clear_profile,
    add_history, get_history,
    get_user_logo_path, has_user_logo, clear_user_logo, LOGO_DIR,
)
from i18n import get_lang, set_lang

ADMIN_ID = int(os.getenv("ADMIN_ID", "445677777"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@kofman88")

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    MenuButtonCommands,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    FSInputFile,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

from configs.fonts import FONTS
from configs.layout import LAYOUT, BYBIT_CUSTOM_LAYOUT
from utils.draw_text import draw_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =====================================================
# ThreadPool для CPU-heavy задач (PIL рендеринг)
# =====================================================
_THREAD_POOL = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)

# =====================================================
# Кэш для цен и точности (TTL 10 сек для цены, 1 час для precision)
# =====================================================
_PRICE_CACHE: TTLCache = TTLCache(maxsize=512, ttl=10)
_PRECISION_CACHE: TTLCache = TTLCache(maxsize=512, ttl=3600)

# =====================================================
# Кэш шрифтов — шрифты грузятся один раз
# =====================================================
@functools.lru_cache(maxsize=64)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)

# =====================================================
# Кэш шаблонов — изображения грузятся один раз
# =====================================================
@functools.lru_cache(maxsize=16)
def _load_template(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")

# =====================================================
# Кэш иконок
# =====================================================
@functools.lru_cache(maxsize=32)
def _load_icon(path: str, size: int) -> Image.Image:
    icon = Image.open(path).convert("RGBA")
    return icon.resize((size, size), Image.LANCZOS)


# =====================================================
# USER LOGO OVERLAY — bottom-right watermark applied AFTER renderer returns,
# so the existing pixel-perfect renderers stay untouched.
# =====================================================
def _apply_user_logo(card_path: str, user_id: int) -> str:
    """If `user_id` has a saved logo, composite it onto the card's bottom-right
    corner at ~12% width with 70% alpha and return a NEW path. If there's no
    logo or anything fails, returns the original path unchanged."""
    logo_path = get_user_logo_path(user_id)
    if not logo_path:
        return card_path
    try:
        card = Image.open(card_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
        target_w = max(80, int(card.width * 0.12))
        scale    = target_w / logo.width
        target_h = max(1, int(logo.height * scale))
        logo = logo.resize((target_w, target_h), Image.LANCZOS)
        # Knock alpha down to 70% so the logo doesn't fight the card content.
        r, g, b, a = logo.split()
        a = a.point(lambda p: int(p * 0.7))
        logo = Image.merge("RGBA", (r, g, b, a))
        margin = max(20, int(card.width * 0.035))
        x = card.width  - logo.width  - margin
        y = card.height - logo.height - margin
        card.alpha_composite(logo, (x, y))
        out = card_path.rsplit(".", 1)[0] + "_wm.png"
        card.convert("RGB").save(out, "PNG", optimize=True)
        return out
    except Exception as e:
        logger.warning(f"Logo overlay failed for user {user_id}: {e}")
        return card_path


# =====================================================
# FSM
# =====================================================
class CustomExchange(StatesGroup):
    template = State()
    username = State()
    side = State()
    status = State()
    symbol = State()
    entry = State()
    exit_price = State()
    leverage = State()
    referral = State()
    datetime_str = State()

class CustomExchangeUSDT(StatesGroup):
    username = State()
    side = State()
    symbol = State()
    entry = State()
    exit_price = State()
    leverage = State()
    deposit = State()

class TradeForm(StatesGroup):
    exchange = State()
    symbol = State()
    side = State()
    entry = State()
    mark = State()
    amount = State()
    deposit = State()
    leverage = State()

class MarathonStatesGroup(StatesGroup):
    start_deposit = State()

class SignalForm(StatesGroup):
    exit_choice = State()
    exchange    = State()
    status      = State()
    template    = State()
    leverage    = State()
    username    = State()
    referral    = State()
    datetime_str = State()
    preview     = State()   # final card draft with [✏️ Edit X] / [✅ Send] / [❌ Cancel]
    edit_value  = State()   # transient — waiting for new text/number for the field being edited

class ProfileForm(StatesGroup):
    edit_value = State()    # transient — waiting for new value for profile field
    set_logo   = State()    # transient — waiting for a photo to use as watermark

BASE_H = 467

def scale_font(size: int, img_h: int) -> int:
    return max(10, int(size * img_h / BASE_H))

def px(val: float, size: int) -> int:
    return int(val * size)

# =====================================================
# BOT (использует Redis для FSM — надёжно при рестарте)
# =====================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =====================================================
# PERSISTENT REPLY KEYBOARD — always visible at the bottom of the chat so the
# user never has to type /start. Buttons send their text as a regular message;
# the dispatcher catches them via menu_button_handler below.
# =====================================================
MENU_BTN_QUICK   = "⚡ Быстрый скрин"
MENU_BTN_SERIES  = "📊 Сводка"
MENU_BTN_HISTORY = "🕘 История"
MENU_BTN_PROFILE = "👤 Профиль"
MENU_BTN_TEXTS = {MENU_BTN_QUICK, MENU_BTN_SERIES, MENU_BTN_HISTORY, MENU_BTN_PROFILE}

MAIN_MENU_RKB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=MENU_BTN_QUICK),   KeyboardButton(text=MENU_BTN_HISTORY)],
        [KeyboardButton(text=MENU_BTN_SERIES),  KeyboardButton(text=MENU_BTN_PROFILE)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Меню или сигнал…",
)


@dp.message(F.text.in_(MENU_BTN_TEXTS))
async def menu_button_handler(msg: Message, state: FSMContext):
    """Highest-priority handler — clears any in-progress FSM and routes the
    user to the requested screen. Registered BEFORE every other @dp.message
    so a tap always wins over partial-FSM input."""
    if await state.get_state() is not None:
        await state.clear()
    btn = msg.text
    if btn == MENU_BTN_QUICK:
        await msg.answer(
            "⚡ <b>Быстрый скрин</b>\n\n"
            "Пришли следующим сообщением сигнал в любом формате — бот сам распознает.\n"
            "Минимум: символ + сторона + цены.\n\n"
            "<b>Пример:</b>\n"
            "<code>BTCUSDT\nШорт\nВход: 78373\nСтоп: 79546\nTP1: 76613</code>",
            parse_mode="HTML",
        )
    elif btn == MENU_BTN_HISTORY:
        await cmd_history(msg)
    elif btn == MENU_BTN_SERIES:
        await cmd_series(msg)
    elif btn == MENU_BTN_PROFILE:
        await cmd_profile(msg)


@dp.message(Command("menu"))
async def cmd_menu(msg: Message):
    """Backup command — same as /start but explicit."""
    await start(msg)
# =====================================================
# МАРАФОН (в памяти — при необходимости перенести в Redis)
# =====================================================
MARATHON: dict[int, dict[str, float]] = {}

# =====================================================
# aiohttp сессия (переиспользуется)
# =====================================================
_HTTP_SESSION: aiohttp.ClientSession | None = None
_HTTP_LOCK = asyncio.Lock()

async def get_http_session() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    async with _HTTP_LOCK:
        if _HTTP_SESSION is None or _HTTP_SESSION.closed:
            connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=5)
            _HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _HTTP_SESSION

# =====================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================
async def safe_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Cannot delete message: {e}")

def _cleanup_old_files(directory: str, prefix: str, max_age_seconds: int = 3600) -> None:
    try:
        now = time.time()
        for fname in os.listdir(directory):
            if fname.startswith(prefix):
                fpath = os.path.join(directory, fname)
                try:
                    if now - os.path.getmtime(fpath) > max_age_seconds:
                        os.remove(fpath)
                except OSError:
                    pass
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")

async def parse_float(message: Message) -> float | None:
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0:
            await message.answer("Число должно быть больше 0 🙏")
            return None
        return val
    except (ValueError, AttributeError):
        await message.answer("Введите число 🙏")
        return None

# =====================================================
# КЛАВИАТУРЫ (предсозданные — не пересоздавать каждый раз)
# =====================================================
restart_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🔁 В начало", callback_data="restart")]]
)
exchange_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="⚫ Bybit", callback_data="exchange_bybit"),
        InlineKeyboardButton(text="🔵 BingX", callback_data="exchange_bingx"),
    ]]
)
side_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="📈 Long", callback_data="side_long"),
        InlineKeyboardButton(text="📉 Short", callback_data="side_short"),
    ]]
)
back_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
)
mark_price_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📡 Взять цену с биржи", callback_data="get_mark_price")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ]
)
skip_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_field")]]
)

# BingX custom-template chooser ----------------------------------------------
BINGX_TEMPLATES = ("football", "curve", "doge")
BINGX_TEMPLATE_LABELS = {
    "football": "⚽ Футбол",
    "curve":    "📉 Свеча",
    "doge":     "🐕 Доге",
}

bingx_template_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text=BINGX_TEMPLATE_LABELS["football"], callback_data="bingx_tpl:football"),
        InlineKeyboardButton(text=BINGX_TEMPLATE_LABELS["curve"],    callback_data="bingx_tpl:curve"),
        InlineKeyboardButton(text=BINGX_TEMPLATE_LABELS["doge"],     callback_data="bingx_tpl:doge"),
    ]]
)

# Open vs closed position chooser (controls "Нереализованная П/У" / "Реализованная П/У"
# header and "Последняя цена" / "Цена закрытия" label)
bingx_status_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Открытая", callback_data="bingx_st:open"),
        InlineKeyboardButton(text="🏁 Закрытая", callback_data="bingx_st:closed"),
    ]]
)

_MAIN_KB_MARKUP: InlineKeyboardMarkup | None = None

def get_main_kb() -> InlineKeyboardMarkup:
    global _MAIN_KB_MARKUP
    if _MAIN_KB_MARKUP is None:
        kb = InlineKeyboardBuilder()
        kb.button(text="⚡ Быстрый скрин", callback_data="quick_signal")
        kb.button(text="🕘 История", callback_data="history_show")
        kb.button(text="📊 Сводка", callback_data="series_show")
        kb.button(text="👤 Профиль", callback_data="profile_show")
        kb.button(text="📊 Bybit", callback_data="exchange_bybit")
        kb.button(text="📊 BingX", callback_data="exchange_bingx")
        kb.button(text="🎨 Кастом Bybit", callback_data="custom_bybit")
        kb.button(text="💵 Кастом Bybit $", callback_data="custom_bybit_usdt")
        kb.button(text="🎨 Кастом BingX", callback_data="custom_bingx")
        kb.button(text="🏁 Марафон", callback_data="marathon:menu")
        kb.adjust(1)
        _MAIN_KB_MARKUP = kb.as_markup()
    return _MAIN_KB_MARKUP

# =====================================================
# START / TEST
# =====================================================
@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id
    has_access, reason = check_access(user_id)

    if has_access:
        left = days_left(user_id)
        label = "🔓 Пробный" if reason == "trial" else "✅ Полный доступ"
        kb = InlineKeyboardBuilder()
        kb.button(text="⚡ Быстрый скрин", callback_data="quick_signal")
        kb.button(text="🕘 История", callback_data="history_show")
        kb.button(text="📊 Сводка", callback_data="series_show")
        kb.button(text="👤 Профиль", callback_data="profile_show")
        kb.button(text="📊 Bybit", callback_data="exchange_bybit")
        kb.button(text="📊 BingX", callback_data="exchange_bingx")
        kb.button(text="🎨 Кастом Bybit", callback_data="custom_bybit")
        kb.button(text="💵 Кастом Bybit $", callback_data="custom_bybit_usdt")
        kb.button(text="🎨 Кастом BingX", callback_data="custom_bingx")
        kb.button(text="🏁 Марафон", callback_data="marathon:menu")
        kb.adjust(1)
        # 1) Persistent reply-keyboard sticks at the bottom of the chat (sent
        #    once, here). 2) Inline keyboard with all modes is included with
        #    THIS message only (since reply_markup is the inline kb here).
        # Telegram allows ONE reply_markup per message, so we send the inline
        # menu first and the persistent keyboard separately as a tiny prompt.
        await message.answer("Меню всегда снизу 👇", reply_markup=MAIN_MENU_RKB)
        await message.answer(
            f"{label} • осталось дней: {left}\n\nВыбери режим:",
            reply_markup=kb.as_markup()
        )
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="🆓 Пробная версия (2 дня)", callback_data="trial_access")
        kb.button(text="💎 Получить полный доступ", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")
        kb.adjust(1)
        text = (
            "👋 Привет! Это бот для генерации скриншотов сделок.\n\n"
            "🔒 Доступ платный.\n\n"
            "• Нажми «Пробная версия» — получишь 2 дня бесплатно\n"
            "• Для полного доступа — напиши администратору"
        )
        if reason == "expired":
            text = "⏳ Твой доступ истёк.\n\nДля продления напиши администратору 👇"
        await message.answer(text, reply_markup=kb.as_markup())


@dp.message(Command("grant"))
async def cmd_grant(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /grant <user_id> [дней]\nПример: /grant 123456789 30")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 else 30
    except ValueError:
        await message.answer("Неверный формат. /grant <user_id> [дней]")
        return
    grant_access(uid, days)
    left = days_left(uid)
    await message.answer(f"✅ Доступ выдан пользователю {uid} на {days} дней.\nОсталось: {left} дн.")
    try:
        await bot.send_message(uid, f"✅ Вам выдан полный доступ на {days} дней!\nОсталось: {left} дн.")
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")

@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /revoke <user_id>")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return
    revoke_access(uid)
    await message.answer(f"🚫 Доступ пользователя {uid} отозван.")

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    import json, os
    from access import ACCESS_FILE
    if not os.path.exists(ACCESS_FILE):
        await message.answer("Нет пользователей.")
        return
    with open(ACCESS_FILE) as f:
        data = json.load(f)
    if not data:
        await message.answer("Нет пользователей.")
        return
    lines = []
    for uid, info in data.items():
        left = days_left(int(uid))
        lines.append(f"• {uid} | {info['type']} | осталось {left} дн.")
    await message.answer("👥 Пользователи:\n" + "\n".join(lines))


# =====================================================
# SIGNAL FLOW (paste signal text → bot generates card)
# =====================================================
def _signal_summary(parsed: dict) -> str:
    sym = parsed.get("symbol") or "—"
    side = {"long": "Лонг", "short": "Шорт"}.get(parsed.get("side"), "—")
    entry = parsed.get("entry")
    sl = parsed.get("sl")
    tps = parsed.get("tps", [])
    lines = [
        "📊 Распознал сигнал:",
        f"  Символ:  {sym}",
        f"  Сторона: {side}",
        f"  Вход:    {entry if entry is not None else '—'}",
        f"  SL:      {sl if sl is not None else '—'}",
    ]
    for i, tp in enumerate(tps[:5], 1):
        lines.append(f"  TP{i}:     {tp}")
    return "\n".join(lines)


def _signal_exit_kb(parsed: dict) -> InlineKeyboardMarkup:
    """Buttons: TP1/TP2/TP3 (those that exist), SL, live-from-exchange, custom."""
    rows = []
    tp_row = []
    for i, tp in enumerate(parsed.get("tps", [])[:3], 1):
        tp_row.append(InlineKeyboardButton(text=f"TP{i} ({tp})", callback_data=f"sig_exit:tp{i-1}"))
    if tp_row:
        rows.append(tp_row)
    bottom = []
    if parsed.get("sl") is not None:
        bottom.append(InlineKeyboardButton(text=f"SL ({parsed['sl']})", callback_data="sig_exit:sl"))
    bottom.append(InlineKeyboardButton(text="📡 Сейчас (с биржи)", callback_data="sig_exit:live"))
    bottom.append(InlineKeyboardButton(text="↪︎ Своя цена", callback_data="sig_exit:custom"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


_SIG_EXCHANGE_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🟡 Bybit", callback_data="sig_ex:bybit"),
    InlineKeyboardButton(text="🟢 BingX", callback_data="sig_ex:bingx"),
]])
_SIG_STATUS_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Реализованная (закрытая)", callback_data="sig_st:closed"),
    InlineKeyboardButton(text="⏳ Нереализованная (открытая)", callback_data="sig_st:open"),
]])
_SIG_TEMPLATE_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🐳 Whale", callback_data="sig_tpl:whale"),
    InlineKeyboardButton(text="🟢 Dot",   callback_data="sig_tpl:dot"),
    InlineKeyboardButton(text="⚽ Football", callback_data="sig_tpl:football"),
]])


@dp.callback_query(F.data == "quick_signal")
async def quick_signal_prompt(call: CallbackQuery, state: FSMContext):
    """Show the user the expected signal format and wait for the next message
    (auto-detected by signal_auto_detect handler)."""
    has_access, _ = check_access(call.from_user.id)
    if not has_access:
        await call.answer("🔒 Доступ закрыт. Нажми /start и оформи доступ.", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.answer(
        "⚡ <b>Быстрый скрин</b>\n\n"
        "Пришли следующим сообщением сигнал в любом формате — бот сам распознает.\n"
        "Минимум: символ + сторона + цены.\n\n"
        "<b>Пример:</b>\n"
        "<code>BTCUSDT\n"
        "Шорт\n"
        "Вход: 78373\n"
        "Стоп: 79546\n"
        "TP1: 76613\n"
        "TP2: 74893\n"
        "TP3: 73165</code>\n\n"
        "Или с эмодзи / в любом другом виде — бот разберёт.",
        parse_mode="HTML",
    )


@dp.message(Command("signal"))
async def cmd_signal(msg: Message, state: FSMContext):
    """Manual /signal — text after the command, or fall back to a help blurb."""
    text = (msg.text or "").split(None, 1)
    if len(text) < 2:
        await msg.answer(
            "📥 Пришли мне текст сигнала следующим сообщением — пример:\n\n"
            "<code>📉 $BTC -USDT-SWAP ШОРТ\n"
            "🎯 Entry: 78,373\n"
            "🛑 SL: 79,546\n"
            "✅ TP1: 76,613\n"
            "✅ TP2: 74,893</code>",
            parse_mode="HTML",
        )
        await state.clear()
        return
    await _handle_signal_text(msg, state, text[1])


async def _handle_signal_text(msg: Message, state: FSMContext, text: str):
    parsed = parse_signal(text)
    if not parsed["symbol"] or not parsed["side"]:
        await msg.answer("Не смог распознать. Нужны хотя бы символ ($BTC) и сторона (LONG/SHORT).")
        return
    if parsed["entry"] is None and not parsed["tps"]:
        await msg.answer("Не нашёл цены — нужен Entry или TP.")
        return
    has_access, _ = check_access(msg.from_user.id)
    if not has_access:
        can_use, _ = check_daily_limit(msg.from_user.id)
        if not can_use:
            await msg.answer(
                "🔒 Лимит 3 бесплатных скрина в день исчерпан.\n"
                f"Напиши {ADMIN_USERNAME} для полного доступа.",
            )
            return
    await state.clear()
    # Pre-fill from saved profile so the flow can skip exchange/username/referral
    # for return users. _profile_defaults is consumed by _signal_advance and
    # stripped before persisting to history.
    profile = get_profile(msg.from_user.id)
    parsed["_profile_defaults"] = {
        "exchange": profile.get("exchange"),
        "username": profile.get("username"),
        "referral": profile.get("referral"),
    }
    if parsed["entry"] is None and parsed["tps"]:
        parsed["entry"] = parsed["tps"][0]
        await msg.answer(_signal_summary(parsed) + f"\n  (entry не найден — взял TP1 = {parsed['tps'][0]})\n\nКакую цену выхода взять?",
                         reply_markup=_signal_exit_kb(parsed))
    else:
        await msg.answer(_signal_summary(parsed) + "\n\nКакую цену выхода взять?",
                         reply_markup=_signal_exit_kb(parsed))
    await state.update_data(_sig=parsed, _user_id=msg.from_user.id)
    await state.set_state(SignalForm.exit_choice)


async def _signal_advance(target, state: FSMContext):
    """Look at current `_sig` draft and route to the next missing field, or to
    preview when everything is filled. Honours `_profile_defaults` so saved
    user defaults (exchange / username / referral) silently skip those prompts."""
    data = await state.get_data()
    sig = dict(data.get("_sig", {}))
    defaults = sig.get("_profile_defaults", {}) or {}

    # 1) exchange
    if "exchange" not in sig:
        if defaults.get("exchange"):
            sig["exchange"] = defaults["exchange"]
            await state.update_data(_sig=sig)
        else:
            await target.answer("📊 Биржа?", reply_markup=_SIG_EXCHANGE_KB)
            await state.set_state(SignalForm.exchange)
            return
    # 1b) live exit fetch — only possible once exchange is known.
    # Marker is set by signal_pick_exit when user taps «📡 Сейчас (с биржи)».
    if sig.pop("_sig_use_live_exit", False):
        live = await async_get_mark_price(sig["exchange"], sig["symbol"])
        if live is None:
            await target.answer(f"Не смог достать цену {sig['symbol']} с {sig['exchange']}. "
                                "Введи сам или перевыбери биржу.")
            sig["_sig_use_live_exit"] = True   # restore so retry works
            await state.update_data(_sig=sig)
            return
        sig["exit"] = live
        sig["_sig_exit_from_live"] = True   # surfaced in preview caption
        await state.update_data(_sig=sig)
        await target.answer(f"📡 Цена {sig['symbol']} с {sig['exchange']}: <b>{live}</b>",
                            parse_mode="HTML")
    # 2) status
    if "status" not in sig:
        await target.answer("📈 Состояние сделки?", reply_markup=_SIG_STATUS_KB)
        await state.set_state(SignalForm.status)
        return
    # 3) BingX template
    if sig.get("exchange") == "bingx" and "template" not in sig:
        await target.answer("🎨 Шаблон BingX?", reply_markup=_SIG_TEMPLATE_KB)
        await state.set_state(SignalForm.template)
        return
    # 4) leverage (always required, never auto-filled)
    if "leverage" not in sig:
        await target.answer("⚖️ Введите плечо (например 50):")
        await state.set_state(SignalForm.leverage)
        return
    # 5) username — `is not None` so a saved empty-string ("always blank") still skips
    if "username" not in sig:
        if defaults.get("username") is not None:
            sig["username"] = defaults["username"]
            await state.update_data(_sig=sig)
        else:
            await target.answer("👤 Имя пользователя? (или пропусти)", reply_markup=skip_kb)
            await state.set_state(SignalForm.username)
            return
    # 6) referral
    if "referral" not in sig:
        if defaults.get("referral") is not None:
            sig["referral"] = defaults["referral"]
            await state.update_data(_sig=sig)
        else:
            await target.answer("🎁 Реферальный код? (или пропусти)", reply_markup=skip_kb)
            await state.set_state(SignalForm.referral)
            return
    # 7) datetime (BingX only)
    if sig.get("exchange") == "bingx" and "datetime_str" not in sig:
        await target.answer("📅 Дата/время? (или пропусти, например 02/14 19:00)", reply_markup=skip_kb)
        await state.set_state(SignalForm.datetime_str)
        return
    # 8) all fields present → preview
    await _render_preview(target, state)


@dp.callback_query(SignalForm.exit_choice, F.data.startswith("sig_exit:"))
async def signal_pick_exit(call: CallbackQuery, state: FSMContext):
    choice = call.data.split(":", 1)[1]
    data = await state.get_data()
    parsed = data.get("_sig", {})
    if choice.startswith("tp"):
        idx = int(choice[2:])
        exit_price = parsed["tps"][idx] if idx < len(parsed.get("tps", [])) else None
    elif choice == "sl":
        exit_price = parsed.get("sl")
    elif choice == "live":
        # Don't set exit yet — _signal_advance will fetch from the exchange API
        # once the user has picked one (or it's pre-filled from profile).
        await state.update_data(_sig={**parsed, "_sig_use_live_exit": True})
        await call.answer("📡 Подтянем рыночную цену после выбора биржи")
        try: await call.message.delete()
        except Exception: pass
        await _signal_advance(call.message, state)
        return
    elif choice == "custom":
        await call.answer()
        await call.message.answer("Введите свою цену выхода:")
        await state.update_data(_sig_awaiting_custom_exit=True)
        return
    else:
        await call.answer()
        await call.message.answer("Введите свою цену выхода:")
        await state.update_data(_sig_awaiting_custom_exit=True)
        return
    if exit_price is None:
        await call.answer("Не удалось получить цену", show_alert=True)
        return
    await state.update_data(_sig={**parsed, "exit": exit_price})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.message(SignalForm.exit_choice)
async def signal_custom_exit(msg: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("_sig_awaiting_custom_exit"):
        return
    val = await parse_float(msg)
    if val is None: return
    parsed = data.get("_sig", {})
    await state.update_data(_sig={**parsed, "exit": val}, _sig_awaiting_custom_exit=False)
    await safe_delete_message(msg)
    await _signal_advance(msg, state)


@dp.callback_query(SignalForm.exchange, F.data.startswith("sig_ex:"))
async def signal_pick_exchange(call: CallbackQuery, state: FSMContext):
    exchange = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "exchange": exchange})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.callback_query(SignalForm.status, F.data.startswith("sig_st:"))
async def signal_pick_status(call: CallbackQuery, state: FSMContext):
    status = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "status": status})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.callback_query(SignalForm.template, F.data.startswith("sig_tpl:"))
async def signal_pick_template(call: CallbackQuery, state: FSMContext):
    tpl = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "template": tpl})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.message(SignalForm.leverage)
async def signal_leverage(msg: Message, state: FSMContext):
    try:
        lev = float((msg.text or "").strip().lower().replace("x", ""))
        if lev < 1 or lev > 200: raise ValueError
    except ValueError:
        await msg.answer("Введите число от 1 до 200")
        return
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "leverage": f"{lev:g}x"})
    await safe_delete_message(msg)
    await _signal_advance(msg, state)


@dp.callback_query(SignalForm.username, F.data == "skip_field")
async def signal_skip_username(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "username": ""})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.message(SignalForm.username)
async def signal_username(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()[:50]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "username": text})
    await safe_delete_message(msg)
    await _signal_advance(msg, state)


@dp.callback_query(SignalForm.referral, F.data == "skip_field")
async def signal_skip_referral(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "referral": ""})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.message(SignalForm.referral)
async def signal_referral(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()[:30]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "referral": text})
    await safe_delete_message(msg)
    await _signal_advance(msg, state)


@dp.callback_query(SignalForm.datetime_str, F.data == "skip_field")
async def signal_skip_datetime(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "datetime_str": ""})
    await call.answer()
    try: await call.message.delete()
    except Exception: pass
    await _signal_advance(call.message, state)


@dp.message(SignalForm.datetime_str)
async def signal_datetime(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()[:20]
    data = await state.get_data()
    await state.update_data(_sig={**data["_sig"], "datetime_str": text})
    await safe_delete_message(msg)
    await _signal_advance(msg, state)


# =====================================================
# FEES — match each exchange's default Taker rate so the displayed ROI mirrors
# the exchange's "Closed PnL" / «Закрытый PnL» view (which is already net of
# entry+exit trading fees). VIP tier discounts aren't modelled — these are the
# spot-VIP-0 taker rates published by each exchange as of late-2025.
# =====================================================
BYBIT_TAKER_RATE = 0.00055   # 0.055% per leg
BINGX_TAKER_RATE = 0.00050   # 0.050% per leg


def compute_pnl_breakdown(entry: float, exit_price: float, leverage: float,
                          side: str, exchange: str) -> tuple[float, float, float]:
    """Return (gross_pct, fee_pct, net_pct).

    Math (USDT-margined linear perp, isolated):
      gross_pct = ((exit-entry)/entry) × leverage × 100        (LONG; mirrored for SHORT)
      fee_pct   = ((entry+exit)/entry) × taker × leverage × 100
                ≈ 2 × taker × leverage × 100 when entry≈exit; the exact form
                handles big swings correctly because the closing notional uses
                the exit price.
      net_pct   = gross_pct - fee_pct
    """
    if side == "long":
        gross_pct = ((exit_price - entry) / entry) * leverage * 100
    else:
        gross_pct = ((entry - exit_price) / entry) * leverage * 100
    rate = BYBIT_TAKER_RATE if exchange == "bybit" else BINGX_TAKER_RATE
    fee_pct = ((entry + exit_price) / entry) * rate * leverage * 100
    return gross_pct, fee_pct, gross_pct - fee_pct


def _build_image_data(sig: dict) -> tuple[dict, callable]:
    """Convert SignalForm draft into image_data + the right renderer fn.
    The displayed `pnl` is NET-of-fees so it matches the exchange's «Закрытый PnL»
    view. The pre-fee gross and the deducted fee are also returned via
    `pnl_gross` / `pnl_fee_pct` for the caption breakdown."""
    entry = sig["entry"]
    exit_price = sig["exit"]
    side = sig["side"]
    lev_raw = sig.get("leverage", "1x").lower().replace("x", "")
    try: leverage = float(lev_raw)
    except ValueError: leverage = 1.0
    gross_pct, fee_pct, net_pct = compute_pnl_breakdown(
        entry, exit_price, leverage, side, sig["exchange"])
    image_data = {
        "username":     sig.get("username", ""),
        "symbol":       sig["symbol"],
        "pnl":          round(net_pct, 2),
        "pnl_gross":    round(gross_pct, 2),
        "pnl_fee_pct":  round(fee_pct, 2),
        "entry":        entry,
        "exit":         exit_price,
        "side":         side,
        "referral":     sig.get("referral", ""),
        "leverage":     sig["leverage"],
        "status":       sig.get("status", "closed"),
    }
    if sig["exchange"] == "bingx":
        image_data["template"]     = sig.get("template", "football")
        image_data["datetime_str"] = sig.get("datetime_str", "")
        return image_data, generate_custom_bingx_image
    return image_data, generate_custom_bybit_image


def _preview_kb(sig: dict) -> InlineKeyboardMarkup:
    """Inline keyboard for the draft-preview state."""
    is_bingx = sig.get("exchange") == "bingx"
    rows = [
        [InlineKeyboardButton(text="✏️ Плечо",       callback_data="sig_edit:leverage"),
         InlineKeyboardButton(text="✏️ Состояние",   callback_data="sig_edit:status")],
        [InlineKeyboardButton(text="✏️ Биржа",       callback_data="sig_edit:exchange"),
         InlineKeyboardButton(text="✏️ Сторона",     callback_data="sig_edit:side")],
        [InlineKeyboardButton(text="✏️ Цена входа",  callback_data="sig_edit:entry"),
         InlineKeyboardButton(text="✏️ Цена выхода", callback_data="sig_edit:exit")],
        [InlineKeyboardButton(text="✏️ Имя",         callback_data="sig_edit:username"),
         InlineKeyboardButton(text="✏️ Реф.код",     callback_data="sig_edit:referral")],
    ]
    if is_bingx:
        rows.append([
            InlineKeyboardButton(text="✏️ Шаблон",   callback_data="sig_edit:template"),
            InlineKeyboardButton(text="✏️ Дата",     callback_data="sig_edit:datetime_str"),
        ])
    rows.append([
        InlineKeyboardButton(text="✅ Отправить", callback_data="sig_send"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="sig_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_preview(msg, state: FSMContext):
    """Render current draft, show as photo with edit/send keyboard."""
    data = await state.get_data()
    sig = data["_sig"]
    uid = data.get("_user_id")
    image_data, gen_func = _build_image_data(sig)
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_THREAD_POOL, gen_func, image_data)
        if uid:
            path = await loop.run_in_executor(_THREAD_POOL, _apply_user_logo, path, uid)
    except Exception as e:
        logger.error(f"Preview render error: {e}")
        await msg.answer("Ошибка генерации картинки.")
        return
    rate_pct = (BYBIT_TAKER_RATE if sig.get('exchange') == 'bybit'
                else BINGX_TAKER_RATE) * 100
    live_tag = " 📡" if sig.get("_sig_exit_from_live") else ""
    summary = (
        f"📋 <b>Черновик</b>\n"
        f"  {sig['symbol']} • {'Лонг' if sig['side']=='long' else 'Шорт'} "
        f"{sig.get('leverage','—')} • <b>{image_data['pnl']:+.2f}%</b>\n"
        f"  Грязный: {image_data['pnl_gross']:+.2f}% • "
        f"Комиссия: −{image_data['pnl_fee_pct']:.2f}% (taker {rate_pct:.3f}% × 2)\n"
        f"  Биржа: {sig.get('exchange','—')} • Состояние: "
        f"{'Закрыта' if sig.get('status')=='closed' else 'Открыта'}\n"
        f"  Вход: {sig['entry']} → Выход: {sig['exit']}{live_tag}\n"
        f"\nТапни поле для правки или ✅ отправить."
    )
    sent = await msg.answer_photo(FSInputFile(path), caption=summary,
                                  parse_mode="HTML", reply_markup=_preview_kb(sig))
    await state.update_data(_sig_preview_msg_id=sent.message_id)
    await state.set_state(SignalForm.preview)


async def _signal_finish(msg, state: FSMContext):
    """Was: send card immediately. Now: show preview, send only on ✅."""
    await _render_preview(msg, state)


# ---- Preview state callbacks ----

@dp.callback_query(SignalForm.preview, F.data == "sig_send")
async def signal_preview_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sig = data["_sig"]
    image_data, gen_func = _build_image_data(sig)
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_THREAD_POOL, gen_func, image_data)
        path = await loop.run_in_executor(_THREAD_POOL, _apply_user_logo, path, call.from_user.id)
        await call.message.answer_photo(FSInputFile(path), reply_markup=restart_kb)
        has_access, _ = check_access(call.from_user.id)
        if not has_access:
            increment_usage(call.from_user.id)
        # Auto-save profile (exchange always, username/referral only if non-empty
        # — empty in this signal means "skipped this time", not "clear my profile").
        try:
            update_profile(call.from_user.id,
                           exchange=sig.get("exchange"),
                           username=(sig.get("username") or None),
                           referral=(sig.get("referral") or None))
            add_history(call.from_user.id, sig)
        except Exception as e:
            logger.warning(f"profile/history save failed: {e}")
        # Autopost to user's saved channel, if any.
        profile = get_profile(call.from_user.id)
        chan = profile.get("channel")
        if chan:
            side_ru = "Лонг" if sig["side"] == "long" else "Шорт"
            chan_caption = (f"📊 {sig['symbol']} • {side_ru} {sig.get('leverage','')} • "
                            f"<b>{image_data['pnl']:+.2f}%</b>")
            try:
                await bot.send_photo(chan, FSInputFile(path), caption=chan_caption,
                                     parse_mode="HTML")
                await call.message.answer(f"📢 Также опубликовано в {chan}")
            except Exception as e:
                logger.warning(f"Channel autopost failed for {call.from_user.id} → {chan}: {e}")
                await call.message.answer(f"⚠️ Не получилось запостить в канал {chan}: {e}")
    except Exception as e:
        logger.error(f"Final send render error: {e}")
        await call.message.answer("Ошибка генерации картинки.", reply_markup=restart_kb)
    await call.answer("✅ Отправлено")
    try: await call.message.delete()
    except Exception: pass
    await state.clear()


@dp.callback_query(SignalForm.preview, F.data == "sig_cancel")
async def signal_preview_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer("❌ Отменено")
    try: await call.message.delete()
    except Exception: pass
    await state.clear()
    await call.message.answer("Черновик удалён. /start чтобы начать заново.")


# ---- Edit dispatchers (each field) ----

_EDIT_PROMPTS = {
    "leverage":     "Введите новое плечо (1..200):",
    "entry":        "Введите новую цену входа:",
    "exit":         "Введите новую цену выхода:",
    "username":     "Введите имя пользователя (или /skip):",
    "referral":     "Введите реферальный код (или /skip):",
    "datetime_str": "Введите дату/время (или /skip):",
}
_EDIT_BUTTON_FIELDS = {"exchange", "status", "side", "template"}


@dp.callback_query(SignalForm.preview, F.data.startswith("sig_edit:"))
async def signal_preview_edit(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":", 1)[1]
    await call.answer()
    await state.update_data(_sig_editing_field=field)
    if field in _EDIT_BUTTON_FIELDS:
        kb = _edit_choice_kb(field)
        await call.message.answer(f"Выбери новое значение для «{_field_title(field)}»:", reply_markup=kb)
        # stay in preview state — choice handlers will save + re-render
        return
    if field in _EDIT_PROMPTS:
        await call.message.answer(_EDIT_PROMPTS[field])
        await state.set_state(SignalForm.edit_value)
        return
    await call.message.answer(f"Не знаю как редактировать поле {field!r}.")


def _field_title(field: str) -> str:
    return {
        "leverage": "плечо", "entry": "цена входа", "exit": "цена выхода",
        "username": "имя", "referral": "реф.код", "datetime_str": "дата",
        "exchange": "биржа", "status": "состояние", "side": "сторона",
        "template": "шаблон", "channel": "канал",
    }.get(field, field)


def _edit_choice_kb(field: str) -> InlineKeyboardMarkup:
    if field == "exchange":
        rows = [[InlineKeyboardButton(text="🟡 Bybit", callback_data="sig_setval:exchange:bybit"),
                 InlineKeyboardButton(text="🟢 BingX", callback_data="sig_setval:exchange:bingx")]]
    elif field == "status":
        rows = [[InlineKeyboardButton(text="✅ Закрытая", callback_data="sig_setval:status:closed"),
                 InlineKeyboardButton(text="⏳ Открытая", callback_data="sig_setval:status:open")]]
    elif field == "side":
        rows = [[InlineKeyboardButton(text="🟢 Лонг", callback_data="sig_setval:side:long"),
                 InlineKeyboardButton(text="🔴 Шорт", callback_data="sig_setval:side:short")]]
    elif field == "template":
        rows = [[InlineKeyboardButton(text="🐳 Whale", callback_data="sig_setval:template:whale"),
                 InlineKeyboardButton(text="🟢 Dot",   callback_data="sig_setval:template:dot"),
                 InlineKeyboardButton(text="⚽ Football", callback_data="sig_setval:template:football")]]
    else:
        rows = []
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("sig_setval:"))
async def signal_setval(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer("bad payload", show_alert=True); return
    _, field, value = parts
    data = await state.get_data()
    sig = dict(data.get("_sig", {}))
    sig[field] = value
    await state.update_data(_sig=sig, _sig_editing_field=None)
    await call.answer(f"✓ {_field_title(field)} = {value}")
    try: await call.message.delete()
    except Exception: pass
    # If user switched exchange to/from bingx, may need to add/remove template
    if field == "exchange" and value == "bingx" and not sig.get("template"):
        sig["template"] = "football"
        await state.update_data(_sig=sig)
    await _render_preview(call.message, state)


@dp.message(SignalForm.edit_value)
async def signal_edit_value(msg: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("_sig_editing_field")
    if not field:
        await msg.answer("Не знаю что редактирую — нажми /start заново.")
        await state.clear(); return
    txt = (msg.text or "").strip()
    if txt == "/skip":
        new_val = ""
    elif field == "leverage":
        try:
            v = float(txt.lower().replace("x", ""))
            if v < 1 or v > 200: raise ValueError
            new_val = f"{v:g}x"
        except ValueError:
            await msg.answer("Введите число от 1 до 200.")
            return
    elif field in ("entry", "exit"):
        v = await parse_float(msg)
        if v is None: return
        new_val = v
    else:  # username, referral, datetime_str
        new_val = txt[:50]
    sig = dict(data.get("_sig", {}))
    sig[field] = new_val
    await state.update_data(_sig=sig, _sig_editing_field=None)
    await safe_delete_message(msg)
    await _render_preview(msg, state)


@dp.message(Command("test_all"))
async def test_all(message: Message):
    text = (
        "Из сигнала: пришли любой текст с символом + LONG/SHORT + ценами,\n"
        "или /signal <текст> — бот сам распознает и сгенерит карточку.\n\n"
        "Эталоны (точная копия assets/*/JPG):\n"
        "/test_ref_all  ← разом все 11\n"
        "  /test_ref_bingx_long_whale\n"
        "  /test_ref_bingx_long_dot\n"
        "  /test_ref_bingx_long_football\n"
        "  /test_ref_bingx_short_whale\n"
        "  /test_ref_bingx_short_dot\n"
        "  /test_ref_bingx_short_football\n"
        "  /test_ref_bybit_plus_long\n"
        "  /test_ref_bybit_plus_short\n"
        "  /test_ref_bybit_minus_long\n"
        "  /test_ref_bingx_normal_long\n"
        "  /test_ref_bingx_normal_short\n"
        "\n"
        "Варианты с произвольными значениями:\n"
        "/test_bybit_long  /test_bybit_short\n"
        "/test_bingx_long  /test_bingx_short\n"
        "/test_custom_bybit_long  /test_custom_bybit_short\n"
        "/test_custom_bingx_long  /test_custom_bingx_short\n"
        "/test_custom_bingx_short_football\n"
        "/test_custom_bingx_short_curve\n"
        "/test_custom_bingx_short_doge\n"
        "/test_custom_bybit_usdt_long\n"
        "/test_custom_bybit_usdt_short"
    )
    await message.answer(text)


@dp.message(Command("test_bybit_long"))
async def test_bybit_long(message: Message):
    await _run_spot_test(message, exchange="bybit", side="long")


@dp.message(Command("test_bybit_short"))
async def test_bybit_short(message: Message):
    await _run_spot_test(message, exchange="bybit", side="short")


@dp.message(Command("test_bingx_long"))
async def test_bingx_long(message: Message):
    await _run_spot_test(message, exchange="bingx", side="long")


@dp.message(Command("test_bingx_short"))
async def test_bingx_short(message: Message):
    await _run_spot_test(message, exchange="bingx", side="short")

async def _run_spot_test(message: Message, exchange: str, side: str):
    amount = 100
    entry = 42000
    mark = 43250 if side == "long" else 41000
    leverage = 20

    qty = calculate_qty(exchange, amount, entry, leverage)
    cost = calculate_cost(exchange, amount, leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(entry, mark, qty, side, leverage, exchange)
    pnl = percent
    liquidation = calculate_liquidation(entry, leverage, side)

    data = {
        "exchange": exchange,
        "symbol": "PYTHUSDT",
        "side": side,
        "entry": entry,
        "mark": mark,
        "amount": amount,
        "deposit": 50,
        "leverage": leverage,
        "qty": qty,
        "liquidation": liquidation,
        "cost": cost,
    }

    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(
            _THREAD_POOL,
            generate_trade_image,
            data,
            percent,
            pnl,
            pnl_usdt,
        )
        await message.answer_photo(FSInputFile(path))
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await message.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)

async def _run_custom_test(message: Message, exchange: str, side: str, template: str | None = None, status: str | None = None):
    entry = 0.1068
    exit_price = 0.1092 if side == "long" else 0.1040
    leverage_str = "50x"
    leverage = float(leverage_str.replace("x", ""))

    if side == "long":
        pnl_percent = ((exit_price - entry) / entry * 100) * leverage
    else:
        pnl_percent = ((entry - exit_price) / entry * 100) * leverage

    image_data = {
        "username": "ТЕСТ ПОЛЬЗОВАТЕЛЬ",
        "symbol": "PYTHUSDT",
        "pnl": round(pnl_percent, 2),
        "entry": entry,
        "exit": exit_price,
        "leverage": leverage_str,
        "side": side,
        "referral": "D1BFA4",
        "datetime_str": "02/14 19:00",
    }
    if template:
        image_data["template"] = template
    if status:
        image_data["status"] = status

    loop = asyncio.get_event_loop()
    try:
        if exchange == "bingx":
            path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bingx_image, image_data)
        else:
            path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_image, image_data)
        await message.answer_photo(FSInputFile(path))
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await message.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)


@dp.message(Command("test_custom_bybit_long"))
async def test_custom_bybit_long(message: Message):
    await _run_custom_test(message, exchange="bybit", side="long")


@dp.message(Command("test_custom_bybit_short"))
async def test_custom_bybit_short(message: Message):
    await _run_custom_test(message, exchange="bybit", side="short")


@dp.message(Command("test_custom_bingx_long"))
async def test_custom_bingx_long(message: Message):
    await _run_custom_test(message, exchange="bingx", side="long")


@dp.message(Command("test_custom_bingx_short"))
async def test_custom_bingx_short(message: Message):
    await _run_custom_test(message, exchange="bingx", side="short")


@dp.message(Command("test_custom_bingx_short_curve"))
async def test_custom_bingx_short_curve(message: Message):
    await _run_custom_test(message, exchange="bingx", side="short", template="curve")


@dp.message(Command("test_custom_bingx_short_doge"))
async def test_custom_bingx_short_doge(message: Message):
    await _run_custom_test(message, exchange="bingx", side="short", template="doge")


@dp.message(Command("test_custom_bingx_short_football"))
async def test_custom_bingx_short_football(message: Message):
    await _run_custom_test(message, exchange="bingx", side="short", template="football")


# =====================================================
# REFERENCE-MATCHING tests (pixel reproduction of the reference JPGs)
# =====================================================

# (data, render_fn, percent, pnl_usdt_or_None, caption)
_REF_TEST_CASES = {
    # BingX share cards — match BINGX_Custom_<variant>_<plus|minus>.JPG
    "bingx_long_whale": dict(
        render="bingx_share",
        data={"template":"whale","side":"long","status":"closed","symbol":"SKYAIUSDT","pnl":224.11,
              "entry":0.34856,"exit":0.38763,"leverage":"20x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    "bingx_long_dot": dict(
        render="bingx_share",
        data={"template":"dot","side":"long","status":"closed","symbol":"SKYAIUSDT","pnl":224.11,
              "entry":0.34856,"exit":0.38763,"leverage":"20x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    "bingx_long_football": dict(
        render="bingx_share",
        data={"template":"football","side":"long","status":"closed","symbol":"SKYAIUSDT","pnl":224.11,
              "entry":0.34856,"exit":0.38763,"leverage":"20x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    "bingx_short_whale": dict(
        render="bingx_share",
        data={"template":"whale","side":"short","status":"open","symbol":"ETHUSDT","pnl":-4.07,
              "entry":2284.36,"exit":2302.97,"leverage":"5x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    "bingx_short_dot": dict(
        render="bingx_share",
        data={"template":"dot","side":"short","status":"open","symbol":"ETHUSDT","pnl":-4.07,
              "entry":2284.36,"exit":2302.97,"leverage":"5x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    "bingx_short_football": dict(
        render="bingx_share",
        data={"template":"football","side":"short","status":"open","symbol":"ETHUSDT","pnl":-4.07,
              "entry":2284.36,"exit":2302.97,"leverage":"5x","username":"CHM_LAB",
              "referral":"D1BFA4","datetime_str":"05-02"},
    ),
    # Bybit share cards — match Bybit_custom_<plus|minus>PNL_<LONG|SHORT>.JPG
    "bybit_plus_long": dict(
        render="bybit_share",
        # Baked reference uses "Текущая цена" → status="open" matches the asset
        data={"username":"chmst","symbol":"WIFUSDT","pnl":12.72,"entry":0.9058,"exit":0.9074,
              "leverage":"75x","side":"long","referral":"PGKDGV","status":"open"},
    ),
    "bybit_plus_short": dict(
        render="bybit_share",
        data={"username":"chmst","symbol":"SUIUSDT","pnl":8.97,"entry":3.4667,"exit":3.4603,
              "entry_str":"3.46670","exit_str":"3.46030",
              "leverage":"50x","side":"short","referral":"POKOIV","status":"open"},
    ),
    "bybit_minus_long": dict(
        render="bybit_share",
        data={"username":"chmst","symbol":"WLDUSDT","pnl":-100.79,"entry":0.9869,"exit":0.9665,
              "entry_str":"0.9869","exit_str":"0.9665",
              "leverage":"50x","side":"long","referral":"PGKDGV","status":"open"},
    ),
    # BingX normal UI — match NORMAL_BINGX_<LONG|SHORT>.jpg
    "bingx_normal_long": dict(
        render="bingx_normal",
        data={"symbol":"GIGGLEUSDT","side":"long","leverage":"5","entry":31.0,"mark":29.87,
              "amount":1039.3437,"liquidation":0,"realized_pnl":-5.0092,
              "position_usdt":5006.8,"risk_pct":2.04,"tp_value":"33.66","sl_value":"--"},
        percent=-18.06, pnl_usdt=-187.7344,
    ),
    "bingx_normal_short": dict(
        render="bingx_normal",
        data={"symbol":"ETHUSDT","side":"short","leverage":"5","entry":2284.36,"mark":2302.12,
              "amount":1713.27,"liquidation":3952.18,"realized_pnl":-2.5429,
              "position_usdt":8632.94,"risk_pct":2.04,"tp_value":"2204.00","sl_value":"--"},
        percent=-3.88, pnl_usdt=-66.5999,
    ),
}

async def _run_ref_test(message: Message, key: str):
    case = _REF_TEST_CASES.get(key)
    if case is None:
        await message.answer(f"Неизвестный референс: {key}")
        return
    loop = asyncio.get_event_loop()
    try:
        if case["render"] == "bingx_share":
            path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bingx_image, case["data"])
        elif case["render"] == "bybit_share":
            path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_image, case["data"])
        elif case["render"] == "bingx_normal":
            path = await loop.run_in_executor(
                _THREAD_POOL, generate_bingx_normal_card,
                case["data"], case["percent"], case["pnl_usdt"],
            )
        else:
            await message.answer(f"Неизвестный рендер: {case['render']}")
            return
        await message.answer_photo(FSInputFile(path), caption=f"ref: {key}")
    except Exception as e:
        logger.error(f"Ref test {key} error: {e}")
        await message.answer(f"Ошибка генерации {key}: {e}")

@dp.message(Command("test_ref_bingx_long_whale"))
async def _t1(m: Message): await _run_ref_test(m, "bingx_long_whale")
@dp.message(Command("test_ref_bingx_long_dot"))
async def _t2(m: Message): await _run_ref_test(m, "bingx_long_dot")
@dp.message(Command("test_ref_bingx_long_football"))
async def _t3(m: Message): await _run_ref_test(m, "bingx_long_football")
@dp.message(Command("test_ref_bingx_short_whale"))
async def _t4(m: Message): await _run_ref_test(m, "bingx_short_whale")
@dp.message(Command("test_ref_bingx_short_dot"))
async def _t5(m: Message): await _run_ref_test(m, "bingx_short_dot")
@dp.message(Command("test_ref_bingx_short_football"))
async def _t6(m: Message): await _run_ref_test(m, "bingx_short_football")
@dp.message(Command("test_ref_bybit_plus_long"))
async def _t7(m: Message): await _run_ref_test(m, "bybit_plus_long")
@dp.message(Command("test_ref_bybit_plus_short"))
async def _t8(m: Message): await _run_ref_test(m, "bybit_plus_short")
@dp.message(Command("test_ref_bybit_minus_long"))
async def _t9(m: Message): await _run_ref_test(m, "bybit_minus_long")
@dp.message(Command("test_ref_bingx_normal_long"))
async def _t10(m: Message): await _run_ref_test(m, "bingx_normal_long")
@dp.message(Command("test_ref_bingx_normal_short"))
async def _t11(m: Message): await _run_ref_test(m, "bingx_normal_short")

@dp.message(Command("test_ref_all"))
async def test_ref_all(message: Message):
    """Send all 11 reference-matching screens in sequence so the user can
    eyeball them against the assets/{bingx,bybit}/*.JPG / *.jpg references."""
    await message.answer("Запускаю генерацию всех 11 эталонов…")
    for key in _REF_TEST_CASES.keys():
        await _run_ref_test(message, key)


async def _run_custom_usdt_test(message: Message, side: str):
    entry = 0.1068
    exit_price = 0.1092 if side == "long" else 0.1040
    leverage = 50.0
    deposit = 1000.0
    _, _, pnl_percent = compute_pnl_breakdown(entry, exit_price, leverage, side, "bybit")
    pnl_usdt = pnl_percent / 100 * deposit

    image_data = {
        "username": "ТЕСТ ПОЛЬЗОВАТЕЛЬ",
        "symbol": "PYTHUSDT",
        "pnl_usdt": round(pnl_usdt, 2),
        "entry": entry,
        "exit": exit_price,
        "leverage": "50.0x",
        "side": side,
    }

    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_usdt_image, image_data)
        await message.answer_photo(FSInputFile(path))
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await message.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)


@dp.message(Command("test_custom_bybit_usdt_long"))
async def test_custom_bybit_usdt_long(message: Message):
    await _run_custom_usdt_test(message, side="long")


@dp.message(Command("test_custom_bybit_usdt_short"))
async def test_custom_bybit_usdt_short(message: Message):
    await _run_custom_usdt_test(message, side="short")



# =====================================================
# МАРАФОН
# =====================================================


@dp.callback_query(F.data == "marathon:menu")
async def marathon_menu(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is None:
        await call.message.answer(
            "Марафон ещё не запущен.\n\nОтправь стартовый депозит (например, 100)."
        )
        await state.set_state(MarathonStatesGroup.start_deposit)
    else:
        start_val = marathon["start"]
        balance = marathon["balance"]
        pnl_total = balance - start_val
        pnl_pct = (pnl_total / start_val * 100) if start_val else 0.0
        kb = InlineKeyboardBuilder()
        kb.button(text="🚀 Сделка в марафоне", callback_data="marathon:start")
        kb.button(text="🛑 Выключить марафон", callback_data="marathon:stop")
        kb.adjust(1)
        await call.message.answer(
            f"🏁 Марафон\nСтарт: {start_val:.2f} USDT\n"
            f"Текущий баланс: {balance:.2f} USDT\n"
            f"Итог: {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%)",
            reply_markup=kb.as_markup(),
        )
    await call.answer()

@dp.message(MarathonStatesGroup.start_deposit)
async def marathon_set_start(message: Message, state: FSMContext):
    try:
        start_val = float(message.text.replace(",", "."))
        if start_val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число, например: 100")
        return
    user_id = message.from_user.id
    MARATHON[user_id] = {"start": start_val, "balance": start_val}
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Bybit", callback_data="exchange_bybit")
    kb.button(text="📊 BingX", callback_data="exchange_bingx")
    kb.adjust(1)
    await message.answer(
        f"Марафон запущен! Стартовый депозит: {start_val:.2f} USDT."
    )
    await message.answer("Выбери биржу для сделки в марафоне:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "marathon:start")
async def marathon_start(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if user_id not in MARATHON:
        await call.message.answer("Сначала запусти марафон через 🏁 Марафон.")
        await call.answer()
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Bybit", callback_data="exchange_bybit")
    kb.button(text="📊 BingX", callback_data="exchange_bingx")
    kb.adjust(1)
    await call.message.answer("Выбери биржу для сделки в марафоне:", reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "marathon:stop")
async def marathon_stop(call: CallbackQuery, state: FSMContext):
    MARATHON.pop(call.from_user.id, None)
    await state.clear()
    await call.message.answer("Марафон выключен.")
    await call.answer()

# =====================================================
# НАВИГАЦИЯ TRADEFORM
# =====================================================
@dp.callback_query(F.data == "trial_access")
async def trial_access(call: CallbackQuery):
    user_id = call.from_user.id
    activated = activate_trial(user_id)
    if activated:
        kb = InlineKeyboardBuilder()
        kb.button(text="⚡ Быстрый скрин", callback_data="quick_signal")
        kb.button(text="🕘 История", callback_data="history_show")
        kb.button(text="📊 Сводка", callback_data="series_show")
        kb.button(text="👤 Профиль", callback_data="profile_show")
        kb.button(text="📊 Bybit", callback_data="exchange_bybit")
        kb.button(text="📊 BingX", callback_data="exchange_bingx")
        kb.button(text="🎨 Кастом Bybit", callback_data="custom_bybit")
        kb.button(text="💵 Кастом Bybit $", callback_data="custom_bybit_usdt")
        kb.button(text="🎨 Кастом BingX", callback_data="custom_bingx")
        kb.button(text="🏁 Марафон", callback_data="marathon:menu")
        kb.adjust(1)
        await call.message.answer(
            "✅ Пробный доступ активирован на 2 дня!\n\nВыбери режим:",
            reply_markup=kb.as_markup()
        )
    else:
        await call.message.answer(
            "❌ Пробный период уже был использован.\n"
            f"Для полного доступа напиши: {ADMIN_USERNAME}"
        )
    await call.answer()


@dp.callback_query(lambda c: c.data == "restart")
async def restart(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Выбери режим:", reply_markup=get_main_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "back")
async def go_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    prev = data.get("prev_state")
    steps = {
        TradeForm.symbol: ("Введи монету (например BTCUSDT)", TradeForm.symbol, None),
        TradeForm.side: ("Выбери направление 👇", TradeForm.side, side_kb),
        TradeForm.entry: ("Введите цену входа:", TradeForm.entry, back_kb),
        TradeForm.mark: ("Введите цену маркировки:", TradeForm.mark, mark_price_kb),
        TradeForm.amount: ("На какую сумму заходишь? (USDT)", TradeForm.amount, back_kb),
        TradeForm.deposit: ("Какой депозит? (USDT)", TradeForm.deposit, back_kb),
        TradeForm.leverage: ("Введите плечо (например 10)", TradeForm.leverage, back_kb),
    }
    step = steps.get(prev)
    if step:
        text, st, kb = step
        await show_step(call.message, state, text, kb)
        await state.set_state(st)
    else:
        await call.message.answer("Выбери режим:", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("exchange_"))
async def exchange_selected(call: CallbackQuery, state: FSMContext):
    has_access, _ = check_access(call.from_user.id)
    if not has_access:
        await call.answer("🔒 Доступ закрыт. Нажми /start и оформи доступ.", show_alert=True)
        return

    await state.update_data(
        exchange=call.data.split("_")[1],
        prev_state=TradeForm.exchange,
    )
    await show_step(call.message, state, "Введи монету (например BTCUSDT)")
    await state.set_state(TradeForm.symbol)
    await call.answer()


@dp.message(TradeForm.symbol)
async def get_symbol(message: Message, state: FSMContext):
    symbol = message.text.upper()
    data = await state.get_data()
    exchange = data.get("exchange")
    if not exchange:
        await message.answer("Ошибка: биржа не выбрана. Начните сначала /start")
        await state.clear()
        return
    # Получаем точность асинхронно
    precision = await async_get_price_precision(exchange, symbol)
    await state.update_data(symbol=symbol, price_precision=precision, prev_state=TradeForm.symbol)
    await safe_delete_message(message)
    await show_step(message, state, "Выбери направление 👇", side_kb)
    await state.set_state(TradeForm.side)

@dp.callback_query(TradeForm.side, lambda c: c.data in ("side_long", "side_short"))
async def side_selected(call: CallbackQuery, state: FSMContext):
    side = "long" if call.data == "side_long" else "short"
    await state.update_data(side=side, prev_state=TradeForm.side)
    await show_step(call.message, state, "Введите цену входа:", back_kb)
    await state.set_state(TradeForm.entry)
    await call.answer()

@dp.message(TradeForm.entry)
async def get_entry(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(entry=value, prev_state=TradeForm.entry)
    await safe_delete_message(message)
    await show_step(message, state, "Введите цену маркировки:", mark_price_kb)
    await state.set_state(TradeForm.mark)

@dp.message(TradeForm.mark)
async def get_mark(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(mark=value, prev_state=TradeForm.mark)
    await safe_delete_message(message)
    await show_step(message, state, "На какую сумму заходишь? (USDT)", back_kb)
    await state.set_state(TradeForm.amount)

@dp.message(TradeForm.amount)
async def get_amount(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(amount=value, prev_state=TradeForm.amount)
    await safe_delete_message(message)
    user_id = message.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is not None:
        await state.update_data(deposit=marathon["balance"], prev_state=TradeForm.deposit)
        await show_step(message, state, "Введите плечо (например 10)", back_kb)
        await state.set_state(TradeForm.leverage)
        return
    await show_step(message, state, "Какой депозит? (USDT)", back_kb)
    await state.set_state(TradeForm.deposit)

@dp.message(TradeForm.deposit)
async def get_deposit(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(deposit=value, prev_state=TradeForm.deposit)
    await safe_delete_message(message)
    await show_step(message, state, "Введите плечо (например 10)", back_kb)
    await state.set_state(TradeForm.leverage)

@dp.message(TradeForm.leverage)
async def get_leverage(message: Message, state: FSMContext):
    try:
        leverage = int(message.text)
        if leverage <= 0 or leverage > 125:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 125")
        return
    await safe_delete_message(message)
    data = await state.get_data()
    user_id = message.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is not None:
        data["deposit"] = marathon["balance"]

    qty = calculate_qty(data["exchange"], data["amount"], data["entry"], leverage)
    cost = calculate_cost(data["exchange"], data["amount"], leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(
        data["entry"], data["mark"], qty, data["side"], leverage, data.get("exchange", "bybit")
    )
    liquidation = calculate_liquidation(data["entry"], leverage, data["side"])
    data.update(leverage=leverage, qty=qty, liquidation=liquidation, cost=cost)
    if message.from_user.username:
        data["telegram_username"] = message.from_user.username

    has_access, _ = check_access(message.from_user.id)
    if not has_access:
        can_use, remaining = check_daily_limit(message.from_user.id)
        if not can_use:
            await message.answer(
                "🔒 Лимит 3 бесплатных скрина в день исчерпан.\n"
                f"Напиши {ADMIN_USERNAME} для полного доступа.",
                reply_markup=restart_kb
            )
            await state.clear()
            return

    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(
            _THREAD_POOL, generate_trade_image, data, percent, percent, pnl_usdt
        )
        await message.answer_photo(FSInputFile(path), reply_markup=restart_kb)
        has_access, _ = check_access(message.from_user.id)
        if not has_access:
            increment_usage(message.from_user.id)
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await message.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)

    if marathon is not None:
        marathon["balance"] += pnl_usdt
        start_val = marathon["start"]
        balance = marathon["balance"]
        pnl_total = balance - start_val
        pnl_pct = (pnl_total / start_val * 100) if start_val else 0.0
        await message.answer(
            f"🏁 Марафон\nСтарт: {start_val:.2f} USDT\n"
            f"Текущий баланс: {balance:.2f} USDT\n"
            f"Итог: {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%)"
        )
    await state.clear()
    await state.clear()

# =====================================================
# API: ASYNC цены и точность
# =====================================================
async def async_get_mark_price(exchange: str, symbol: str) -> float | None:
    cache_key = f"price:{exchange}:{symbol}"
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]
    try:
        session = await get_http_session()
        if exchange == "bybit":
            url = "https://api.bybit.com/v5/market/tickers"
            params = {"category": "linear", "symbol": symbol}
            async with session.get(url, params=params) as r:
                data = await r.json()
            price = float(data["result"]["list"][0]["markPrice"])
        elif exchange == "bingx":
            if "-" not in symbol:
                symbol = symbol.replace("USDT", "-USDT")
            url = "https://open-api.bingx.com/openApi/swap/v2/quote/price"
            async with session.get(url, params={"symbol": symbol}) as r:
                data = await r.json()
            price = float(data["data"]["price"])
        else:
            return None
        _PRICE_CACHE[cache_key] = price
        return price
    except Exception as e:
        logger.error(f"MARK PRICE ERROR: {e}")
        return None

async def async_get_price_precision(exchange: str, symbol: str) -> int | None:
    cache_key = f"precision:{exchange}:{symbol}"
    if cache_key in _PRECISION_CACHE:
        return _PRECISION_CACHE[cache_key]
    try:
        session = await get_http_session()
        if exchange == "bybit":
            url = "https://api.bybit.com/v5/market/instruments-info"
            async with session.get(url, params={"category": "linear", "symbol": symbol}) as r:
                data = await r.json()
            tick = data["result"]["list"][0]["priceFilter"]["tickSize"]
            precision = len(tick.split(".")[1].rstrip("0")) if "." in tick else 0
        elif exchange == "bingx":
            url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
            async with session.get(url) as r:
                data = await r.json()
            precision = next(
                (int(item["pricePrecision"]) for item in data["data"] if item["symbol"] == symbol),
                2,
            )
        else:
            return None
        _PRECISION_CACHE[cache_key] = precision
        return precision
    except Exception as e:
        logger.error(f"PRECISION ERROR: {e}")
        return None

# =====================================================
# КНОПКА: взять цену с биржи
# =====================================================
@dp.callback_query(lambda c: c.data == "get_mark_price")
async def get_mark_from_exchange(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    exchange = data.get("exchange")
    symbol = data.get("symbol")
    if not exchange or not symbol:
        await call.answer("Нет данных", show_alert=True)
        return
    price = await async_get_mark_price(exchange, symbol)
    if price is None:
        await call.answer("Не удалось получить цену", show_alert=True)
        return
    await state.update_data(mark=price, prev_state=TradeForm.mark)
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    await show_step(call.message, state, "На какую сумму заходишь? (USDT)", back_kb)
    await state.set_state(TradeForm.amount)
    await call.answer("Цена получена ✅")

# =====================================================
# РАСЧЁТЫ
# =====================================================
def calculate_qty(exchange: str, amount: float, entry: float, leverage: int) -> float:
    if entry <= 0:
        return 0.0
    qty = amount * leverage / entry
    return round(qty, 4 if exchange == "bybit" else 2)

def format_price(value: float, precision: int | None = None) -> str:
    if precision is not None:
        return f"{value:,.{precision}f}"
    if value == 0:
        return "0"
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")

def format_qty(value: float) -> str:
    """Format large quantities: 2000000 -> 2.000M, 1500 -> 1,500"""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.3f}M"
    if abs(value) >= 1_000:
        return f"{value:,.0f}"
    return f"{value:,.4f}"

def calculate_liquidation(entry: float, leverage: int | float, side: str, mm: float = 0.005) -> float:
    if leverage <= 0 or entry <= 0:
        return 0.0
    return entry * (1 - 1 / leverage + mm) if side == "long" else entry * (1 + 1 / leverage - mm)

def calculate_cost(exchange: str, amount: float, leverage: int | float) -> float:
    return round(amount * leverage, 2)

def calculate_pnl_linear(
    entry: float, mark: float, qty: float, side: str, leverage: float,
    exchange: str = "bybit",
) -> tuple[float, float, float]:
    """USDT-margined linear-perp PnL with taker fees subtracted on both legs,
    so the returned ROI matches the exchange's «Закрытый PnL» view."""
    gross_usd = qty * (mark - entry) if side == "long" else qty * (entry - mark)
    margin = (entry * qty / leverage) if leverage and entry else 0.0
    rate = BYBIT_TAKER_RATE if exchange == "bybit" else BINGX_TAKER_RATE
    fee_usd = qty * (entry + mark) * rate
    pnl_usd = gross_usd - fee_usd
    pnl_percent = (pnl_usd / margin * 100) if margin > 0 else 0.0
    return round(pnl_usd, 4), round(margin, 4), round(pnl_percent, 2)

# =====================================================
# SUMMARY / show_step
# =====================================================
def build_summary(data: dict) -> str:
    parts = ["📊 Уже введено:\n"]
    if "exchange" in data:
        parts.append(f"🏦 Биржа: {data['exchange'].title()}\n")
    if "symbol" in data:
        parts.append(f"🪙 Монета: {data['symbol']}\n")
    if "side" in data:
        parts.append(f"📈 Направление: {'Лонг' if data['side'] == 'long' else 'Шорт'}\n")
    if "entry" in data:
        parts.append(f"🎯 Вход: {data['entry']}\n")
    if "mark" in data:
        parts.append(f"📍 Марк: {data['mark']}\n")
    if "amount" in data:
        parts.append(f"💰 Сумма: {data['amount']} USDT\n")
    if "deposit" in data:
        parts.append(f"🏦 Депозит: {data['deposit']} USDT\n")
    return "".join(parts)

def build_custom_summary(data: dict) -> str:
    exchange = (data or {}).get("exchange", "bybit").title()
    parts = [f"📊 КАСТОМ {exchange}\n\n"]
    for key, emoji, label in [
        ("username", "👤", None),
        ("symbol", "🪙", None),
        ("entry", "💰 Вход:", None),
        ("exit", "🚪 Выход:", None),
        ("leverage", "⚙️", None),
        ("referral", "👥 Рефкод:", None),
        ("datetime_str", "🕒", None),
    ]:
        if key in data:
            if key == "side":
                emoji_s = "📈" if data["side"] == "long" else "📉"
                parts.append(f"{emoji_s} {'Лонг' if data['side'] == 'long' else 'Шорт'}\n")
            else:
                parts.append(f"{emoji} {data[key]}\n")
    return "".join(parts)

_PRETTY_QUESTIONS = {
    "Введи монету (например BTCUSDT)": "🪙 Введите монету:",
    "Выбери направление 👇": "📈 Направление сделки:",
    "Введите цену входа:": "💰 Цена входа:",
    "Введите цену маркировки:": "📍 Цена сейчас:",
    "На какую сумму заходишь? (USDT)": "💵 Сумма (USDT):",
    "Какой депозит? (USDT)": "🏦 Депозит (USDT):",
    "Введите плечо (например 10)": "⚙️ Плечо:",
}

async def show_step(
    message: Message,
    state: FSMContext,
    question: str,
    keyboard: InlineKeyboardMarkup | None = None,
):
    data = await state.get_data()
    summary = (
        build_custom_summary(data)
        if "username" in data and data.get("exchange") in ("bybit", "bingx")
        else build_summary(data)
    )
    question_text = _PRETTY_QUESTIONS.get(question, f"❓ {question}")
    last_msg_id = data.get("last_bot_msg_id") or data.get("custom_last_msg_id")
    if last_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, last_msg_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    msg = await message.answer(
        f"{summary}\n{question_text}", parse_mode="HTML", reply_markup=keyboard
    )
    await state.update_data(last_bot_msg_id=msg.message_id, custom_last_msg_id=msg.message_id)

# =====================================================
# РЕНДЕР ОБЫЧНОЙ КАРТИНКИ
# =====================================================
def draw_gray_box(draw, x, y, text, font, cfg):
    padding_x = cfg.get("pad_x", 16)
    padding_y = cfg.get("pad_y", 10)
    min_h = cfg.get("min_h", 0)
    min_w = cfg.get("min_w", 0)
    radius = cfg.get("radius", 14)
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    box_h = max(min_h, h + padding_y * 2)
    box_w = max(min_w, w + padding_x * 2)
    # Pill colour from new BingX UI ≈ (35,35,38) — clearly visible on dark bg
    fill = cfg.get("fill", (35, 35, 38))
    draw.rounded_rectangle(
        (x - box_w // 2, y - box_h // 2,
         x + box_w // 2, y + box_h // 2),
        radius=radius, fill=fill,
    )
    draw.text((x, y), text, fill=(255, 255, 255), font=font, anchor="mm")

def draw_side_badge(draw, x, y, text, color, exchange, fonts_cfg, cfg=None):
    img_h = draw.im.size[1]
    badge_font_file = fonts_cfg["files"].get("badge", fonts_cfg["files"]["regular"])
    font = _load_font(
        os.path.join(BASE_DIR, badge_font_file),
        scale_font(fonts_cfg["sizes"]["badge"], img_h),
    )
    if exchange == "bingx" and cfg is not None:
        sizes = fonts_cfg.get("sizes", {})
        is_long = (text == "Лонг")
        if is_long:
            pad_x = sizes.get("badge_pad_green_x", 16)
            pad_y = sizes.get("badge_pad_green_y", 16)
            min_w = sizes.get("badge_min_green_w", 110)
            min_h = sizes.get("badge_min_green_h", 46)
        else:
            pad_x = sizes.get("badge_pad_red_x", 16)
            pad_y = sizes.get("badge_pad_red_y", 16)
            min_w = sizes.get("badge_min_red_w", 110)
            min_h = sizes.get("badge_min_red_h", 25)
        radius = cfg.get("radius", 14)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        box_w = max(min_w, text_w + pad_x * 2)
        box_h = max(min_h, text_h + pad_y * 2)
    else:
        padding_x, padding_y = 16, 18
        radius = cfg.get("radius", 20) if cfg is not None else 20
        bbox = draw.textbbox((0, 0), text, font=font)
        box_w = bbox[2] - bbox[0] + padding_x * 2
        box_h = bbox[3] - bbox[1] + padding_y * 1.5
    x1, y1 = x - box_w // 2, y - box_h // 2
    x2, y2 = x1 + box_w, y1 + box_h
    badge_style = fonts_cfg.get("badge_style", "outline")
    if badge_style == "filled":
        # BingX: заливка цветом, белый текст
        draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=color)
        text_color = (255, 255, 255)
    else:
        # Bybit: серый фон, цветной текст (зелёный/красный), без рамки
        draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(30, 33, 38))
        text_color = color
    text_offset_y = fonts_cfg.get("sizes", {}).get("badge_text_offset_y", 0) if exchange == "bingx" else 0
    draw.text(((x1 + x2) / 2, (y1 + y2) / 2 + text_offset_y), text, fill=text_color, font=font, anchor="mm")

def clear_by_layout(img, draw, layout, key):
    cfg = layout.get(key)
    if cfg is None:
        return
    iw, ih = img.size
    x, y = px(cfg["x"], iw), px(cfg["y"], ih)
    cw, ch = px(cfg["w"], iw), px(cfg["h"], ih)
    bgx = px(cfg["bg_x"], iw) if "bg_x" in cfg else x + 2
    bgy = px(cfg["bg_y"], ih) if "bg_y" in cfg else y + 2
    bg = img.getpixel((bgx, bgy))
    draw.rectangle((x, y, x + cw, y + ch), fill=bg)

def draw_bingx_icon(
    img: Image.Image,
    symbol: str,
    layout: dict,
    font: ImageFont.FreeTypeFont,
    w: int,
    h: int,
) -> None:
    cfg = layout.get("symbol_icon")
    if not cfg:
        return

    icon_path = os.path.join(BASE_DIR, "assets", "bingx", "icon.png")
    if not os.path.exists(icon_path):
        return

    # грузим иконку
    size = int(cfg.get("size", 24))
    icon = _load_icon(icon_path, size)

    # базовая точка — такая же, как у текста символа
    x = int(cfg["x"] * w) + cfg.get("dx", 0)
    y = int(cfg["y"] * h) + cfg.get("dy", 0)

    # считаем ширину символа тем же шрифтом
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox = d.textbbox((0, 0), symbol, font=font)
    text_width = bbox[2] - bbox[0]

    gap = cfg.get("gap", 8)

    # финальная позиция иконки — сразу после текста
    x = x + text_width + gap

    img.paste(icon, (x, y), icon)


def generate_bingx_normal_card(data: dict, percent: float, pnl_usdt: float) -> str:
    """BingX position card rendered ON TOP of the user's actual reference
    screenshots (NORMAL_BINGX_LONG.jpg / NORMAL_BINGX_SHORT.jpg, 1290x~800).
    Only the dynamic values are wiped and re-drawn — every static element
    (background, pills, labels, buttons, refresh icon, candle icon) keeps
    its original BingX styling pixel-for-pixel.
    """
    side = data["side"]
    is_long = side == "long"
    template_name = "NORMAL_BINGX_LONG.jpg" if is_long else "NORMAL_BINGX_SHORT.jpg"
    template_path = os.path.join(BASE_DIR, "assets", "bingx", template_name)
    if not os.path.exists(template_path):
        # Fallback to old template if user hasn't uploaded references
        return _legacy_generate_trade_image(data, percent, percent, pnl_usdt)

    output_dir = os.path.join(BASE_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"result_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    if img.mode != "RGB":
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    BG    = (10, 10, 10)
    WHITE = (255, 255, 255)
    GREEN = (44, 196, 134)
    RED   = (245, 80, 95)
    GREY  = (138, 144, 158)

    fp_r = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Regular.otf")
    fp_b = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Semibold.otf")

    # Per-template y offset (LONG sits ~15px higher)
    YOFF = -15 if is_long else 0

    def wipe(x1, y1, x2, y2):
        draw.rectangle([x1, y1, x2, y2], fill=BG)

    # ---- 1. Symbol (ETHUSDT / GIGGLEUSDT / …) + candle icon next to it ----
    # In the real BingX UI the icon FOLLOWS the symbol, so we wipe the entire
    # symbol+icon strip and re-draw the symbol followed by the candle icon
    # extracted from the original screenshot.
    symbol = data["symbol"].upper()
    sym_y_center = 67 + YOFF
    sym_font = _load_font(fp_b, 46)
    # Wipe everything across the symbol/icon strip (well past where any symbol
    # could end on either base template).
    wipe(45, 40 + YOFF, 430, 100 + YOFF)
    draw.text((52, sym_y_center), symbol, fill=WHITE, font=sym_font, anchor="lm")
    # Paste candle icon ~24px after the symbol's right edge
    sym_w = draw.textlength(symbol, font=sym_font)
    icon_path = os.path.join(BASE_DIR, "assets", "bingx", "candle_icon.png")
    if os.path.exists(icon_path):
        icon = Image.open(icon_path).convert("RGBA")
        ix = int(52 + sym_w + 24)
        iy = sym_y_center - icon.height // 2
        img.paste(icon, (ix, iy), icon)
        draw = ImageDraw.Draw(img)

    # ---- 2. Leverage pill text (e.g. "5X") — wipe and rewrite to match user's leverage ----
    lev_int = int(float(str(data["leverage"]).replace("x", "").replace("X", "")))
    lev_text = f"{lev_int}X"
    lev_pill_y = (110 + 169) // 2 + YOFF
    lev_pill_x = (336 + 416) // 2  # short pill bounds
    if is_long:
        lev_pill_x = (327 + 407) // 2
    # Repaint the leverage pill bg (29,29,29) and add text
    pill_color = (29, 29, 29)
    if is_long:
        x1, x2 = 327, 407
    else:
        x1, x2 = 336, 416
    y1, y2 = 110 + YOFF, 169 + YOFF
    draw.rounded_rectangle((x1, y1, x2, y2), radius=18, fill=pill_color)
    lev_font = _load_font(fp_r, 36)
    draw.text((lev_pill_x, lev_pill_y), lev_text, fill=WHITE, font=lev_font, anchor="mm")

    # ---- 3. Big PnL value + (% change) on the right side ----
    color = GREEN if pnl_usdt >= 0 else RED
    val_text = f"{pnl_usdt:+.4f}"
    pct_text = f"({percent:+.2f}%)"
    wipe(770, 100 + YOFF, 1245, 175 + YOFF)
    pnl_val_font = _load_font(fp_r, 48)
    pnl_pct_font = _load_font(fp_r, 36)
    pct_w = draw.textlength(pct_text, font=pnl_pct_font)
    pct_y = 142 + YOFF
    draw.text((1235, pct_y), pct_text, fill=color, font=pnl_pct_font, anchor="rm")
    draw.text((1235 - pct_w - 14, pct_y), val_text, fill=color, font=pnl_val_font, anchor="rm")

    # ---- Common values for row A and the risk calculation ----
    margin_v = float(data.get("amount") or 0)
    lev_v    = float(data.get("leverage") or 0)
    entry_v  = float(data.get("entry") or 0)
    mark_v   = float(data.get("mark") or 0)
    qty_u = (margin_v * lev_v / entry_v) if entry_v > 0 else 0.0
    auto_position = qty_u * (mark_v if mark_v else entry_v)
    # Optional override so the user can dictate exact values shown on the card.
    position_usdt = float(data["position_usdt"]) if data.get("position_usdt") is not None else auto_position

    # ---- 4. Row A values: Position USDT / Margin / Risk ----
    val_font = _load_font(fp_r, 38)
    rowA_y = 302 + YOFF
    # Wider wipes to fully erase any value the base template carries
    wipe(45,  rowA_y - 22,  340, rowA_y + 22)   # Position
    wipe(520, rowA_y - 22,  860, rowA_y + 22)   # Margin
    wipe(1080, rowA_y - 22, 1250, rowA_y + 22)  # Risk
    pos_text = f"{position_usdt:,.2f}".rstrip("0").rstrip(".") or "0"
    mar_text = f"{margin_v:,.4f}"
    # Risk: prefer explicit override; otherwise approximate maintenance/balance ratio.
    if data.get("risk_pct") is not None:
        risk_val = float(data["risk_pct"])
    else:
        mm_rate = 0.004
        maint_margin = position_usdt * mm_rate
        margin_balance = margin_v + pnl_usdt
        risk_val = (maint_margin / margin_balance * 100) if margin_balance > 0 else 0.0
    risk_text = f"{risk_val:.2f}%" if risk_val > 0 else "--"
    risk_color = GREEN if 0 < risk_val < 50 else (RED if risk_val >= 70 else GREEN)

    draw.text((52, rowA_y), pos_text, fill=WHITE, font=val_font, anchor="lm")
    draw.text((529, rowA_y), mar_text, fill=WHITE, font=val_font, anchor="lm")
    draw.text((1235, rowA_y), risk_text, fill=risk_color, font=val_font, anchor="rm")

    # ---- 5. Row B values: Entry / Mark / Liquidation ----
    rowB_y = 440 + YOFF
    wipe(45,  rowB_y - 22,  340, rowB_y + 22)
    wipe(520, rowB_y - 22,  860, rowB_y + 22)
    wipe(1080, rowB_y - 22, 1250, rowB_y + 22)
    liq_v = float(data.get("liquidation") or 0)
    liq_color = GREEN if (entry_v > 0 and abs(liq_v - entry_v) / entry_v > 0.05) else RED
    draw.text((52, rowB_y), format_price(entry_v).replace(",", ","), fill=WHITE, font=val_font, anchor="lm")
    draw.text((529, rowB_y), format_price(mark_v).replace(",", ","), fill=WHITE, font=val_font, anchor="lm")
    draw.text((1235, rowB_y), format_price(liq_v) if liq_v > 0 else "0",
              fill=liq_color, font=val_font, anchor="rm")

    # ---- "Вся позиция: TP/SL" — overridable via data["tp_value"] / data["sl_value"] ----
    tpsl_y = 540 + YOFF
    wipe(780, tpsl_y - 22, 1245, tpsl_y + 22)
    tpsl_font = _load_font(fp_r, 32)
    tp_value = str(data.get("tp_value") or "--")
    sl_value = str(data.get("sl_value") or "--")
    chev = "›"
    chev_w = draw.textlength(chev, font=tpsl_font)
    draw.text((1235, tpsl_y), chev, fill=GREY, font=tpsl_font, anchor="rm")
    cursor = 1235 - chev_w - 10
    sl_w = draw.textlength(sl_value, font=tpsl_font)
    draw.text((cursor, tpsl_y), sl_value, fill=RED, font=tpsl_font, anchor="rm")
    cursor -= sl_w + 4
    slash_w = draw.textlength("/", font=tpsl_font)
    draw.text((cursor, tpsl_y), "/", fill=GREY, font=tpsl_font, anchor="rm")
    cursor -= slash_w + 4
    tp_w = draw.textlength(tp_value, font=tpsl_font)
    draw.text((cursor, tpsl_y), tp_value, fill=GREEN, font=tpsl_font, anchor="rm")
    cursor -= tp_w + 8
    label = "Вся позиция: "
    draw.text((cursor, tpsl_y), label, fill=GREY, font=tpsl_font, anchor="rm")

    # ---- 6. Realized P/U value on the right (just below T-п/с-л row) ----
    realized = float(data.get("realized_pnl") or 0)
    rl_y = 622 + YOFF
    wipe(1000, rl_y - 22, 1250, rl_y + 22)
    if realized != 0:
        rl_text = f"{realized:+.4f}"
        rl_color = GREEN if realized > 0 else RED
    else:
        rl_text, rl_color = "0", GREEN
    draw.text((1235, rl_y), rl_text, fill=rl_color, font=val_font, anchor="rm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), "result_")
    return output_path


def generate_trade_image(data: dict, percent: float, pnl: float, pnl_usdt: float) -> str:
    if data.get("exchange") == "bingx":
        return generate_bingx_normal_card(data, percent, pnl_usdt)
    return _legacy_generate_trade_image(data, percent, pnl, pnl_usdt)


def _legacy_generate_trade_image(data: dict, percent: float, pnl: float, pnl_usdt: float) -> str:
    exchange = data["exchange"]
    template_path = os.path.join(BASE_DIR, "assets", exchange, "template.png")
    output_dir = os.path.join(BASE_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"result_{uuid.uuid4().hex[:8]}.png")

    cfg = FONTS[exchange]
    layout = LAYOUT[exchange]
    font_regular = os.path.join(BASE_DIR, cfg["files"]["regular"])
    font_bold = os.path.join(BASE_DIR, cfg["files"]["bold"])
    sizes = cfg["sizes"]

    # Копируем шаблон из кэша
    img = _load_template(template_path).copy()
    draw = ImageDraw.Draw(img)

    clear_keys = [
        "clear_symbol", "clear_leverage", "clear_side_badge", "clear_entry",
        "clear_mark", "clear_pnl", "clear_qty", "clear_liq", "clear_margin", "clear_risk",
    ]
    for key in clear_keys:
        if exchange == "bybit" and key == "clear_margin":
            continue
        clear_by_layout(img, draw, layout, key)

    WHITE = (255, 255, 255)
    # Цвета из реальных скриншотов приложений
    if exchange == "bybit":
        GREEN  = (0, 194, 108)     # зелёный PnL/badge (извлечён из template.png)
        RED    = (220, 59, 90)     # красный badge/PnL — малиновый (извлечён из template.png)
        ORANGE = (245, 157, 60)    # оранжевый ликвидация (извлечён из template.png)
    else:  # bingx
        GREEN  = (62, 146, 103)    # зелёный badge fill (из template.png)
        RED    = (218, 102, 97)    # красный PnL (из template.png)
        ORANGE = (255, 162, 56)    # оранжевый ликвидация
    side_color = GREEN if data["side"] == "long" else RED
    pnl_color = GREEN if pnl >= 0 else RED

    symbol_font = _load_font(font_bold, sizes["symbol"])
    pnl_font = _load_font(font_bold, sizes["pnl"])
    lev_font = _load_font(font_regular, sizes["leverage"])

    w, h = img.size

    def pos(c):
        return int(c["x"] * w) + c.get("dx", 0), int(c["y"] * h) + c.get("dy", 0)

    symbol_text = data["symbol"]
    badge_text = "Лонг" if data["side"] == "long" else "Шорт"
    if exchange == "bingx":
        # BingX UI: full 4-decimal precision and a space before the bracketed pct
        pnl_text = f"{pnl_usdt:+.4f} ({pnl:+.2f}%)"
    else:
        pnl_text = f"{pnl_usdt:+.2f}({pnl:+.2f}%)"
    lev_text = f"Кросс {data['leverage']}x" if exchange == "bybit" else ""

    sx, sy = pos(layout["symbol"])
    draw.text((sx, sy), symbol_text, fill=WHITE, font=symbol_font, anchor=layout["symbol"]["anchor"])

    bx, by = pos(layout["side_badge"])
    if exchange == "bybit":
        sym_bbox = draw.textbbox((0, 0), symbol_text, font=symbol_font)
        bx = sx + (sym_bbox[2] - sym_bbox[0]) + 75 + layout["side_badge"].get("dx", 0)
    draw_side_badge(draw, bx, by, badge_text, side_color, exchange, cfg, layout.get("side_badge"))

    px_val, py_val = pos(layout["pnl"])
    draw.text((px_val, py_val), pnl_text, fill=pnl_color, font=pnl_font, anchor=layout["pnl"]["anchor"])

    lx, ly = pos(layout["leverage"])
    draw.text((lx, ly), lev_text, fill=WHITE, font=lev_font, anchor=layout["leverage"]["anchor"])
        
    
    if exchange == "bingx":
        badge_font_file = os.path.join(BASE_DIR, cfg["files"].get("badge", cfg["files"]["regular"]))
        badge_font = _load_font(badge_font_file, scale_font(sizes["badge"], h))
        mx, my = pos(layout["margin_mode"])
        lbx, lby = pos(layout["leverage_bingx"])
        draw_gray_box(draw, mx, my, "Кросс", badge_font, layout["margin_mode"])
        draw_gray_box(draw, lbx, lby, f"{data['leverage']}X", badge_font, layout["leverage_bingx"])
        draw_bingx_icon(img, data["symbol"], layout, symbol_font, w, h)

        
        # ----- Позиция / qty -----
    if exchange == "bybit":
        # Bybit: количество монет
        qty_value = float(data.get("qty") or 0)
        qty_text = format_qty(qty_value)
    else:  # bingx
        # BingX UI shows current notional: qty × mark price (not entry-based notional)
        margin_v = float(data.get("amount") or 0)
        lev_v = float(data.get("leverage") or 0)
        entry_v = float(data.get("entry") or 0)
        mark_v = float(data.get("mark") or 0)
        qty_unrounded = (margin_v * lev_v / entry_v) if entry_v > 0 else 0.0
        qty_value = qty_unrounded * (mark_v if mark_v else entry_v)
        qty_text = f"{qty_value:,.2f}"

    # рисуем qty для ОБЕИХ бирж
    draw_text(
        draw,
        layout,
        "qty",
        qty_text,
        font_regular,
        sizes["qty"],
        WHITE,
        w,
        h,
    )

    precision = data.get("price_precision")

    # дальше — ОБЩИЙ вывод цен для обеих бирж
    draw_text(
        draw,
        layout,
        "entry",
        format_price(data["entry"], precision),
        font_regular,
        sizes["entry"],
        WHITE,
        w,
        h,
    )
    draw_text(
        draw,
        layout,
        "mark",
        format_price(data["mark"], precision),
        font_regular,
        sizes["mark"],
        WHITE,
        w,
        h,
    )
    draw_text(
        draw,
        layout,
        "liq",
        format_price(data["liquidation"], precision),
        font_regular,
        sizes["liq"],
        ORANGE,
        w,
        h,
    )


    precision = data.get("price_precision")

    if exchange == "bingx":
        # BingX-style margin: thousands comma + 4 decimals (e.g. "1,713.2700")
        margin_disp = f"{float(data['amount']):,.4f}"
        draw_text(draw, layout, "margin", margin_disp, font_regular, sizes["qty"], WHITE, w, h)
        draw_text(draw, layout, "entry", format_price(data["entry"], precision), font_regular, sizes["entry"], WHITE, w, h)
        draw_text(draw, layout, "mark", format_price(data["mark"], precision), font_regular, sizes["mark"], WHITE, w, h)
        # Liquidation in green when far from entry (safe), orange when close
        entry_v = float(data.get("entry") or 0)
        liq_v   = float(data.get("liquidation") or 0)
        liq_color = GREEN if (entry_v > 0 and abs(liq_v - entry_v) / entry_v > 0.05) else ORANGE
        draw_text(draw, layout, "liq", format_price(liq_v, precision), font_regular, sizes["liq"], liq_color, w, h)

    if exchange == "bingx" and "risk" in layout:
        # BingX "Риск" = Margin Ratio = maintenance_margin / margin_balance × 100
        # mm rate ≈ 0.4% of position notional for major coins
        margin_v = float(data.get("amount") or 0)
        lev_v    = float(data.get("leverage") or 0)
        entry_v  = float(data.get("entry") or 0)
        mark_v   = float(data.get("mark") or 0)
        qty_u = (margin_v * lev_v / entry_v) if entry_v > 0 else 0.0
        position = qty_u * (mark_v if mark_v else entry_v)
        is_long_local = data.get("side") == "long"
        raw_pnl = (qty_u * (mark_v - entry_v)) if is_long_local else (qty_u * (entry_v - mark_v))
        maint_margin = position * 0.004
        margin_balance = margin_v + raw_pnl
        if margin_balance > 0 and position > 0:
            risk = maint_margin / margin_balance * 100.0
            risk_text = f"{risk:.2f}%" if round(risk, 2) != 0 else "--"
            risk_color = GREEN if risk <= 40 else (ORANGE if risk <= 70 else RED)
        else:
            risk_text, risk_color = "--", ORANGE
        rx, ry = pos(layout["risk"])
        draw.text((rx, ry), risk_text, fill=risk_color,
                  font=_load_font(font_regular, sizes["qty"]),
                  anchor=layout["risk"]["anchor"])

    # Watermark (subtle, bottom-right corner)
    if data.get("telegram_username"):
        wm_font = _load_font(font_regular, max(12, int(h * 0.025)))
        wm_text = f"@{data['telegram_username']}"
        wm_x = w - 10
        wm_y = h - 10
        # Semi-transparent by drawing in dark gray
        draw.text((wm_x, wm_y), wm_text, fill=(60, 60, 65), font=wm_font, anchor="rb")

    img.save(output_path)
    # Синхронная очистка старых файлов — здесь мы уже в пуле потоков
    _cleanup_old_files(os.path.dirname(output_path), "result_")
    return output_path


# =====================================================
# SIGNAL PARSER (free-form text → structured trade)
# =====================================================
import re as _re_signal

_SIGNAL_NUMBER_RX = r'(?<!\w)[\d,]+(?:\.\d+)?'
_SIGNAL_BLACKLIST = {'LONG', 'SHORT', 'ЛОНГ', 'ШОРТ', 'TP', 'SL', 'TARGET', 'STOP',
                     'ENTRY', 'USDT', 'BUY', 'SELL', 'QUALITY', 'SWAP'}

def _parse_number(s: str):
    s = s.strip().rstrip('.,').replace(' ', '')
    if _re_signal.fullmatch(r'\d{1,3}(,\d{3})+(\.\d+)?', s):
        return float(s.replace(',', ''))
    if _re_signal.fullmatch(r'\d+,\d+', s):
        return float(s.replace(',', '.'))
    try:
        return float(s)
    except ValueError:
        return None

def parse_signal(text: str) -> dict:
    """Extract a trade signal from free-form text.

    Returns: {symbol, side, entry, sl, tps[]}.
    Symbol always normalised to TICKERUSDT. Side is "long"/"short"/None.
    Numbers may have thousands-comma (78,373) or decimal (2284.36 / 5,42).
    """
    t = _re_signal.sub(r'[​-‏⁠﻿]', '', text)  # strip zero-width chars
    out = {"symbol": None, "side": None, "entry": None, "sl": None, "tps": []}

    # Symbol — prefer $TICKER, then TICKER-USDT / TICKERUSDT.
    sym = None
    for m in _re_signal.finditer(r'\$([A-Z][A-Z0-9]{1,9})\b', t.upper()):
        cand = m.group(1)
        if cand in _SIGNAL_BLACKLIST: continue
        sym = cand if cand.endswith("USDT") else cand + "USDT"
        break
    if not sym:
        for m in _re_signal.finditer(r'\b([A-Z][A-Z0-9]{1,9})[\s/_-]*USDT(?:[-_]SWAP)?\b', t.upper()):
            cand = m.group(1)
            if cand in _SIGNAL_BLACKLIST: continue
            sym = cand + "USDT"
            break
    out["symbol"] = sym

    if _re_signal.search(r'\bШОРТ\b|\bSHORT\b', t, _re_signal.I) or '📉' in t:
        out["side"] = "short"
    elif _re_signal.search(r'\bЛОНГ\b|\bLONG\b', t, _re_signal.I) or '📈' in t:
        out["side"] = "long"

    # Pass 3 (last resort) — if side is detected but symbol still isn't, accept
    # the first standalone alphanumeric token at the start of any line as the
    # symbol. Handles compact channel format: "LAB LONG 1.49 ...", "PEPE Шорт".
    # Bounded by side-presence so random capitalised words don't trigger.
    if not out["symbol"] and out["side"]:
        for line in t.split('\n'):
            line = line.strip()
            if not line: continue
            m = _re_signal.match(r'^\$?([A-Za-z][A-Za-z0-9]{1,9})\b', line)
            if m:
                cand = m.group(1).upper()
                if cand in _SIGNAL_BLACKLIST: continue
                out["symbol"] = cand if cand.endswith("USDT") else cand + "USDT"
                break

    def find_num_after(keywords):
        for kw in keywords:
            for m in _re_signal.finditer(_re_signal.escape(kw), t, _re_signal.I):
                if m.start() > 0 and t[m.start()-1].isalpha(): continue
                tail = t[m.end():m.end()+80]
                n = _re_signal.search(_SIGNAL_NUMBER_RX, tail)
                if n:
                    v = _parse_number(n.group())
                    if v and v > 0: return v
        return None

    out["entry"] = find_num_after(['Entry', 'Вход', 'вход', '🎯'])
    out["sl"]    = find_num_after(['SL', 'Стоп', 'Stop', '🛑'])

    seen_offsets = set()
    def add_tp(v):
        if v and v > 0 and v not in out["tps"]:
            out["tps"].append(v)
    for kw in (f'TP{i}' for i in range(1, 10)):
        for m in _re_signal.finditer(_re_signal.escape(kw) + r'\b', t, _re_signal.I):
            seen_offsets.add(m.start())
            n = _re_signal.search(_SIGNAL_NUMBER_RX, t[m.end():m.end()+80])
            if n: add_tp(_parse_number(n.group()))
    for kw in ('TP', 'Тейк', 'Take', 'Target', '✅'):
        pat = (_re_signal.compile(r'\b' + _re_signal.escape(kw) + r'\b', _re_signal.I)
               if kw.isalpha() else _re_signal.compile(_re_signal.escape(kw), _re_signal.I))
        for m in pat.finditer(t):
            if m.start() in seen_offsets: continue
            seen_offsets.add(m.start())
            n = _re_signal.search(_SIGNAL_NUMBER_RX, t[m.end():m.end()+80])
            if n: add_tp(_parse_number(n.group()))

    # Last-resort number harvest — if side is found but no Entry/TP keywords
    # gave us anything, consume raw numbers in document order: 1st = entry,
    # 2..6 = TPs (the SL value, if known, is excluded). Handles channel
    # formats like "LAB LONG 1.49 1,77 2,21 3,77 Стоп 1,025".
    if out["side"] and out["entry"] is None and not out["tps"]:
        found = []
        for m in _re_signal.finditer(_SIGNAL_NUMBER_RX, t):
            v = _parse_number(m.group())
            if v and v > 0 and v not in found:
                found.append(v)
        if out["sl"] in found:
            found.remove(out["sl"])
        if found:
            out["entry"] = found[0]
            out["tps"] = found[1:6]
    return out


def looks_like_signal(text: str) -> bool:
    """Quick check whether a free-form message looks like a trade signal."""
    if not text or len(text) < 10: return False
    parsed = parse_signal(text)
    return bool(parsed["symbol"] and parsed["side"] and (parsed["entry"] or parsed["tps"]))


# =====================================================
# КАСТОМНЫЕ КАРТИНКИ
# =====================================================
def generate_custom_bybit_image(data: dict) -> str:
    """Bybit custom share card rendered ON TOP of the user's reference templates
    (Bybit_custom_<plus|minus>PNL_<LONG|SHORT>.JPG, 960x1320). Only the dynamic
    values are wiped and re-drawn — the rocket/wallet illustrations, BYBIT logo,
    avatar icon, ROI/Цена входа/Текущая цена labels, and the bottom promotional
    band stay pixel-perfect from the reference.
    """
    try:
        pnl = float(str(data["pnl"]).replace("%", "").replace(",", "."))
    except ValueError:
        pnl = 0.0
    side = data.get("side", "long")
    is_long = side == "long"

    # Pick a template based on PnL sign + side. minusPNL_SHORT is missing;
    # fall back to minusPNL_LONG (still wallet illustration) — pill colour gets
    # overridden anyway by our re-draw, so the fallback works fine.
    sign = "plus" if pnl >= 0 else "minus"
    side_name = "LONG" if is_long else "SHORT"
    template_name = f"Bybit_custom_{sign}PNL_{side_name}.JPG"
    template_path = os.path.join(BASE_DIR, "assets", "bybit", template_name)
    if not os.path.exists(template_path):
        # Fallbacks
        for cand in (f"Bybit_custom_{sign}PNL_LONG.JPG",
                     "Bybit_custom_plusPNL_LONG.JPG"):
            cand_path = os.path.join(BASE_DIR, "assets", "bybit", cand)
            if os.path.exists(cand_path):
                template_path = cand_path
                break
        else:
            # Legacy fallback to old screenshot_*.png
            return _legacy_generate_custom_bybit_image(data)

    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bybit_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    if img.mode != "RGB":
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size  # 960 x 1320

    BG    = (20, 20, 20)
    WHITE = (255, 255, 255)
    GREEN = (0, 208, 132)
    RED   = (255, 59, 92)
    GRAY  = (140, 150, 172)
    PILL_BG = (27, 27, 27)
    BLACK = (0, 0, 0)

    fp_r = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Regular.otf")
    fp_b = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Semibold.otf")

    def wipe(x1, y1, x2, y2, src_y0=None, src_y1=None):
        """Cover (x1..x2, y1..y2) with a vertical-strip copy from (x1..x2,
        src_y0..src_y1). Tiles vertically if the source is shorter than the
        zone — preserves the chart-grid texture in the Bybit reference instead
        of leaving a flat-fill rectangle.

        The default source is the strip immediately above the zone."""
        zone_h = y2 - y1
        if src_y0 is None:
            src_y1_eff = y1 - 4
            src_y0 = max(0, src_y1_eff - zone_h)
            src_y1 = src_y1_eff
        elif src_y1 is None:
            src_y1 = src_y0 + zone_h
        src_h = src_y1 - src_y0
        if src_h <= 0:
            return
        strip = img.crop((x1, src_y0, x2, src_y1))
        cur_y = y1
        while cur_y < y2:
            paint_h = min(src_h, y2 - cur_y)
            sub = strip if paint_h == src_h else strip.crop((0, 0, strip.width, paint_h))
            img.paste(sub, (x1, cur_y))
            cur_y += paint_h

    # Pre-computed clean source y-ranges (per Bybit reference layout).
    # Bounds verified pixel-precise: each strip ends BEFORE the next text/label
    # starts (anti-alias halo of "+12.72%" reaches y=545; "Цена входа" top
    # stroke begins at y=625, so SUB ends at y=624 to avoid pasting the
    # gray label top into the PnL wipe destination).
    BG_STRIP_LOGO  = (110, 175)   # below BYBIT logo (y=105), above chmst (y=177)
    BG_STRIP_PRE   = (227, 294)   # below chmst (y=224), above WIFUSDT (y=296)
    BG_STRIP_MID   = (346, 407)   # below pill (y=343), above ROI label (y=409)
    BG_STRIP_SUB   = (548, 624)   # below +12.72% (y=545), above "Цена входа" top (y=625)
    BG_STRIP_GAP   = (732, 739)   # below 0.9058 (y=730), above "Текущая цена" (y=740) — only 8px, rarely used

    # Reference exact text positions/sizes/colours measured from
    # Bybit_custom_*.JPG (sub-pixel verified):
    #   chmst:    Reg 40pt, white, x=133, y_center=202
    #   SYMBOL:   Bold 62pt, white, x=62, y_center=320
    #   pill:     Bold 36pt, green/red text, pill bg=(27,27,27), Bold pill text size 36
    #   PnL:      Bold 104pt, brand green/red, x=68, y_center=506
    #   value:    Bold 46pt, white, x=62, y_center=690 / 816
    # Brand colours — sampled from the DOMINANT fill colour of the ref PnL letters
    # (not the brightest anti-alias edge). Earlier (15,222,140)/(255,50,75) were
    # outline/AA values and rendered slightly too neon vs the ref core fill.
    #   green: (5, 196, 109) — most common in +12.72% (71x), darker emerald
    #   red:   (255, 64, 78) — most common in -100.79% (46x)
    GREEN = (5, 196, 109)
    RED   = (255, 64, 78)

    # ---- Username ("chmst" → custom). Avatar at x=61-128 stays.
    # Reference: WHITE, Reg 40pt, x=133 (right edge of avatar).
    # If user skipped the username field, wipe BOTH the avatar AND the text
    # so that area is fully blank (per user request — "имя пользователя
    # оставалось пустым полностью"). ----
    username = str(data.get("username", "")).strip()
    if username:
        username_font = _load_font(fp_r, 40)
        new_w = int(draw.textlength(username, font=username_font))
        wipe_w = max(new_w, 110) + 6
        wipe(133, 188, 133 + wipe_w, 217, *BG_STRIP_LOGO)
        draw.text((133, 202), username, fill=WHITE, font=username_font, anchor="lm")
    else:
        # Wipe both the baked avatar circle (x=55-130) and the baked "chmst"
        # text (x=133-260) — entire row blank.
        wipe(55, 175, 280, 230, *BG_STRIP_LOGO)

    # ---- Symbol + side pill — wipe whole row, then draw symbol then pill ----
    symbol = data["symbol"].upper()
    sym_font = _load_font(fp_b, 62)
    sym_y_center = 320
    # Wipe spans symbol+pill row but stops at x=620 (well before any rocket art at this y).
    wipe(45, 295, 620, 350, *BG_STRIP_MID)
    draw.text((62, sym_y_center), symbol, fill=WHITE, font=sym_font, anchor="lm")
    sym_w = draw.textlength(symbol, font=sym_font)

    # Pill — Bold 36 (185 vs ref 184 width). Pad/radius tuned for ref pill height ~50.
    # User's references use Russian "Лонг"/"Шорт" instead of English Long/Short.
    # Pill text uses MUTED brand colours (sampled from brightest pixels of ref
    # pill text — JPG-degraded values that the user's reference actually shows):
    #   green pill: (43, 197, 124)   red pill: (211, 78, 105)
    # Vivid PnL colours (15,222,140)/(255,50,75) make the pill look TOO bold
    # vs reference (the user explicitly flagged this).
    PILL_GREEN = (43, 197, 124)
    PILL_RED   = (211, 78, 105)
    pill_text_color = PILL_GREEN if is_long else PILL_RED
    leverage_num = float(str(data.get("leverage", "1")).replace("x", "").replace("X", ""))
    pill_text = ("Лонг" if is_long else "Шорт") + f" {leverage_num:.1f}X"
    # Reg 38 — matches reference pill text weight (thin, not bold) and width 188≈ref 184.
    pill_font = _load_font(fp_r, 38)
    pad_x, pad_y = 26, 9
    bb = draw.textbbox((0, 0), pill_text, font=pill_font)
    pw = (bb[2] - bb[0]) + pad_x * 2
    ph = max(50, (bb[3] - bb[1]) + pad_y * 2)
    px = int(62 + sym_w + 28)
    py = sym_y_center
    # Reference pill is SEMI-TRANSPARENT — chart-grid lines are visible
    # through the pill bg. Draw on an alpha overlay (alpha ≈ 200/255 ≈ 78%
    # opacity) and composite, so the underlying grid shows through subtly.
    pill_box = (px, py - ph // 2, px + pw, py + ph // 2)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        pill_box, radius=ph // 2, fill=(*PILL_BG, 200)
    )
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"))
    # Re-bind draw on the (re-pasted) RGB image and draw the pill text on top.
    draw = ImageDraw.Draw(img)
    draw.text((px + pw // 2, py), pill_text, fill=pill_text_color, font=pill_font,
              anchor="mm")

    # ---- Big ROI value (+12.72% / +8.97% / -100.79% etc.) ----
    # Reference +12.72% bbox: x=68-468 w=401. Bold size 106 + anchor x=62
    # (accounts for SF Pro left bearing) lands at exactly the same visible x.
    pnl_text = f"{pnl:+.2f}%"
    pnl_color = GREEN if pnl >= 0 else RED
    pnl_size = 106
    pnl_font = _load_font(fp_b, pnl_size)
    while draw.textlength(pnl_text, font=pnl_font) > int(W * 0.55) and pnl_size > 60:
        pnl_size -= 4
        pnl_font = _load_font(fp_b, pnl_size)
    wipe(45, 465, 555, 550, *BG_STRIP_SUB)
    draw.text((62, 506), pnl_text, fill=pnl_color, font=pnl_font, anchor="lm")

    # ---- Entry / Current prices (white bold 46pt) ----
    val_font = _load_font(fp_b, 46)
    entry_text = (data.get("entry_str") or "").strip() or format_price(data.get("entry", 0))
    exit_text  = (data.get("exit_str")  or "").strip() or format_price(data.get("exit", 0))
    # Tighter wipe matching ref value bbox (x=62-218, h≈37) plus margin.
    wipe(45, 670, 320, 712, *BG_STRIP_SUB)
    draw.text((62, 690), entry_text, fill=WHITE, font=val_font, anchor="lm")
    wipe(45, 796, 320, 838, *BG_STRIP_SUB)
    draw.text((62, 816), exit_text,  fill=WHITE, font=val_font, anchor="lm")

    # ---- Second price label: "Цена выхода" (closed) vs baked "Текущая цена" (open) ----
    # The base reference JPG always has "Текущая цена" baked in. For closed trades
    # the user expects "Цена выхода" — overwrite the label.
    if data.get("status") == "closed":
        # Reference layout:
        #   y=751..752: tiny info-mark (matches the one above "Цена входа")
        #   y=755..757: anti-alias halo of "Текущая" letters
        #   y=758..776: label "Текущая цена" main body
        # Wipe y=754..783 — preserves the mark above and removes the label
        # (including its halo).
        wipe(45, 754, 340, 783, *BG_STRIP_SUB)
        label_font = _load_font(fp_r, 32)
        draw.text((62, 768), "Цена выхода", fill=GRAY, font=label_font, anchor="lm")

    # ---- Referral code on the white footer band — replace just the value ----
    referral_code = str(data.get("referral", "")).strip()
    if referral_code:
        # The "Реферальный код:" label ends at x≈450; the value sits at x=470,
        # vertical center y≈1242. Wipe just the value area (white bg) and rewrite.
        FOOTER_BG = (240, 240, 244)
        ref_font = _load_font(fp_b, 48)
        draw.rectangle((460, 1218, 720, 1268), fill=FOOTER_BG)
        draw.text((470, 1242), referral_code, fill=BLACK, font=ref_font, anchor="lm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), "custom_bybit_")
    return output_path


def _legacy_generate_custom_bybit_image(data: dict) -> str:
    """Old screenshot_long/short.png-based renderer kept as fallback if the new
    Bybit_custom_*.JPG references are missing."""
    try:
        pnl = float(str(data["pnl"]).replace("%", "").replace(",", "."))
    except ValueError:
        pnl = 0.0
    template_side = "long" if pnl >= 0 else "short"
    template_path = os.path.join(BASE_DIR, "assets", "bybit", f"screenshot_{template_side}.png")
    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bybit_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    w, h = img.size
    draw = ImageDraw.Draw(img)
    cfg = FONTS["custom_bybit"]
    layout = BYBIT_CUSTOM_LAYOUT["bybit"]

    icon_path = os.path.join(BASE_DIR, "assets", "bybit", "icon.png")
    cfg_icon = layout.get("symbol_icon")
    if os.path.exists(icon_path) and cfg_icon:
        size = cfg_icon.get("size", 60)
        icon = _load_icon(icon_path, size)
        ix = int(cfg_icon["x"] * w) + cfg_icon.get("dx", 0)
        iy = int(cfg_icon["y"] * h) + cfg_icon.get("dy", 0)
        # Center the icon vertically on the username y
        img.paste(icon, (ix, iy - size // 2), icon)
        draw = ImageDraw.Draw(img)

    fp = lambda name, bold=False: os.path.join(BASE_DIR, cfg["files"]["bold" if bold else "regular"])
    username_font = _load_font(fp("regular"), cfg["sizes"]["username"])
    symbol_font   = _load_font(fp("bold", True), cfg["sizes"]["symbol"])
    pnl_text = f"{pnl:+.2f}%"
    pnl_size = cfg["sizes"]["pnl"]
    _dummy_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    pnl_x = int(layout["pnl"]["x"] * w)
    # Allow PnL to span up to 75% of width so big numbers like -100.79% stay big
    max_pnl_w = int(w * 0.78) - pnl_x
    while pnl_size > 40:
        _pf = _load_font(fp("bold", True), pnl_size)
        _bb = _dummy_draw.textbbox((0, 0), pnl_text, font=_pf)
        if (_bb[2] - _bb[0]) <= max_pnl_w:
            break
        pnl_size -= 4
    pnl_font   = _load_font(fp("bold", True), pnl_size)
    entry_font = _load_font(fp("bold", True), cfg["sizes"]["entry"])
    exit_font  = _load_font(fp("bold", True), cfg["sizes"]["exit"])
    lev_font   = _load_font(fp("regular"), cfg["sizes"]["leverage_text"])
    ref_font   = _load_font(fp("bold", True), cfg["sizes"].get("referral", 30))

    WHITE = (255, 255, 255)
    GREEN = (0, 208, 132)
    RED   = (255, 59, 92)
    GRAY  = (140, 150, 172)

    def pos(c):
        return int(c["x"] * w) + c.get("dx", 0), int(c["y"] * h) + c.get("dy", 0)

    # Username (gray), inline with avatar icon
    if "username" in data and "username" in layout:
        draw.text(pos(layout["username"]), data["username"], fill=GRAY, font=username_font, anchor="lm")

    # Symbol (white bold)
    if "symbol" in layout:
        draw.text(pos(layout["symbol"]), data["symbol"], fill=WHITE, font=symbol_font, anchor="lm")

    # ROI big % value — RED if loss, GREEN if profit
    if "pnl" in layout:
        pnl_color = GREEN if pnl >= 0 else RED
        draw.text(pos(layout["pnl"]), pnl_text, fill=pnl_color, font=pnl_font, anchor="lm")

    # Prices (white bold). Prefer the user's original input string so trailing
    # zeros and exact precision (e.g. "3.46670") are preserved.
    entry_text = (data.get("entry_str") or "").strip() or format_price(data["entry"])
    exit_text  = (data.get("exit_str")  or "").strip() or format_price(data["exit"])
    if "entry" in layout:
        draw.text(pos(layout["entry"]), entry_text, fill=WHITE, font=entry_font, anchor="lm")
    if "exit" in layout:
        draw.text(pos(layout["exit"]), exit_text, fill=WHITE, font=exit_font, anchor="lm")

    # Side pill: "Long 50.0X" / "Short 50.0X" with semi-transparent bg, colored text
    if "cross_leverage" in layout:
        direction_text = "Long" if data["side"] == "long" else "Short"
        leverage_num = float(str(data["leverage"]).replace("x", "").replace("X", ""))
        lev_text = f"{direction_text} {leverage_num:.1f}X"
        sym_bbox = draw.textbbox((0, 0), data["symbol"], font=symbol_font)
        sym_pixel_w = sym_bbox[2] - sym_bbox[0]
        sym_x = int(layout["symbol"]["x"] * w) + layout["symbol"].get("dx", 0)
        cl = layout["cross_leverage"]
        padding_x, padding_y = cl.get("pad_x", 22), cl.get("pad_y", 12)
        bbox = draw.textbbox((0, 0), lev_text, font=lev_font)
        box_w = bbox[2] - bbox[0] + padding_x * 2
        box_h = bbox[3] - bbox[1] + padding_y * 2
        gap = 18
        badge_center_x = sym_x + sym_pixel_w + gap + box_w // 2
        lev_pos = (badge_center_x, int(cl["y"] * h))
        x1, y1 = lev_pos[0] - box_w // 2, lev_pos[1] - box_h // 2
        x2, y2 = x1 + box_w, y1 + box_h
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            [x1, y1, x2, y2], radius=cl.get("radius", 50),
            fill=(35, 35, 48, 130),
        )
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        text_color = GREEN if data["side"] == "long" else RED
        draw.text(lev_pos, lev_text, fill=text_color, font=lev_font, anchor="mm")

    # The two Bybit templates differ on the white footer band:
    # - screenshot_long.png  : "Присоединяйтесь и получите / более __ в бонусах! / Реферальный код:"
    #                          ($5,000 missing; "Реферальный код:" label present, value missing)
    # - screenshot_short.png : "Присоединяйтесь и получите / более $5,000 в бонусах!"
    #                          ($5,000 baked in; no "Реферальный код:" label)
    if template_side == "long":
        # Render "$5,000" centered in the gap on line 2
        bonus_font = _load_font(fp("regular"), 32)
        gap_l, gap_r = int(0.155 * w), int(0.321 * w)
        bb = draw.textbbox((0, 0), "$5,000", font=bonus_font)
        bx = gap_l + (gap_r - gap_l - (bb[2] - bb[0])) // 2
        draw.text((bx, int(layout["bonus"]["y"] * h)), "$5,000",
                  fill=(0, 0, 0), font=bonus_font, anchor="lm")
        # Render only the referral CODE after the existing "Реферальный код:" label
        referral_code = str(data.get("referral", "")).strip()
        if referral_code and "referral" in layout:
            draw.text(pos(layout["referral"]), referral_code,
                      fill=(0, 0, 0), font=ref_font, anchor="lm")
    else:  # short
        # Render the FULL "Реферальный код: <code>" line below "более $5,000 в бонусах!"
        referral_code = str(data.get("referral", "")).strip()
        if referral_code:
            full = f"Реферальный код: {referral_code}"
            draw.text((int(0.041 * w), int(0.951 * h)), full,
                      fill=(0, 0, 0), font=ref_font, anchor="lm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), "custom_bybit_")
    return output_path


def generate_custom_bybit_usdt_image(data: dict) -> str:
    pnl_usdt = float(data.get("pnl_usdt", 0.0))
    template_side = "long" if pnl_usdt >= 0 else "short"
    template_path = os.path.join(BASE_DIR, "assets", "bybit", f"screenshot_{template_side}.png")
    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bybit_usdt_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    w, h = img.size
    draw = ImageDraw.Draw(img)
    cfg = FONTS["custom_bybit"]
    layout = BYBIT_CUSTOM_LAYOUT["bybit"]

    # Шаблоны screenshot_long/short.png чистые — очистка зон НЕ нужна

    icon_path = os.path.join(BASE_DIR, "assets", "bybit", "icon.png")
    cfg_icon = layout.get("symbol_icon")
    if os.path.exists(icon_path) and cfg_icon:
        size = cfg_icon.get("size", 60)
        icon = _load_icon(icon_path, size)
        img.paste(icon, (int(cfg_icon["x"] * w) + cfg_icon.get("dx", 0),
                         int(cfg_icon["y"] * h) + cfg_icon.get("dy", 0)), icon)
        draw = ImageDraw.Draw(img)

    fp = lambda name, bold=False: os.path.join(BASE_DIR, cfg["files"]["bold" if bold else "regular"])
    username_font = _load_font(fp("regular"), cfg["sizes"]["username"])
    symbol_font = _load_font(fp("bold", True), cfg["sizes"]["symbol"])
    pnl_text = f"{pnl_usdt:+.2f}"
    pnl_size = cfg["sizes"]["pnl"]
    _dummy_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    pnl_x = int(layout["pnl"]["x"] * w)
    max_pnl_w = int(w * 0.63) - pnl_x
    while pnl_size > 40:
        _pf = _load_font(fp("bold", True), pnl_size)
        _bb = _dummy_draw.textbbox((0, 0), pnl_text, font=_pf)
        if (_bb[2] - _bb[0]) <= max_pnl_w:
            break
        pnl_size -= 4
    pnl_font = _load_font(fp("bold", True), pnl_size)
    entry_font = _load_font(fp("bold", True), cfg["sizes"]["entry"])
    exit_font = _load_font(fp("bold", True), cfg["sizes"]["exit"])
    lev_font = _load_font(fp("regular"), cfg["sizes"]["leverage_text"])

    # Цвета из Bybit share card (pnl_card.py BYBIT_CONFIG)
    WHITE = (255, 255, 255)
    GREEN = (0, 208, 132)     # #00D084 — Bybit profit
    RED   = (255, 59, 92)     # #FF3B5C — Bybit loss
    GRAY  = (140, 150, 172)   # серые лейблы

    def pos(c):
        return int(c["x"] * w) + c.get("dx", 0), int(c["y"] * h) + c.get("dy", 0)

    if "username" in data and "username" in layout:
        draw.text(pos(layout["username"]), data["username"], fill=GRAY, font=username_font, anchor="lm")
    if "symbol" in layout:
        draw.text(pos(layout["symbol"]), data["symbol"], fill=WHITE, font=symbol_font, anchor="lm")
    if "pnl" in layout:
        pnl_color = GREEN if pnl_usdt >= 0 else RED
        draw.text(pos(layout["pnl"]), pnl_text, fill=pnl_color, font=pnl_font, anchor="lm")
    if "entry" in layout:
        draw.text(pos(layout["entry"]), format_price(data["entry"]), fill=WHITE, font=entry_font, anchor="lm")
    if "exit" in layout:
        draw.text(pos(layout["exit"]), format_price(data["exit"]), fill=WHITE, font=exit_font, anchor="lm")
    if "cross_leverage" in layout:
        direction_text = "Лонг" if data["side"] == "long" else "Шорт"
        leverage_num = float(str(data["leverage"]).replace("x", ""))
        lev_text = f"{direction_text} {leverage_num:.1f}X"
        sym_bbox = draw.textbbox((0, 0), data["symbol"], font=symbol_font)
        sym_pixel_w = sym_bbox[2] - sym_bbox[0]
        sym_x = int(layout["symbol"]["x"] * w) + layout["symbol"].get("dx", 0)
        padding_x, padding_y = 18, 10
        bbox = draw.textbbox((0, 0), lev_text, font=lev_font)
        box_w = bbox[2] - bbox[0] + padding_x * 2
        box_h = bbox[3] - bbox[1] + padding_y * 2
        gap = 16
        badge_center_x = sym_x + sym_pixel_w + gap + box_w // 2
        lev_pos = (badge_center_x, layout["cross_leverage"]["y"] * h)
        x1, y1 = lev_pos[0] - box_w // 2, lev_pos[1] - box_h // 2
        x2, y2 = x1 + box_w, y1 + box_h
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            [x1, y1, x2, y2], radius=60,
            fill=(35, 35, 48, 110),
        )
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        text_color = GREEN if data["side"] == "long" else RED
        draw.text(lev_pos, lev_text, fill=text_color, font=lev_font, anchor="mm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), "custom_bybit_usdt_")
    return output_path


def generate_custom_bingx_image(data: dict) -> str:
    """Render BingX share card on top of the user's BINGX_Custom_*.JPG references
    (whale / dot / football, plus/minus). The references already contain the
    finished artwork and a sample of text — we wipe just the dynamic text zones
    on the dark left column and re-draw the user's data."""
    try:
        pnl = float(str(data["pnl"]).replace("%", "").replace(",", "."))
    except ValueError:
        pnl = 0.0

    # The references are split by PnL sign: plus = green/long theme, minus = red/short theme.
    sign = "plus" if pnl >= 0 else "minus"

    # Variant aliases: legacy bot states use "doge" / "curve"; the user's reference
    # filenames are "whale" / "dot". Accept both, normalise to the reference name.
    variant_alias = {"doge": "whale", "curve": "dot", "whale": "whale", "dot": "dot", "football": "football"}
    variant_in = data.get("template", "football")
    template_variant = variant_alias.get(variant_in, "football")

    template_path = os.path.join(BASE_DIR, "assets", "bingx", f"BINGX_Custom_{template_variant}_{sign}.JPG")
    if not os.path.exists(template_path):
        # Fall back to legacy clean_*.png if the reference is missing.
        legacy_side = "long" if pnl >= 0 else "short"
        legacy_variant = {"whale": "doge", "dot": "curve", "football": "football"}[template_variant]
        template_path = os.path.join(BASE_DIR, "assets", "bingx", f"clean_{legacy_side}_{legacy_variant}.png")
    if not os.path.exists(template_path):
        template_path = os.path.join(BASE_DIR, "assets", "bingx",
                                     f"screenshot_{'long' if pnl >= 0 else 'short'}.png")
    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bingx_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size  # 1800x1800
    draw = ImageDraw.Draw(img)

    # Wipe the dynamic-text zones. BINGX_Custom_*.JPG background is uniform
    # near-black with no chart grid, so a flat-fill works — but the bg colour
    # varies subtly across variants (whale/dot/football) and across y bands.
    # Sample a bg colour PER ZONE from the zone's left margin (x=10..40,
    # always clean of any text or artwork) so the wipe blends in cleanly.
    text_zones = [
        (98,  468,  830, 545),   # Header (covers 'Нереализованная П/У' to x=798)
        (98,  600, 1100, 690),   # Symbol │ Side │ Lev row (Russian descenders)
        (98,  788, 1080, 962),   # Big PnL "+224.11%" / "-100.79%"
        (98, 1056,  890, 1135),  # Price 1 (label + value)
        (98, 1170,  890, 1245),  # Price 2 (label + value)
        (270, 1558, 700, 1625),  # Username "CHM_LAB"
        (270, 1653, 450, 1715),  # Date "05-02"
        (900, 1548, 1530, 1625), # Referral label "Реферальный код"
        (1290, 1643, 1530, 1705),# Referral code "D1BFA4"
    ]
    px = img.load()
    for x1, y1, x2, y2 in text_zones:
        # Sample 5 pixels from the left margin (x=10..40) at the zone's mid-y
        # and pick the median — robust to any stray bright pixel.
        mid_y = (y1 + y2) // 2
        samples = sorted(px[x, mid_y] for x in (10, 18, 26, 34, 42))
        bg_local = samples[len(samples) // 2]
        draw.rectangle([x1, y1, x2, y2], fill=bg_local)

    fp_r = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Regular.otf")
    fp_b = os.path.join(BASE_DIR, "fonts", "SF_Pro_Display_Semibold.otf")

    # Font sizes calibrated to WIDTH of reference text (height matches naturally
    # because SF Pro Display has fixed aspect ratio):
    #   "Реализованная П/У" ref w=614 → Bold 68 (w=608)
    #   "SKYAIUSDT │ Лонг │ 20X" ref full row w=983 → Bold 88 (matches)
    #   "+224.11%" ref w=924 → Bold 215 (w=916)
    #   "Цена закрытия" ref w=439 → Reg 66 (w=439 exact)
    #   "0.38763" ref w=241 → Bold 62 (w=243)
    f_header   = _load_font(fp_b, 68)    # ref w=614 (got w=610, close)
    f_meta     = _load_font(fp_b, 76)    # ref SKYAIUSDT w=405 (Bold 76 → w=403)
    f_pnl      = _load_font(fp_b, 215)   # ref w=924
    f_label    = _load_font(fp_r, 66)    # ref w=439 (exact)
    f_value    = _load_font(fp_b, 62)    # ref w=241 (got w=243, close)
    f_username = _load_font(fp_b, 56)    # ref h=49
    f_date     = _load_font(fp_r, 50)    # ref h=44
    f_ref_lbl  = _load_font(fp_r, 50)    # ref h=45
    f_ref_code = _load_font(fp_b, 56)    # ref h=43

    WHITE = (255, 255, 255)
    GREEN = (0, 200, 122)
    RED   = (255, 45, 120)
    GRAY  = (138, 147, 168)
    SEP   = (90, 95, 110)

    side = data.get("side", "long")
    side_color = GREEN if side == "long" else RED
    side_text  = "Лонг" if side == "long" else "Шорт"

    # Pixel-anchor map measured from BINGX_Custom_*.JPG references (1800x1800).
    # HEADER_Y is biased -5 because PIL's "lm" anchor at f_header=64 sits ~5px below
    # visual cap-height center; other fonts/sizes empirically align without bias.
    LEFT_X         = 93     # anchor x; PIL "lm" adds ~5px left bearing → visible text starts at x=98 matching ref
    HEADER_Y       = 499    # "Реализованная / Нереализованная П/У" (ref center 504)
    META_Y         = 642    # "SYMBOL │ Side │ Lev"
    PNL_Y          = 876    # big "+224.11%"
    PRICE_TOP_Y    = 1093   # "Цена закрытия / Последняя цена"
    PRICE_BOT_Y    = 1204   # "Цена входа"
    VALUE_GAP      = 65     # px between label end and value start
    USER_X         = 281    # "CHM_LAB" / "05-02" left edge (right of triangle)
    USER_Y         = 1591   # "CHM_LAB" center
    DATE_Y         = 1677   # "05-02" center
    REF_RIGHT_X    = 1519   # right edge of "Реферальный код" / "D1BFA4"
    REF_LBL_Y      = 1584
    REF_CODE_Y     = 1668

    # ----- Header: "Нереализованная / Реализованная П/У" (top-left) -----
    header_text = "Реализованная П/У" if data.get("status") == "closed" else "Нереализованная П/У"
    draw.text((LEFT_X, HEADER_Y), header_text, fill=WHITE, font=f_header, anchor="lm")

    # ----- Symbol │ Side │ Leverage row -----
    symbol  = str(data.get("symbol", "")).upper()
    lev_raw = str(data.get("leverage", "")).strip().lower().replace("x", "")
    lev_text = f"{lev_raw}X" if lev_raw else ""

    sep_gap = 40            # space on each side of separator
    sep_h = 70              # vertical line height
    sep_thick = 3           # line thickness

    def draw_sep(cx: int):
        draw.rectangle(
            [cx - sep_thick // 2, META_Y - sep_h // 2, cx + sep_thick // 2 + sep_thick % 2, META_Y + sep_h // 2],
            fill=SEP,
        )

    x = LEFT_X
    draw.text((x, META_Y), symbol, fill=WHITE, font=f_meta, anchor="lm")
    x += draw.textlength(symbol, font=f_meta) + sep_gap
    draw_sep(x); x += sep_gap
    draw.text((x, META_Y), side_text, fill=side_color, font=f_meta, anchor="lm")
    x += draw.textlength(side_text, font=f_meta) + sep_gap
    draw_sep(x); x += sep_gap
    if lev_text:
        draw.text((x, META_Y), lev_text, fill=WHITE, font=f_meta, anchor="lm")

    # ----- Huge PnL -----
    pnl_color = GREEN if pnl >= 0 else RED
    pnl_text  = f"{pnl:+.2f}%"
    draw.text((LEFT_X, PNL_Y), pnl_text, fill=pnl_color, font=f_pnl, anchor="lm")

    # ----- Price rows: "Последняя цена / Цена закрытия" + "Цена входа" -----
    label_top = "Последняя цена" if data.get("status", "open") != "closed" else "Цена закрытия"
    label_bot = "Цена входа"

    # BingX style: no thousands separator.
    def _fmt(v):
        return format_price(v).replace(",", "")

    # Draw labels (gray, left-aligned) and values (white, bold, with VALUE_GAP after label).
    draw.text((LEFT_X, PRICE_TOP_Y), label_top, fill=GRAY, font=f_label, anchor="lm")
    val_top_x = LEFT_X + int(draw.textlength(label_top, font=f_label)) + VALUE_GAP
    draw.text((val_top_x, PRICE_TOP_Y), _fmt(data.get("exit", 0)), fill=WHITE, font=f_value, anchor="lm")

    draw.text((LEFT_X, PRICE_BOT_Y), label_bot, fill=GRAY, font=f_label, anchor="lm")
    val_bot_x = LEFT_X + int(draw.textlength(label_bot, font=f_label)) + VALUE_GAP
    draw.text((val_bot_x, PRICE_BOT_Y), _fmt(data.get("entry", 0)), fill=WHITE, font=f_value, anchor="lm")

    # ----- Username + date (under triangle on bottom-left) -----
    # If user opted out of the username field, also wipe the baked avatar
    # triangle (x≈94..239) so the bottom-left area is fully blank.
    username = str(data.get("username", "")).strip()
    datetime_text = str(data.get("datetime_str", "")).strip()
    if username:
        draw.text((USER_X, USER_Y), username, fill=WHITE, font=f_username, anchor="lm")
    else:
        # Wipe BAKED triangle avatar (x≈85..245, y≈1555..1715) plus the
        # username text slot. Date column (x≈260+) untouched.
        try:
            avatar_bg = img.getpixel((30, 1500))
        except Exception:
            avatar_bg = (10, 10, 10)
        draw.rectangle([60, 1545, 260, 1720], fill=avatar_bg)
    if datetime_text:
        draw.text((USER_X, DATE_Y), datetime_text, fill=GRAY, font=f_date, anchor="lm")

    # ----- Referral label + code (bottom-right, left of QR) -----
    referral_code = str(data.get("referral", "")).strip()
    draw.text((REF_RIGHT_X, REF_LBL_Y), "Реферальный код", fill=GRAY, font=f_ref_lbl, anchor="rm")
    if referral_code:
        draw.text((REF_RIGHT_X, REF_CODE_Y), referral_code, fill=WHITE, font=f_ref_code, anchor="rm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), "custom_bingx_")
    return output_path

# =====================================================
# CUSTOM EXCHANGE (FSM)
# =====================================================
@dp.callback_query(F.data == "custom_bybit")
async def start_custom_bybit(cb: CallbackQuery, state: FSMContext):
    has_access, _ = check_access(cb.from_user.id)
    if not has_access:
        await cb.answer("🔒 Доступ закрыт. Нажми /start и оформи доступ.", show_alert=True)
        return

    await state.clear()
    await state.update_data(exchange="bybit", _user_id=cb.from_user.id)
    msg = await cb.message.answer(
        "👤 Введите имя пользователя (или пропустите, чтобы оставить пустым):",
        reply_markup=skip_kb,
    )
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchange.username)


@dp.callback_query(F.data == "custom_bingx")
async def start_custom_bingx(cb: CallbackQuery, state: FSMContext):
    has_access, _ = check_access(cb.from_user.id)
    if not has_access:
        await cb.answer("🔒 Доступ закрыт. Нажми /start и оформи доступ.", show_alert=True)
        return

    await state.clear()
    await state.update_data(exchange="bingx", _user_id=cb.from_user.id)
    msg = await cb.message.answer(
        "🎨 Выбери шаблон:",
        reply_markup=bingx_template_kb,
    )
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchange.template)


@dp.callback_query(CustomExchange.template, F.data.startswith("bingx_tpl:"))
async def custom_bingx_template(call: CallbackQuery, state: FSMContext):
    template = call.data.split(":", 1)[1]
    if template not in BINGX_TEMPLATES:
        await call.answer("❌ Неизвестный шаблон")
        return
    await state.update_data(template=template)
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    msg = await call.message.answer(
        f"Шаблон: {BINGX_TEMPLATE_LABELS[template]}\n👤 Введите имя пользователя (или пропустите, чтобы оставить пустым):",
        reply_markup=skip_kb,
    )
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchange.username)


@dp.callback_query(F.data == "custom_bybit_usdt")
async def start_custom_bybit_usdt(cb: CallbackQuery, state: FSMContext):
    has_access, _ = check_access(cb.from_user.id)
    if not has_access:
        await cb.answer("🔒 Доступ закрыт. Нажми /start и оформи доступ.", show_alert=True)
        return

    await state.clear()
    await state.update_data(_user_id=cb.from_user.id)
    msg = await cb.message.answer("👤 Введите имя пользователя:")
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchangeUSDT.username)


@dp.message(CustomExchangeUSDT.username)
async def cusdt_username(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) > 50:
        await msg.answer("Имя слишком длинное (макс. 50 символов)")
        return
    await state.update_data(username=text)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\n📈 Выбери направление сделки:", reply_markup=side_kb)
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.side)


@dp.callback_query(CustomExchangeUSDT.side)
async def cusdt_side(call: CallbackQuery, state: FSMContext):
    if call.data == "side_long":
        side = "long"
    elif call.data == "side_short":
        side = "short"
    else:
        await call.answer("❌ Ошибка кнопки")
        return
    await state.update_data(side=side)
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    data = await state.get_data()
    new = await call.message.answer(f"{build_custom_summary(data)}\n🪙 Торговая пара (например BTCUSDT):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.symbol)


@dp.message(CustomExchangeUSDT.symbol)
async def cusdt_symbol(msg: Message, state: FSMContext):
    await state.update_data(symbol=msg.text.upper())
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nЦена входа (например 123456.12):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.entry)


@dp.message(CustomExchangeUSDT.entry)
async def cusdt_entry(msg: Message, state: FSMContext):
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(entry=value)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nЦена выхода (например 123456.12):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.exit_price)


@dp.message(CustomExchangeUSDT.exit_price)
async def cusdt_exit(msg: Message, state: FSMContext):
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(exit=value)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nПлечо (например 20):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.leverage)


@dp.message(CustomExchangeUSDT.leverage)
async def cusdt_leverage(msg: Message, state: FSMContext):
    text = msg.text.strip().lower().replace("x", "")
    try:
        lev_val = float(text)
        if lev_val < 1 or lev_val > 200:
            raise ValueError
    except (ValueError, AttributeError):
        await msg.answer("Введите плечо от 1 до 200")
        return
    await state.update_data(leverage=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nРазмер депозита в USD (например 1000):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchangeUSDT.deposit)


@dp.message(CustomExchangeUSDT.deposit)
async def cusdt_finish(msg: Message, state: FSMContext):
    deposit_val = await parse_float(msg)
    if deposit_val is None:
        return
    await state.update_data(deposit=deposit_val)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")

    entry = data["entry"]
    exit_price = data["exit"]
    side = data["side"]
    leverage_raw = str(data.get("leverage") or "1").strip().lower().replace("x", "")
    try:
        leverage = float(leverage_raw) if leverage_raw else 1.0
    except ValueError:
        leverage = 1.0

    _, _, net_pct = compute_pnl_breakdown(entry, exit_price, leverage, side, "bybit")
    pnl_usdt = net_pct / 100 * deposit_val

    image_data = {
        "username": data["username"],
        "symbol": data["symbol"],
        "pnl_usdt": round(pnl_usdt, 2),
        "entry": entry,
        "exit": exit_price,
        "leverage": f"{leverage:.1f}x",
        "side": side,
    }

    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_usdt_image, image_data)
        uid = (await state.get_data()).get("_user_id")
        if uid:
            path = await loop.run_in_executor(_THREAD_POOL, _apply_user_logo, path, uid)
        await msg.answer_photo(FSInputFile(path), reply_markup=restart_kb)
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await msg.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)
    await state.clear()


@dp.callback_query(CustomExchange.username, F.data == "skip_field")
async def skip_username(call: CallbackQuery, state: FSMContext):
    await state.update_data(username="")
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    data = await state.get_data()
    new = await call.message.answer(
        f"{build_custom_summary(data)}\n📈 Выбери направление сделки:", reply_markup=side_kb
    )
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.side)


@dp.message(CustomExchange.username)
async def custom_username(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) > 50:
        await msg.answer("Имя слишком длинное (макс. 50 символов)")
        return
    await state.update_data(username=text)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(
        f"{build_custom_summary(data)}\n📈 Выбери направление сделки:", reply_markup=side_kb
    )
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.side)

@dp.callback_query(CustomExchange.side)
async def custom_side(call: CallbackQuery, state: FSMContext):
    if call.data == "side_long":
        side = "long"
    elif call.data == "side_short":
        side = "short"
    else:
        await call.answer("❌ Ошибка кнопки")
        return
    await state.update_data(side=side)
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    data = await state.get_data()
    # BingX cards have a header that depends on whether the position is still open
    # (Нереализованная П/У) or closed (Реализованная П/У). Ask only for bingx.
    if data.get("exchange") == "bingx":
        new = await call.message.answer(
            f"{build_custom_summary(data)}\n📊 Тип позиции:",
            reply_markup=bingx_status_kb,
        )
        await state.update_data(custom_last_msg_id=new.message_id)
        await state.set_state(CustomExchange.status)
        return
    new = await call.message.answer(f"{build_custom_summary(data)}\n🪙 Торговая пара (например BTCUSDT):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.symbol)


@dp.callback_query(CustomExchange.status, F.data.startswith("bingx_st:"))
async def custom_bingx_status(call: CallbackQuery, state: FSMContext):
    status = call.data.split(":", 1)[1]
    if status not in ("open", "closed"):
        await call.answer("❌ Неизвестный тип")
        return
    await state.update_data(status=status)
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    data = await state.get_data()
    new = await call.message.answer(f"{build_custom_summary(data)}\n🪙 Торговая пара (например BTCUSDT):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.symbol)

@dp.message(CustomExchange.symbol)
async def custom_symbol(msg: Message, state: FSMContext):
    await state.update_data(symbol=msg.text.upper())
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nЦена входа (например 123456.12):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.entry)

@dp.message(CustomExchange.entry)
async def custom_entry(msg: Message, state: FSMContext):
    raw_text = msg.text.strip().replace(",", ".") if msg.text else ""
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(entry=value, entry_str=raw_text)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    if data.get("exchange") == "bingx" and data.get("status") == "open":
        prompt = "Последняя цена (например 123456.12):"
    elif data.get("exchange") == "bingx" and data.get("status") == "closed":
        prompt = "Цена закрытия (например 123456.12):"
    else:
        prompt = "Цена выхода (например 123456.12):"
    new = await msg.answer(f"{build_custom_summary(data)}\n{prompt}")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.exit_price)

@dp.message(CustomExchange.exit_price)
async def custom_exit(msg: Message, state: FSMContext):
    raw_text = msg.text.strip().replace(",", ".") if msg.text else ""
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(exit=value, exit_str=raw_text)
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(f"{build_custom_summary(data)}\nПлечо (например 20):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.leverage)

@dp.message(CustomExchange.leverage)
async def custom_leverage(msg: Message, state: FSMContext):
    text = msg.text.strip().lower().replace("x", "")
    try:
        lev_val = float(text)
        if lev_val < 1 or lev_val > 200:
            raise ValueError
    except (ValueError, AttributeError):
        await msg.answer("Введите плечо от 1 до 200")
        return
    await state.update_data(leverage=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer(
        f"{build_custom_summary(data)}\nВведите реферальный код (например D1BFA4):",
        reply_markup=skip_kb,
    )
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.referral)

@dp.callback_query(CustomExchange.referral, F.data == "skip_field")
async def skip_referral(call: CallbackQuery, state: FSMContext):
    await state.update_data(referral="")
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    new = await call.message.answer("Введите дату и время (например 14/02 19:00):", reply_markup=skip_kb)
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.datetime_str)

@dp.message(CustomExchange.referral)
async def custom_referral(msg: Message, state: FSMContext):
    await state.update_data(referral=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    new = await msg.answer("Введите дату и время (например 02/14 19:00):", reply_markup=skip_kb)
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.datetime_str)

@dp.callback_query(CustomExchange.datetime_str, F.data == "skip_field")
async def skip_datetime(call: CallbackQuery, state: FSMContext):
    await state.update_data(datetime_str="")
    await call.answer()
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Non-critical error: {e}")
    await custom_finish(call.message, state)

@dp.message(CustomExchange.datetime_str)
async def custom_finish(msg: Message, state: FSMContext):
    text_input = getattr(msg, "text", None)
    if text_input:
        await state.update_data(datetime_str=text_input.strip())
        await safe_delete_message(msg)
    data = await state.get_data()
    exchange = data.get("exchange", "bybit")
    entry = data["entry"]
    exit_price = data["exit"]
    side = data["side"]
    leverage_raw = str(data.get("leverage") or "1").strip().lower().replace("x", "")
    try:
        leverage = float(leverage_raw) if leverage_raw else 1.0
    except ValueError:
        leverage = 1.0
    _, _, net_pct = compute_pnl_breakdown(entry, exit_price, leverage, side, exchange)
    image_data = {
        "username": data["username"],
        "symbol": data["symbol"],
        "pnl": round(net_pct, 2),
        "entry": entry,
        "exit": exit_price,
        "entry_str": data.get("entry_str", ""),
        "exit_str": data.get("exit_str", ""),
        "referral": data.get("referral", ""),
        "side": side,
    }
    loop = asyncio.get_event_loop()
    if exchange == "bingx":
        image_data["leverage"] = data["leverage"]
        image_data["datetime_str"] = data.get("datetime_str", "")
        image_data["template"] = data.get("template", "football")
        image_data["status"] = data.get("status", "open")
        gen_func = generate_custom_bingx_image
    else:
        image_data["leverage"] = f"{leverage:.1f}x"
        gen_func = generate_custom_bybit_image

    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception as e:
            logger.debug(f"Non-critical error: {e}")
    try:
        path = await loop.run_in_executor(_THREAD_POOL, gen_func, image_data)
        uid = data.get("_user_id")
        if uid:
            path = await loop.run_in_executor(_THREAD_POOL, _apply_user_logo, path, uid)
        await msg.answer_photo(FSInputFile(path), reply_markup=restart_kb)
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await msg.answer("Ошибка генерации изображения. Попробуйте снова.", reply_markup=restart_kb)
    await state.clear()

# =====================================================
# REFERRAL COMMANDS
# =====================================================
@dp.message(Command("referral"))
async def cmd_referral(message: Message):
    user_id = message.from_user.id
    code = get_referral_code(user_id)
    count, needed = get_referral_stats(user_id)
    text = (
        f"🎁 Твой реферальный код: `{code}`\n\n"
        f"Приглашено: {count}/3\n"
    )
    if needed > 0:
        text += f"Ещё {needed} приглашений = 7 дней бесплатно!\n"
    else:
        text += "✅ Награда получена!\n"
    text += "\nПоделись кодом с друзьями."
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("ref"))
async def cmd_use_ref(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /ref REF123456")
        return
    code = parts[1].strip().upper()
    success, msg = use_referral(message.from_user.id, code)
    await message.answer(f"{'✅' if success else '❌'} {msg}")

# =====================================================
# LANGUAGE TOGGLE
# =====================================================
@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    current = get_lang(message.from_user.id)
    new_lang = "en" if current == "ru" else "ru"
    set_lang(message.from_user.id, new_lang)
    if new_lang == "en":
        await message.answer("🇬🇧 Language switched to English")
    else:
        await message.answer("🇷🇺 Язык переключён на Русский")


# =====================================================
# USER PROFILE — saved defaults that pre-fill signal flow
# =====================================================
def _profile_kb(has_channel: bool = False, has_logo: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Имя",      callback_data="profile_edit:username"),
         InlineKeyboardButton(text="✏️ Реф.код",  callback_data="profile_edit:referral")],
        [InlineKeyboardButton(text="🟡 Bybit",    callback_data="profile_set:exchange:bybit"),
         InlineKeyboardButton(text="🟢 BingX",    callback_data="profile_set:exchange:bingx")],
        [InlineKeyboardButton(text="📢 Канал",    callback_data="profile_edit:channel")],
        [InlineKeyboardButton(text="🖼 Лого",     callback_data="profile_set_logo")],
    ]
    if has_channel:
        rows[-2].append(InlineKeyboardButton(text="🚫 Откл. канал",
                                              callback_data="profile_clear_channel"))
    if has_logo:
        rows[-1].append(InlineKeyboardButton(text="🚫 Убрать лого",
                                              callback_data="profile_clear_logo"))
    rows.append([InlineKeyboardButton(text="🗑 Сбросить", callback_data="profile_clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _profile_text(p: dict, has_logo: bool = False) -> str:
    chan = p.get("channel")
    chan_line = f"Канал: <code>{chan}</code>\n" if chan else ""
    logo_line = f"Лого:  {'✅ установлено' if has_logo else '—'}\n"
    return (
        "👤 <b>Твой профиль</b>\n\n"
        f"Имя:   <code>{p.get('username') or '—'}</code>\n"
        f"Реф:   <code>{p.get('referral') or '—'}</code>\n"
        f"Биржа: <code>{p.get('exchange') or '—'}</code>\n"
        f"{chan_line}"
        f"{logo_line}"
        "\n"
        "Эти значения подставляются автоматически в «⚡ Быстрый скрин» — "
        "соответствующие шаги пропускаются. Меняй их в превью или здесь.\n"
        "Канал — если задан, бот публикует туда финальную карточку после ✅ Отправить.\n"
        "Лого — если задано, накладывается водяным знаком в правом нижнем углу карточки."
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    p = get_profile(message.from_user.id)
    has_logo = has_user_logo(message.from_user.id)
    await message.answer(_profile_text(p, has_logo), parse_mode="HTML",
                         reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


@dp.callback_query(F.data == "profile_show")
async def cb_profile_show(call: CallbackQuery, state: FSMContext):
    await call.answer()
    p = get_profile(call.from_user.id)
    has_logo = has_user_logo(call.from_user.id)
    await call.message.answer(_profile_text(p, has_logo), parse_mode="HTML",
                              reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


@dp.callback_query(F.data == "profile_clear")
async def cb_profile_clear(call: CallbackQuery, state: FSMContext):
    clear_profile(call.from_user.id)
    await call.answer("✓ Профиль сброшен")
    try: await call.message.delete()
    except Exception: pass


@dp.callback_query(F.data.startswith("profile_set:"))
async def cb_profile_set(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer("bad payload", show_alert=True); return
    _, field, value = parts
    update_profile(call.from_user.id, **{field: value})
    await call.answer(f"✓ {field} = {value}")
    try: await call.message.delete()
    except Exception: pass
    p = get_profile(call.from_user.id)
    has_logo = has_user_logo(call.from_user.id)
    await call.message.answer(_profile_text(p, has_logo), parse_mode="HTML",
                              reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


@dp.callback_query(F.data.startswith("profile_edit:"))
async def cb_profile_edit(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":", 1)[1]
    if field not in ("username", "referral", "channel"):
        await call.answer("Это поле меняется кнопкой биржи.", show_alert=True); return
    await state.update_data(_profile_editing_field=field)
    await call.answer()
    prompts = {
        "username": "Введите имя пользователя (или /clear чтобы очистить):",
        "referral": "Введите реферальный код (или /clear чтобы очистить):",
        "channel":  ("Пришли @username канала (или -100… для приватного).\n"
                     "Бот должен быть админом этого канала с правом постить.\n"
                     "/clear — отвязать канал."),
    }
    await call.message.answer(prompts[field])
    await state.set_state(ProfileForm.edit_value)


@dp.callback_query(F.data == "profile_clear_channel")
async def cb_profile_clear_channel(call: CallbackQuery, state: FSMContext):
    update_profile(call.from_user.id, channel="")
    await call.answer("✓ Канал отключён")
    try: await call.message.delete()
    except Exception: pass
    p = get_profile(call.from_user.id)
    has_logo = has_user_logo(call.from_user.id)
    await call.message.answer(_profile_text(p, has_logo), parse_mode="HTML",
                              reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


@dp.callback_query(F.data == "profile_set_logo")
async def cb_profile_set_logo(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(
        "🖼 Пришли картинку (фото или файл-PNG), которая станет твоим водяным "
        "знаком в правом нижнем углу карточек. Прозрачный PNG-фон выглядит "
        "лучше всего.\n\n/cancel — отменить."
    )
    await state.set_state(ProfileForm.set_logo)


@dp.callback_query(F.data == "profile_clear_logo")
async def cb_profile_clear_logo(call: CallbackQuery, state: FSMContext):
    clear_user_logo(call.from_user.id)
    await call.answer("✓ Лого убрано")
    try: await call.message.delete()
    except Exception: pass
    p = get_profile(call.from_user.id)
    has_logo = has_user_logo(call.from_user.id)
    await call.message.answer(_profile_text(p, has_logo), parse_mode="HTML",
                              reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


@dp.message(ProfileForm.set_logo, F.text == "/cancel")
async def msg_profile_set_logo_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.")


@dp.message(ProfileForm.set_logo)
async def msg_profile_set_logo(msg: Message, state: FSMContext):
    """Accepts a photo or an image-document and saves it as the user's logo PNG."""
    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id   # largest variant
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        await msg.answer("Нужно прислать картинку (фото или image-файл). /cancel — отменить.")
        return
    try:
        f = await bot.get_file(file_id)
        out_path = os.path.join(LOGO_DIR, f"{msg.from_user.id}.png")
        # Download into a tmp file, normalise via PIL → PNG so any input format works.
        tmp = f"/tmp/logo_dl_{msg.from_user.id}_{uuid.uuid4().hex}.bin"
        await bot.download_file(f.file_path, tmp)
        img = Image.open(tmp).convert("RGBA")
        img.save(out_path, "PNG", optimize=True)
        try: os.remove(tmp)
        except OSError: pass
    except Exception as e:
        logger.warning(f"Logo upload failed for user {msg.from_user.id}: {e}")
        await msg.answer(f"❌ Не получилось сохранить картинку: {e}")
        return
    await state.clear()
    await msg.answer("✅ Лого сохранено. Теперь оно будет появляться на всех твоих карточках.")
    p = get_profile(msg.from_user.id)
    has_logo = has_user_logo(msg.from_user.id)
    await msg.answer(_profile_text(p, has_logo), parse_mode="HTML",
                     reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


async def _validate_user_channel(channel: str) -> tuple[bool, str]:
    """Try to resolve the channel and check that the bot is admin there.
    Returns (ok, human_message)."""
    try:
        chat = await bot.get_chat(channel)
    except Exception as e:
        return False, f"Не нашёл канал: {e}"
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        return False, f"Не смог проверить статус бота в канале: {e}"
    if member.status not in ("administrator", "creator"):
        return False, "Бот должен быть админом канала с правом постить сообщения."
    return True, f"✓ Канал «{chat.title}» подключён"


@dp.message(ProfileForm.edit_value)
async def msg_profile_edit_value(msg: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("_profile_editing_field")
    if not field:
        await state.clear(); return
    txt = (msg.text or "").strip()
    if txt == "/clear":
        update_profile(msg.from_user.id, **{field: ""})
        await msg.answer(f"✓ {_field_title(field)} очищено")
    elif field == "channel":
        # Validate first — if it fails we don't save.
        ok, message_text = await _validate_user_channel(txt)
        if not ok:
            await msg.answer(f"❌ {message_text}\n\nПопробуй ещё раз или /clear.")
            return  # keep the same state so user can retry
        update_profile(msg.from_user.id, channel=txt[:64])
        await msg.answer(message_text)
    else:
        update_profile(msg.from_user.id, **{field: txt[:50]})
        await msg.answer(f"✓ {_field_title(field)} = <code>{txt[:50]}</code>", parse_mode="HTML")
    await state.clear()
    p = get_profile(msg.from_user.id)
    has_logo = has_user_logo(msg.from_user.id)
    await msg.answer(_profile_text(p, has_logo), parse_mode="HTML",
                     reply_markup=_profile_kb(bool(p.get("channel")), has_logo))


# =====================================================
# SIGNAL HISTORY — last N rendered cards, "repeat" loads draft into preview
# =====================================================
def _history_label(sig: dict, idx: int) -> str:
    sym  = sig.get("symbol", "?")
    side = "Лонг" if sig.get("side") == "long" else "Шорт"
    lev  = sig.get("leverage", "—")
    try:
        entry = float(sig["entry"])
        exit_price = float(sig["exit"])
        lev_n = float(str(sig.get("leverage", "1x")).lower().replace("x", ""))
        pnl = ((exit_price - entry) / entry * 100) * lev_n if sig.get("side") == "long" \
              else ((entry - exit_price) / entry * 100) * lev_n
        return f"{idx+1}. {sym} {side} {lev} {pnl:+.1f}%"
    except Exception:
        return f"{idx+1}. {sym} {side} {lev}"


def _history_kb(items: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=_history_label(s, i),
                                   callback_data=f"hist_use:{i}")] for i, s in enumerate(items)]
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="hist_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    items = get_history(message.from_user.id)
    if not items:
        await message.answer("📭 История пуста — сначала создай хотя бы один скрин.")
        return
    await message.answer("🕘 <b>Последние сигналы</b>\nТапни запись чтобы открыть в превью и поправить:",
                         parse_mode="HTML", reply_markup=_history_kb(items))


@dp.callback_query(F.data == "history_show")
async def cb_history_show(call: CallbackQuery, state: FSMContext):
    items = get_history(call.from_user.id)
    await call.answer()
    if not items:
        await call.message.answer("📭 История пуста — сначала создай хотя бы один скрин.")
        return
    await call.message.answer("🕘 <b>Последние сигналы</b>\nТапни запись чтобы открыть в превью и поправить:",
                              parse_mode="HTML", reply_markup=_history_kb(items))


@dp.callback_query(F.data == "hist_close")
async def cb_history_close(call: CallbackQuery, state: FSMContext):
    await call.answer()
    try: await call.message.delete()
    except Exception: pass


@dp.callback_query(F.data.startswith("hist_use:"))
async def cb_history_use(call: CallbackQuery, state: FSMContext):
    has_access, _ = check_access(call.from_user.id)
    if not has_access:
        can_use, _ = check_daily_limit(call.from_user.id)
        if not can_use:
            await call.answer("🔒 Лимит 3 бесплатных скрина в день исчерпан.", show_alert=True)
            return
    try: idx = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("bad index", show_alert=True); return
    items = get_history(call.from_user.id)
    if idx < 0 or idx >= len(items):
        await call.answer("Запись не найдена", show_alert=True); return
    sig = {k: v for k, v in items[idx].items() if not str(k).startswith("_")}
    await state.clear()
    await state.update_data(_sig=sig)
    await call.answer("Загружено в превью")
    try: await call.message.delete()
    except Exception: pass
    await _render_preview(call.message, state)


# =====================================================
# SERIES SUMMARY — aggregate last N closed trades into a single share-card
# =====================================================
def _series_pnl_pct(sig: dict) -> float | None:
    """Reconstruct the same NET ROI the user saw on each card.
    Returns None if any field is missing/unparseable."""
    try:
        entry = float(sig["entry"]); exit_price = float(sig["exit"])
        lev = float(str(sig.get("leverage", "1x")).lower().replace("x", ""))
        side = sig.get("side", "long")
        exch = sig.get("exchange", "bybit")
        _, _, net = compute_pnl_breakdown(entry, exit_price, lev, side, exch)
        return net
    except (KeyError, ValueError, ZeroDivisionError, TypeError):
        return None


def _series_stats(items: list) -> dict:
    """Aggregate stats over a list of saved signals.
    Sum of percent ROIs is dimensionally OK because each came from the same
    isolated-margin model — it represents `total_pnl / single_position_margin`,
    which is what users mean when they say "за неделю +185%"."""
    rows: list[dict] = []
    wins = 0; losses = 0; total_pct = 0.0
    for sig in items:
        pct = _series_pnl_pct(sig)
        if pct is None: continue
        rows.append({
            "symbol": sig.get("symbol", "?"),
            "side":   sig.get("side", "long"),
            "lev":    sig.get("leverage", "—"),
            "pnl":    round(pct, 2),
        })
        if pct > 0: wins += 1
        elif pct < 0: losses += 1
        total_pct += pct
    n = len(rows)
    return {
        "rows":      rows,
        "n":         n,
        "wins":      wins,
        "losses":    losses,
        "win_rate":  round(wins / n * 100, 1) if n else 0.0,
        "total":     round(total_pct, 2),
    }


def generate_series_image(stats: dict, username: str = "") -> str:
    """Render a 1080x1080 share-card summarising the user's last N trades.
    Layout: dark canvas → title → stats triplet (count / win-rate / Σ-ROI)
    → list of trade rows (max 7) → footer."""
    W, H = 1080, 1080
    BG       = (21, 21, 30)
    FG       = (235, 236, 240)
    DIM      = (140, 142, 155)
    GREEN    = (5, 196, 109)
    RED      = (255, 64, 78)
    DIVIDER  = (44, 46, 60)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    bold = lambda s: _load_font(os.path.join(BASE_DIR, "fonts/SF_Pro_Display_Semibold.otf"), s)
    reg  = lambda s: _load_font(os.path.join(BASE_DIR, "fonts/SF_Pro_Display_Regular.otf"), s)

    # Header
    draw.text((W // 2, 90), "СВОДКА", fill=FG, font=bold(72), anchor="mm")
    if username:
        draw.text((W // 2, 165), f"@{username.lstrip('@')}",
                  fill=DIM, font=reg(36), anchor="mm")

    # Stats triplet
    y_stats = 270
    third = W // 3
    cells = [
        ("СДЕЛОК",   f"{stats['n']}",                       FG),
        ("WIN-RATE", f"{stats['win_rate']:.0f}%",           FG),
        ("ИТОГО",    f"{stats['total']:+.2f}%",             GREEN if stats['total'] >= 0 else RED),
    ]
    for i, (label, value, colour) in enumerate(cells):
        cx = third * i + third // 2
        draw.text((cx, y_stats),       label, fill=DIM, font=reg(30),  anchor="mm")
        draw.text((cx, y_stats + 70),  value, fill=colour, font=bold(64), anchor="mm")

    # Divider
    draw.rectangle([(60, 410), (W - 60, 412)], fill=DIVIDER)

    # Trade list
    rows = stats["rows"][:7]
    row_h = 90
    y0 = 460
    for i, r in enumerate(rows):
        y = y0 + i * row_h
        side_ru = "Лонг" if r["side"] == "long" else "Шорт"
        side_col = GREEN if r["side"] == "long" else RED
        pnl_col  = GREEN if r["pnl"] >= 0 else RED
        # Symbol
        draw.text((100, y), r["symbol"], fill=FG, font=bold(44), anchor="lm")
        # Side
        draw.text((420, y), side_ru, fill=side_col, font=reg(36), anchor="lm")
        # Leverage
        draw.text((600, y), str(r["lev"]), fill=DIM, font=reg(34), anchor="lm")
        # PnL right-aligned
        draw.text((W - 100, y), f"{r['pnl']:+.2f}%", fill=pnl_col, font=bold(48), anchor="rm")

    # Footer
    if not rows:
        draw.text((W // 2, H // 2 + 60),
                  "Нет закрытых сделок в истории.",
                  fill=DIM, font=reg(34), anchor="mm")
    else:
        draw.text((W // 2, H - 60),
                  f"последние {len(rows)} сделок",
                  fill=DIM, font=reg(28), anchor="mm")

    out = f"/tmp/series_{uuid.uuid4().hex}.png"
    img.save(out, "PNG")
    return out


@dp.message(Command("series"))
async def cmd_series(message: Message):
    items = get_history(message.from_user.id)
    if not items:
        await message.answer("📭 История пуста — сначала создай хотя бы один скрин.")
        return
    stats = _series_stats(items)
    if stats["n"] == 0:
        await message.answer("Не смог посчитать ни одной сделки из истории — данные битые.")
        return
    profile = get_profile(message.from_user.id)
    username = profile.get("username", "")
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(_THREAD_POOL, generate_series_image, stats, username)
        path = await loop.run_in_executor(_THREAD_POOL, _apply_user_logo, path, message.from_user.id)
    except Exception as e:
        logger.error(f"Series render error: {e}")
        await message.answer("Ошибка генерации сводной карточки.")
        return
    summary = (
        f"📊 <b>Сводка</b>: {stats['n']} сделок, "
        f"win-rate {stats['win_rate']:.0f}%, "
        f"суммарно <b>{stats['total']:+.2f}%</b>"
    )
    await message.answer_photo(FSInputFile(path), caption=summary, parse_mode="HTML")


@dp.callback_query(F.data == "series_show")
async def cb_series_show(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await cmd_series(call.message)


# =====================================================
# INLINE MODE
# =====================================================
@dp.message(F.text & ~F.text.startswith("/"))
async def signal_auto_detect(msg: Message, state: FSMContext):
    """Auto-detect a free-form trade signal in any text message that isn't part
    of an active FSM flow and isn't a command."""
    if await state.get_state() is not None:
        return  # Inside an active FSM — let dedicated handlers deal with it
    text = msg.text or ""
    if looks_like_signal(text):
        await _handle_signal_text(msg, state, text)
        return
    # The text wasn't a clean signal but the user might have tried — give a
    # specific hint if at least one signal-like marker is present (a side
    # keyword or a $TICKER), instead of silently swallowing the message.
    parsed = parse_signal(text)
    if parsed.get("side") or parsed.get("symbol"):
        missing = []
        if not parsed.get("symbol"): missing.append("символ")
        if not parsed.get("side"):   missing.append("сторона (LONG/SHORT)")
        if parsed.get("entry") is None and not parsed.get("tps"):
            missing.append("цены")
        await msg.answer(
            "Похоже на сигнал, но не нашёл: <b>" + ", ".join(missing) + "</b>.\n\n"
            "Минимум: <code>BTC LONG 78000</code>\n"
            "Лучше:\n"
            "<code>BTC LONG\nВход 78000\nСтоп 79000\nTP1 76000</code>",
            parse_mode="HTML",
        )


@dp.inline_query()
async def inline_pnl(query: InlineQuery):
    """Inline mode: @bot BTCUSDT +150% 50x"""
    text = query.query.strip()
    if not text:
        return
    parts = text.split()
    if len(parts) < 2:
        return
    symbol = parts[0].upper()
    try:
        pnl = float(parts[1].replace("%", "").replace("+", ""))
    except ValueError:
        return
    leverage = 10
    if len(parts) >= 3:
        try:
            leverage = int(parts[2].replace("x", "").replace("X", ""))
        except ValueError:
            pass
    side = "long" if pnl >= 0 else "short"
    result_text = f"📊 {symbol} | {'🟢 Лонг' if side == 'long' else '🔴 Шорт'} {leverage}x\n💰 ROI: {pnl:+.2f}%"
    results = [
        InlineQueryResultArticle(
            id="1",
            title=f"{symbol} {pnl:+.2f}% ({leverage}x)",
            description=f"{'Лонг' if side == 'long' else 'Шорт'} | Нажми чтобы отправить",
            input_message_content=InputTextMessageContent(
                message_text=result_text,
            ),
        )
    ]
    await query.answer(results, cache_time=5)

# =====================================================
# ЗАПУСК
# =====================================================
async def on_startup():
    await get_http_session()
    # Register the slash-command menu (the "/" picker next to text input).
    try:
        await bot.set_my_commands([
            BotCommand(command="start",    description="Главное меню"),
            BotCommand(command="menu",     description="Главное меню"),
            BotCommand(command="signal",   description="Быстрый скрин из текста"),
            BotCommand(command="series",   description="Сводка по последним сделкам"),
            BotCommand(command="history",  description="История последних карточек"),
            BotCommand(command="profile",  description="Мой профиль"),
            BotCommand(command="referral", description="Мой реферальный код"),
        ])
        # Replace the paperclip-area button with a "Menu" button that opens the
        # commands list directly — saves the user from typing "/".
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning(f"Failed to set bot commands menu: {e}")

async def on_shutdown():
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
    _THREAD_POOL.shutdown(wait=False)

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
