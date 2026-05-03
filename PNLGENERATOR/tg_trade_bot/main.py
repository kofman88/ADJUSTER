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
from access import activate_trial, grant_access, revoke_access, check_access, days_left, check_daily_limit, increment_usage, get_referral_code, get_referral_stats, use_referral
from i18n import get_lang, set_lang

ADMIN_ID = int(os.getenv("ADMIN_ID", "445677777"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@kofman88")

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
        kb.button(text="📊 Bybit", callback_data="exchange_bybit")
        kb.button(text="📊 BingX", callback_data="exchange_bingx")
        kb.button(text="🎨 Кастом Bybit", callback_data="custom_bybit")
        kb.button(text="💵 Кастом Bybit $", callback_data="custom_bybit_usdt")
        kb.button(text="🎨 Кастом BingX", callback_data="custom_bingx")
        kb.button(text="🏁 Марафон", callback_data="marathon:menu")
        kb.adjust(1)
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


@dp.message(Command("test_all"))
async def test_all(message: Message):
    text = (
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
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(entry, mark, qty, side, leverage)
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

    if side == "long":
        pnl_percent = ((exit_price - entry) / entry * 100) * leverage
    else:
        pnl_percent = ((entry - exit_price) / entry * 100) * leverage

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
        data["entry"], data["mark"], qty, data["side"], leverage
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
    entry: float, mark: float, qty: float, side: str, leverage: float
) -> tuple[float, float, float]:
    pnl_usd = qty * (mark - entry) if side == "long" else qty * (entry - mark)
    margin = (entry * qty / leverage) if leverage and entry else 0.0
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
    # Brand colours sampled from the JPGs:
    #   green = (15, 222, 140), red = (255, 50, 75) — overrides the constants above
    GREEN = (15, 222, 140)
    RED   = (255, 50, 75)

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
        # Reference label bbox: y=759-776, x≈62-260. Wipe the row and redraw.
        wipe(45, 752, 340, 783, *BG_STRIP_SUB)
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

    # Font sizes calibrated to text bounding boxes measured in BINGX_Custom_*.JPG
    f_header   = _load_font(fp_b, 64)    # ref h=57
    f_meta     = _load_font(fp_b, 80)    # ref h=58
    f_pnl      = _load_font(fp_b, 215)   # ref h=156
    f_label    = _load_font(fp_r, 50)    # ref h=45
    f_value    = _load_font(fp_b, 56)    # ref h=45 (value slightly bolder than label)
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
    LEFT_X         = 98     # all left-aligned texts share this x
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
    await state.update_data(exchange="bybit")
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
    await state.update_data(exchange="bingx")
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

    pnl_percent = (
        ((exit_price - entry) / entry * 100) * leverage
        if side == "long"
        else ((entry - exit_price) / entry * 100) * leverage
    )
    pnl_usdt = pnl_percent / 100 * deposit_val

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
    pnl_percent = (
        ((exit_price - entry) / entry * 100) * leverage
        if side == "long"
        else ((entry - exit_price) / entry * 100) * leverage
    )
    image_data = {
        "username": data["username"],
        "symbol": data["symbol"],
        "pnl": round(pnl_percent, 2),
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
# INLINE MODE
# =====================================================
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
