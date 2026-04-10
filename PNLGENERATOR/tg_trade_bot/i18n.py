"""Internationalization support for the bot."""

TEXTS = {
    "ru": {
        "welcome": "👋 Добро пожаловать в PNL Generator!\nВыберите действие:",
        "choose_exchange": "Выберите биржу:",
        "enter_symbol": "Введи монету (например BTCUSDT)",
        "choose_side": "Выбери направление 👇",
        "enter_entry": "Введите цену входа:",
        "enter_mark": "Введите цену маркировки:",
        "enter_amount": "На какую сумму заходишь? (USDT)",
        "enter_deposit": "Какой депозит? (USDT)",
        "enter_leverage": "Введите плечо (например 10)",
        "leverage_error": "Введите число от 1 до 125",
        "enter_number": "Введите число 🙏",
        "positive_number": "Число должно быть больше 0 🙏",
        "image_error": "Ошибка генерации изображения. Попробуйте снова.",
        "daily_limit": "🔒 Лимит {limit} бесплатных скринов в день исчерпан.\nНапиши {admin} для полного доступа.",
        "no_access": "🔒 Доступ закрыт. Нажми /start и оформи доступ.",
        "long": "Лонг",
        "short": "Шорт",
        "cross": "Кросс",
        "entry_price": "Цена входа",
        "mark_price": "Цена маркировки",
        "current_price": "Текущая цена",
        "exit_price": "Цена выхода",
        "close_price": "Цена закрытия",
        "liq_price": "Ожид. цена ликвид.",
        "position_size": "Размер позиции",
        "referral_code": "Реферальный код",
        "restart": "🔁 В начало",
        "back": "⬅️ Назад",
        "get_price": "📡 Взять цену с биржи",
        "skip": "⏭ Пропустить",
        "lang_btn": "🇬🇧 English",
    },
    "en": {
        "welcome": "👋 Welcome to PNL Generator!\nChoose an action:",
        "choose_exchange": "Choose exchange:",
        "enter_symbol": "Enter coin (e.g. BTCUSDT)",
        "choose_side": "Choose direction 👇",
        "enter_entry": "Enter entry price:",
        "enter_mark": "Enter mark price:",
        "enter_amount": "Enter position size (USDT)",
        "enter_deposit": "Enter deposit (USDT)",
        "enter_leverage": "Enter leverage (e.g. 10)",
        "leverage_error": "Enter a number from 1 to 125",
        "enter_number": "Enter a number 🙏",
        "positive_number": "Number must be greater than 0 🙏",
        "image_error": "Image generation error. Try again.",
        "daily_limit": "🔒 Daily limit of {limit} free screenshots reached.\nContact {admin} for full access.",
        "no_access": "🔒 Access denied. Press /start to get access.",
        "long": "Long",
        "short": "Short",
        "cross": "Cross",
        "entry_price": "Entry Price",
        "mark_price": "Mark Price",
        "current_price": "Current Price",
        "exit_price": "Exit Price",
        "close_price": "Close Price",
        "liq_price": "Est. Liq. Price",
        "position_size": "Position Size",
        "referral_code": "Referral Code",
        "restart": "🔁 Restart",
        "back": "⬅️ Back",
        "get_price": "📡 Get price from exchange",
        "skip": "⏭ Skip",
        "lang_btn": "🇷🇺 Русский",
    },
}

# User language preferences (in-memory, could be persisted)
_USER_LANG: dict[int, str] = {}

def get_lang(user_id: int) -> str:
    return _USER_LANG.get(user_id, "ru")

def set_lang(user_id: int, lang: str):
    _USER_LANG[user_id] = lang

def t(user_id: int, key: str, **kwargs) -> str:
    lang = get_lang(user_id)
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text
