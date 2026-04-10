"""
utils/pnl_card.py
─────────────────────────────────────────────────────────────────────────────
Pixel-perfect PnL share-card generator for Bybit and BingX.

Основан на детальном анализе оригинальных карточек с бирж.

Входные данные (функция generate_pnl_card):
    exchange      – "bybit" | "bingx"
    symbol        – str, напр. "BTCUSDT"
    direction     – "LONG" | "SHORT"
    leverage      – int
    entry_price   – float
    exit_price    – float   (для BingX закрытая/текущая цена)
    pnl_usdt      – float   (абсолютная П/У в USDT)
    roi_percent   – float   (ROI %, напр. 302.01)
    size          – float   (размер позиции в USDT)
    username      – str
    timestamp     – str     (напр. "03/11 10:18")
    pnl_type      – "realized" | "unrealized"  (только для BingX)
    referral      – str     (реферальный код, напр. "D1BFA4")

Возвращает: io.BytesIO с PNG-изображением.
"""

import io
import os
import math
import functools
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# Пути
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────────────────────────────────────
# ══  BYBIT КОНФИГ  ══
#
# Шаблон: assets/bybit/screenshot_long.png  /  screenshot_short.png
# Размер холста: 864 × 1184 px
# Соотношение сторон: 0.73 (портрет)
# Фон: очень тёмный navy #090C14, rocket-image справа
# Шрифт: SF Pro Display (Regular / Semibold)
# ─────────────────────────────────────────────────────────────────────────────
BYBIT_CONFIG = {
    # ── Размер холста ─────────────────────────────────────────────────────────
    "canvas": (864, 1184),

    # ── Шрифты ────────────────────────────────────────────────────────────────
    "font_regular": "fonts/SF_Pro_Display_Regular.otf",
    "font_bold":    "fonts/SF_Pro_Display_Semibold.otf",

    # ── Размеры шрифтов (px для базового разрешения 864×1184) ─────────────────
    # NOTE: шаблон screenshot_long/short.png уже содержит статичные лейблы:
    #   "ROI" (~y=35%), "Цена входа" (~y=49%), "Текущая цена" (~y=59%)
    # Поэтому мы рисуем только ЗНАЧЕНИЯ, не повторяя лейблы.
    "sizes": {
        "username":    36,    # имя пользователя под иконкой
        "symbol":      58,    # тикер монеты (BTCUSDT)
        "badge":       40,    # текст бейджа «Лонг 20.0X»
        "pnl":        120,    # большое PnL-число (auto-shrink)
        "value":       48,    # белые значения цен
        "footer":      26,    # промо-текст внизу
    },

    # ── Цвета (RGB) ───────────────────────────────────────────────────────────
    "profit_color":  (0, 208, 132),   # #00D084 — зелёный для прибыли
    "loss_color":    (255, 59, 92),   # #FF3B5C — красный для убытка
    "long_color":    (0, 208, 132),
    "short_color":   (255, 59, 92),
    "white":         (255, 255, 255),
    "gray":          (140, 150, 172), # серые лейблы #8C96AC

    # ── Позиции (относительные: 0..1 от ширины/высоты холста) ─────────────────
    # Позиции взяты из BYBIT_CUSTOM_LAYOUT["bybit"] и откалиброваны
    # по реальным скриншотам Bybit.

    # Иконка пользователя (маленький аватар левее username)
    "icon_pos":         (0.045, 0.143),  # (x_rel, y_rel) левый верхний угол
    "icon_size":        60,

    # username (строка под логотипом Bybit)
    "username_pos":     (0.120, 0.160),  # anchor="lm"

    # тикер (BTCUSDT)
    "symbol_pos":       (0.060, 0.240),  # anchor="lm"

    # badge (Лонг 20.0X) — рисуется правее символа
    "badge_gap":        16,              # px между правым краем символа и левым краем badge
    "badge_y_rel":      0.240,           # та же Y, что и symbol
    "badge_radius":     60,
    "badge_pad_x":      18,
    "badge_pad_y":      10,

    # большое PnL-число (ROI label уже есть на шаблоне)
    "pnl_pos":          (0.060, 0.390),  # anchor="lm"
    "pnl_max_width":    0.60,

    # значения цен (лейблы уже на шаблоне)
    # «Цена входа» label бейкнута ~y=0.490 → значение рисуем ниже
    "entry_value_pos":  (0.063, 0.540),  # anchor="lm"
    # «Текущая цена» label бейкнута ~y=0.595 → значение рисуем ниже
    "current_value_pos":(0.063, 0.650),  # anchor="lm"

    # QR-код
    "qr_size":    120,
    "qr_pos":     (0.800, 0.905),        # anchor="mm"

    # Промо / реферальный текст
    "promo_pos":    (0.065, 0.893),      # anchor="lm"
    "ref_code_pos": (0.065, 0.930),      # anchor="lm"

    # ── Зоны очистки (x, y, w, h) — в долях от холста ────────────────────────
    "clear_zones": {
        "username":  (0.040, 0.103, 0.60, 0.080),
        "symbol":    (0.040, 0.195, 0.72, 0.078),
        "pnl":       (0.040, 0.320, 0.63, 0.135),
        "entry_val": (0.040, 0.510, 0.40, 0.080),
        "curr_val":  (0.040, 0.615, 0.40, 0.080),
        "footer":    (0.040, 0.830, 0.72, 0.155),
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# ══  BINGX КОНФИГ  ══
#
# Шаблон: assets/bingx/screenshot_long.png  /  screenshot_short.png
# Размер холста: 1800 × 1800 px (квадрат)
# Фон: тёмный navy #08091A, фото футболистов справа
# Логотип: "BingX" — уже на шаблоне (верхний левый угол)
# Шрифт: SF Pro Display (Regular / Semibold)
# ─────────────────────────────────────────────────────────────────────────────
BINGX_CONFIG = {
    # ── Размер холста ─────────────────────────────────────────────────────────
    "canvas": (1800, 1800),

    # ── Шрифты ────────────────────────────────────────────────────────────────
    "font_regular": "fonts/SF_Pro_Display_Regular.otf",
    "font_bold":    "fonts/SF_Pro_Display_Semibold.otf",

    # ── Размеры шрифтов (px для 1800×1800) ────────────────────────────────────
    "sizes": {
        "pnl_type":    42,    # "Реализованная П/У"
        "symbol":      66,    # тикер "NAORISUSDT"
        "side":        58,    # "Шорт" | "Лонг" — цветной
        "divider":     58,    # "|" разделители между symbol/side/leverage
        "leverage":    58,    # "50X"
        "pnl":        200,    # "+302.01%" (auto-shrink)
        "label":       42,    # серые лейблы
        "value":       52,    # белые значения цен
        "footer_user": 46,    # имя пользователя (CHM_LAB)
        "footer_date": 42,    # дата (03/11 10:18)
        "ref_label":   34,    # "Реферальный код"
        "ref_code":    42,    # "D1BFA4"
    },

    # ── Цвета (RGB) ───────────────────────────────────────────────────────────
    "profit_color":     (0, 200, 122),    # #00C87A — яркий зелёный BingX
    "loss_color":       (255, 45, 120),   # #FF2D78 — розово-красный BingX
    "long_color":       (0, 200, 122),
    "short_color":      (255, 45, 120),
    "long_badge_bg":    (0, 200, 122),    # фон заливки бейджа Long
    "short_badge_bg":   (255, 45, 120),   # фон заливки бейджа Short
    "white":            (255, 255, 255),
    "gray":             (130, 140, 165),  # серые лейблы #828CA5
    "divider_color":    (100, 110, 135),  # цвет "|" разделителей
    "separator_color":  (40, 50, 75),     # тонкая горизонтальная линия

    # ── Позиции (относительные) ───────────────────────────────────────────────
    # Тип П/У (маленький заголовок)
    "pnl_type_pos":     (0.055, 0.215),   # anchor="lm"

    # Строка: SYMBOL | SIDE | LEVERAGE
    "symbol_row_y":     0.285,            # Y для всей строки символа
    "symbol_x":         0.055,            # X начала символа
    "divider_gap":      28,               # px между текстом и "|"
    "side_badge_pad_x": 20,               # паддинг бейджа side
    "side_badge_pad_y": 12,
    "side_badge_radius":14,

    # Большое PnL
    "pnl_pos":          (0.055, 0.445),   # anchor="lm"
    "pnl_max_width":    0.60,

    # Разделительная линия
    "separator_y":      0.575,            # Y позиция линии
    "separator_x1":     0.055,
    "separator_x2":     0.480,
    "separator_thick":  2,

    # Строки цен
    "entry_label_pos":  (0.055, 0.620),   # anchor="lm"
    "entry_value_pos":  (0.055, 0.675),   # anchor="lm"
    "exit_label_pos":   (0.055, 0.730),   # anchor="lm"
    "exit_value_pos":   (0.055, 0.785),   # anchor="lm"

    # Футер (нижняя полоса)
    "footer_icon_size": 72,               # размер иконки BingX в футере
    "footer_icon_pos":  (0.055, 0.900),   # anchor="lm" (центр иконки по Y)
    "footer_user_pos":  (0.120, 0.893),   # anchor="lm"
    "footer_date_pos":  (0.120, 0.920),   # anchor="lm"

    # QR-код
    "qr_size":          160,
    "qr_pos":           (0.845, 0.900),   # anchor="mm"
    "qr_radius":        16,

    # Реферальный блок
    "ref_label_pos":    (0.710, 0.893),   # anchor="lm"
    "ref_code_pos":     (0.710, 0.924),   # anchor="lm"

    # Зоны очистки (x, y, w, h) относительные
    "clear_zones": {
        "pnl_type": (0.05, 0.190, 0.45, 0.065),
        "symbol":   (0.05, 0.248, 0.50, 0.072),
        "pnl":      (0.05, 0.360, 0.58, 0.175),
        "entry":    (0.05, 0.590, 0.45, 0.115),
        "exit":     (0.05, 0.710, 0.45, 0.100),
        "footer":   (0.04, 0.865, 0.92, 0.120),
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Кэш шрифтов
# ─────────────────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _fp(cfg: dict, bold: bool = False) -> str:
    key = "font_bold" if bold else "font_regular"
    return os.path.join(BASE_DIR, cfg[key])


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────
def _px(rel: float, size: int) -> int:
    """Относительная координата → пиксели."""
    return int(rel * size)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _auto_shrink_font(
    draw_ref: ImageDraw.ImageDraw,
    text: str,
    bold_path: str,
    start_size: int,
    max_px_width: int,
    min_size: int = 40,
) -> ImageFont.FreeTypeFont:
    """Уменьшает размер шрифта пока текст не уместится в max_px_width."""
    size = start_size
    while size > min_size:
        f = _font(bold_path, size)
        w, _ = _text_size(draw_ref, text, f)
        if w <= max_px_width:
            return f
        size -= 4
    return _font(bold_path, min_size)


def _format_price(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        # Не более 5 знаков после запятой для читаемости
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) > 5:
            s = f"{value:.5f}".rstrip("0").rstrip(".")
        return s
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _draw_gradient_background(img: Image.Image, top_color: tuple, bottom_color: tuple):
    """Рисует вертикальный градиент поверх изображения (мягкий оверлей)."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y in range(h):
        t = y / h
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        a = int(top_color[3] * (1 - t) + bottom_color[3] * t) if len(top_color) == 4 else 60
        overlay.paste((r, g, b, a), (0, y, w, y + 1))
    img.alpha_composite(overlay)


def _draw_pnl_value(
    draw: ImageDraw.ImageDraw,
    text: str,
    pos: tuple,           # (x_rel, y_rel)
    canvas_w: int,
    canvas_h: int,
    bold_path: str,
    start_size: int,
    max_width_rel: float,
    color: tuple,
):
    """Рисует большое PnL-число с авто-подгонкой размера."""
    x = _px(pos[0], canvas_w)
    y = _px(pos[1], canvas_h)
    max_w = _px(max_width_rel, canvas_w)
    # dummy draw для измерений
    dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    font = _auto_shrink_font(dummy, text, bold_path, start_size, max_w)
    draw.text((x, y), text, fill=color, font=font, anchor="lm")
    return font


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    text: str,
    bg_color: tuple,
    text_color: tuple,
    font: ImageFont.FreeTypeFont,
    pad_x: int = 18,
    pad_y: int = 10,
    radius: int = 60,
    style: str = "filled",  # "filled" | "outline"
):
    """
    Рисует бейдж (pill) с текстом.
    style="filled"  → закрашенный фон (BingX)
    style="outline" → только рамка (Bybit)
    """
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    bw = tw + pad_x * 2
    bh = th + pad_y * 2
    x1, y1 = cx - bw // 2, cy - bh // 2
    x2, y2 = x1 + bw, y1 + bh

    if style == "filled":
        draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=bg_color)
    else:
        draw.rounded_rectangle(
            (x1, y1, x2, y2), radius=radius,
            fill=(30, 35, 48),
            outline=bg_color,
            width=2,
        )
    draw.text(((x1 + x2) / 2, (y1 + y2) / 2), text, fill=text_color, font=font, anchor="mm")
    return bw  # возвращаем ширину бейджа


def _draw_trade_details(
    draw: ImageDraw.ImageDraw,
    canvas_w: int,
    canvas_h: int,
    cfg: dict,
    label1: str,
    value1: str,
    label2: str,
    value2: str,
    label_font: ImageFont.FreeTypeFont,
    value_font: ImageFont.FreeTypeFont,
    gray: tuple,
    white: tuple,
):
    """
    Рисует два блока label+value:
      label1     label2
      value1     value2
    (для одноколоночного — просто передай одинаковые X)
    """
    def _p(pos_key):
        return (
            _px(cfg[pos_key][0], canvas_w),
            _px(cfg[pos_key][1], canvas_h),
        )

    draw.text(_p("entry_label_pos"), label1, fill=gray, font=label_font, anchor="lm")
    draw.text(_p("entry_value_pos"), value1, fill=white, font=value_font, anchor="lm")
    draw.text(_p("exit_label_pos"),  label2, fill=gray, font=label_font, anchor="lm")
    draw.text(_p("exit_value_pos"),  value2, fill=white, font=value_font, anchor="lm")


def _make_qr(data: str, size: int) -> Image.Image:
    """
    Создаёт QR-код. Требует пакет qrcode.
    Если не установлен — возвращает серый placeholder.
    """
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    except ImportError:
        # placeholder
        ph = Image.new("RGBA", (size, size), (200, 200, 200, 255))
        d = ImageDraw.Draw(ph)
        d.rectangle((4, 4, size - 4, size - 4), outline=(80, 80, 80), width=3)
        return ph


def _paste_centered(canvas: Image.Image, layer: Image.Image, cx: int, cy: int):
    """Вставляет layer с центром в (cx, cy)."""
    x = cx - layer.width // 2
    y = cy - layer.height // 2
    if layer.mode == "RGBA":
        canvas.paste(layer, (x, y), layer)
    else:
        canvas.paste(layer, (x, y))


def _clear_zone(img: Image.Image, draw: ImageDraw.ImageDraw, zone: tuple):
    """
    Заливает прямоугольник цветом фона (пиксель из угла зоны).
    zone = (x_rel, y_rel, w_rel, h_rel)
    """
    iw, ih = img.size
    x  = _px(zone[0], iw)
    y  = _px(zone[1], ih)
    x2 = x + _px(zone[2], iw)
    y2 = y + _px(zone[3], ih)
    # сэмплируем фоновый цвет из левого верхнего угла зоны
    bg = img.getpixel((max(0, x + 2), max(0, y + 2)))[:3]
    draw.rectangle((x, y, x2, y2), fill=bg)


# ─────────────────────────────────────────────────────────────────────────────
# ══  ГЛАВНАЯ ФУНКЦИЯ  ══
# ─────────────────────────────────────────────────────────────────────────────
def generate_pnl_card(
    exchange:    Literal["bybit", "bingx"],
    symbol:      str,
    direction:   Literal["LONG", "SHORT"],
    leverage:    int,
    entry_price: float,
    exit_price:  float,
    pnl_usdt:    float,
    roi_percent: float,
    size:        float        = 0.0,
    username:    str          = "",
    timestamp:   str          = "",
    pnl_type:    Literal["realized", "unrealized"] = "realized",
    referral:    str          = "",
) -> io.BytesIO:
    """
    Генерирует PnL share-карточку и возвращает io.BytesIO с PNG.

    Примеры:
        buf = generate_pnl_card(
            exchange="bingx", symbol="NAORISUSDT", direction="SHORT",
            leverage=50, entry_price=0.05828, exit_price=0.05477,
            pnl_usdt=0, roi_percent=302.01,
            username="CHM_LAB", timestamp="03/11 10:18", referral="D1BFA4"
        )
        with open("card.png", "wb") as f:
            f.write(buf.getvalue())
    """
    symbol = symbol.upper()
    direction = direction.upper()

    if exchange == "bybit":
        return _generate_bybit_card(
            symbol=symbol, direction=direction, leverage=leverage,
            entry_price=entry_price, exit_price=exit_price,
            pnl_usdt=pnl_usdt, roi_percent=roi_percent,
            username=username, referral=referral,
        )
    elif exchange == "bingx":
        return _generate_bingx_card(
            symbol=symbol, direction=direction, leverage=leverage,
            entry_price=entry_price, exit_price=exit_price,
            pnl_usdt=pnl_usdt, roi_percent=roi_percent,
            username=username, timestamp=timestamp,
            pnl_type=pnl_type, referral=referral,
        )
    else:
        raise ValueError(f"Неизвестная биржа: {exchange}")


# ─────────────────────────────────────────────────────────────────────────────
# ══  BYBIT CARD  ══
# ─────────────────────────────────────────────────────────────────────────────
def _generate_bybit_card(
    symbol: str,
    direction: str,
    leverage: int,
    entry_price: float,
    exit_price: float,
    pnl_usdt: float,
    roi_percent: float,
    username: str = "",
    referral: str = "",
) -> io.BytesIO:
    cfg = BYBIT_CONFIG
    W, H = cfg["canvas"]

    # ── Шаблон ────────────────────────────────────────────────────────────────
    # Шаблоны screenshot_long/short.png уже содержат:
    #   • Фон (gradient + grid + rocket image)
    #   • Логотип BYBIT (верхний левый угол)
    #   • Статичные лейблы: "ROI", "Цена входа", "Текущая цена"
    # Мы накладываем поверх только динамические данные.
    template_side = "long" if roi_percent >= 0 else "short"
    tmpl_path = os.path.join(BASE_DIR, "assets", "bybit", f"screenshot_{template_side}.png")
    if not os.path.exists(tmpl_path):
        raise FileNotFoundError(f"Шаблон не найден: {tmpl_path}")

    img = Image.open(tmpl_path).convert("RGBA")
    img = img.resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    # ── Очищаем переменные зоны ───────────────────────────────────────────────
    for zone in cfg["clear_zones"].values():
        _clear_zone(img, draw, zone)
    draw = ImageDraw.Draw(img)

    # ── Шрифты ────────────────────────────────────────────────────────────────
    fp_r = _fp(cfg, bold=False)
    fp_b = _fp(cfg, bold=True)
    sz = cfg["sizes"]
    f_user   = _font(fp_r, sz["username"])
    f_symbol = _font(fp_b, sz["symbol"])
    f_badge  = _font(fp_r, sz["badge"])
    f_value  = _font(fp_b, sz["value"])
    f_footer = _font(fp_r, sz["footer"])

    # ── Цвета ─────────────────────────────────────────────────────────────────
    WHITE    = cfg["white"]
    GRAY     = cfg["gray"]
    is_long  = direction == "LONG"
    dir_color = cfg["long_color"]   if is_long else cfg["short_color"]
    pnl_color = cfg["profit_color"] if roi_percent >= 0 else cfg["loss_color"]
    dir_text  = "Лонг" if is_long else "Шорт"

    # ── Иконка Bybit (маленькая слева от username) ────────────────────────────
    icon_path = os.path.join(BASE_DIR, "assets", "bybit", "icon.png")
    if os.path.exists(icon_path):
        icon_size = cfg["icon_size"]
        icon = Image.open(icon_path).convert("RGBA")
        icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
        ix = _px(cfg["icon_pos"][0], W)
        iy = _px(cfg["icon_pos"][1], H)
        img.paste(icon, (ix, iy), icon)
        draw = ImageDraw.Draw(img)

    # ── Username ─────────────────────────────────────────────────────────────
    if username:
        ux = _px(cfg["username_pos"][0], W)
        uy = _px(cfg["username_pos"][1], H)
        draw.text((ux, uy), username, fill=GRAY, font=f_user, anchor="lm")

    # ── Symbol ────────────────────────────────────────────────────────────────
    sx = _px(cfg["symbol_pos"][0], W)
    sy = _px(cfg["symbol_pos"][1], H)
    draw.text((sx, sy), symbol, fill=WHITE, font=f_symbol, anchor="lm")
    sym_w, _ = _text_size(draw, symbol, f_symbol)

    # ── Badge (Лонг 20.0X) — полупрозрачный pill сразу справа от символа ─────
    badge_text = f"{dir_text} {leverage:.1f}X"
    bb = draw.textbbox((0, 0), badge_text, font=f_badge)
    bw = bb[2] - bb[0] + cfg["badge_pad_x"] * 2
    badge_cx = sx + sym_w + cfg["badge_gap"] + bw // 2
    badge_cy = _px(cfg["badge_y_rel"], H)

    # Полупрозрачный фон через RGBA overlay
    bh_val = bb[3] - bb[1] + cfg["badge_pad_y"] * 2
    x1 = badge_cx - bw // 2
    y1 = badge_cy - bh_val // 2
    x2 = x1 + bw
    y2 = y1 + bh_val
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        [x1, y1, x2, y2], radius=cfg["badge_radius"],
        fill=(35, 35, 48, 110),
    )
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    draw.text(
        ((x1 + x2) / 2, (y1 + y2) / 2),
        badge_text, fill=dir_color, font=f_badge, anchor="mm",
    )

    # ── PnL (ROI label уже на шаблоне) ───────────────────────────────────────
    roi_text = f"{roi_percent:+.2f}%"
    _draw_pnl_value(
        draw, roi_text,
        cfg["pnl_pos"], W, H,
        fp_b, sz["pnl"], cfg["pnl_max_width"],
        pnl_color,
    )

    # ── Значения цен (лейблы уже бейкнуты в шаблоне) ─────────────────────────
    draw.text(
        (_px(cfg["entry_value_pos"][0], W), _px(cfg["entry_value_pos"][1], H)),
        _format_price(entry_price), fill=WHITE, font=f_value, anchor="lm",
    )
    draw.text(
        (_px(cfg["current_value_pos"][0], W), _px(cfg["current_value_pos"][1], H)),
        _format_price(exit_price), fill=WHITE, font=f_value, anchor="lm",
    )

    # ── Footer / QR ───────────────────────────────────────────────────────────
    if referral:
        qr_img = _make_qr(referral, cfg["qr_size"])
        qr_cx = _px(cfg["qr_pos"][0], W)
        qr_cy = _px(cfg["qr_pos"][1], H)
        _paste_centered(img, qr_img, qr_cx, qr_cy)
        draw = ImageDraw.Draw(img)

        promo = "Присоединяйтесь и получите более $5,000 в бонусах!"
        draw.text(
            (_px(cfg["promo_pos"][0], W), _px(cfg["promo_pos"][1], H)),
            promo, fill=GRAY, font=f_footer, anchor="lm",
        )
        draw.text(
            (_px(cfg["ref_code_pos"][0], W), _px(cfg["ref_code_pos"][1], H)),
            f"Реферальный код: {referral}",
            fill=WHITE, font=f_footer, anchor="lm",
        )

    # ── Сохраняем ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# ══  BINGX CARD  ══
# ─────────────────────────────────────────────────────────────────────────────
def _generate_bingx_card(
    symbol: str,
    direction: str,
    leverage: int,
    entry_price: float,
    exit_price: float,
    pnl_usdt: float,
    roi_percent: float,
    username: str = "",
    timestamp: str = "",
    pnl_type: str = "realized",
    referral: str = "",
) -> io.BytesIO:
    cfg = BINGX_CONFIG
    W, H = cfg["canvas"]

    # ── Шаблон ────────────────────────────────────────────────────────────────
    template_side = "long" if roi_percent >= 0 else "short"
    tmpl_path = os.path.join(BASE_DIR, "assets", "bingx", f"screenshot_{template_side}.png")
    if not os.path.exists(tmpl_path):
        raise FileNotFoundError(f"Шаблон не найден: {tmpl_path}")

    img = Image.open(tmpl_path).convert("RGBA")
    img = img.resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    # ── Очистка переменных зон ────────────────────────────────────────────────
    for zone in cfg["clear_zones"].values():
        _clear_zone(img, draw, zone)
    draw = ImageDraw.Draw(img)

    # ── Шрифты ────────────────────────────────────────────────────────────────
    fp_r = _fp(cfg, bold=False)
    fp_b = _fp(cfg, bold=True)
    sz = cfg["sizes"]
    f_type    = _font(fp_r, sz["pnl_type"])
    f_symbol  = _font(fp_b, sz["symbol"])
    f_side    = _font(fp_b, sz["side"])
    f_div     = _font(fp_r, sz["divider"])
    f_lev     = _font(fp_r, sz["leverage"])
    f_label   = _font(fp_r, sz["label"])
    f_value   = _font(fp_b, sz["value"])
    f_fuser   = _font(fp_b, sz["footer_user"])
    f_fdate   = _font(fp_r, sz["footer_date"])
    f_rlabel  = _font(fp_r, sz["ref_label"])
    f_rcode   = _font(fp_b, sz["ref_code"])

    # ── Цвета ─────────────────────────────────────────────────────────────────
    WHITE       = cfg["white"]
    GRAY        = cfg["gray"]
    DIV_COL     = cfg["divider_color"]
    is_long     = direction == "LONG"
    dir_color   = cfg["long_color"]   if is_long else cfg["short_color"]
    badge_bg    = cfg["long_badge_bg"] if is_long else cfg["short_badge_bg"]
    pnl_color   = cfg["profit_color"]  if roi_percent >= 0 else cfg["loss_color"]
    dir_ru      = "Лонг" if is_long else "Шорт"
    type_ru     = "Реализованная П/У" if pnl_type == "realized" else "Нереализованная П/У"
    # Метки для цен
    if pnl_type == "realized":
        exit_label  = "Цена закрытия"
        entry_label = "Цена входа"
    else:
        exit_label  = "Последняя цена"
        entry_label = "Цена входа"

    # ── Тип П/У ───────────────────────────────────────────────────────────────
    draw.text(
        (_px(cfg["pnl_type_pos"][0], W), _px(cfg["pnl_type_pos"][1], H)),
        type_ru, fill=GRAY, font=f_type, anchor="lm",
    )

    # ── Строка Symbol | Side | Leverage ──────────────────────────────────────
    row_y = _px(cfg["symbol_row_y"], H)
    cur_x = _px(cfg["symbol_x"], W)
    GAP = cfg["divider_gap"]

    # Symbol
    draw.text((cur_x, row_y), symbol, fill=WHITE, font=f_symbol, anchor="lm")
    sym_w, sym_h = _text_size(draw, symbol, f_symbol)
    cur_x += sym_w + GAP

    # Разделитель "|"
    draw.text((cur_x, row_y), "|", fill=DIV_COL, font=f_div, anchor="lm")
    div_w, _ = _text_size(draw, "|", f_div)
    cur_x += div_w + GAP

    # Side badge (заливка)
    bb_side = draw.textbbox((0, 0), dir_ru, font=f_side)
    side_tw = bb_side[2] - bb_side[0]
    side_th = bb_side[3] - bb_side[1]
    pad_x = cfg["side_badge_pad_x"]
    pad_y = cfg["side_badge_pad_y"]
    rad   = cfg["side_badge_radius"]
    bw = side_tw + pad_x * 2
    bh = side_th + pad_y * 2
    side_cx = cur_x + bw // 2
    side_cy = row_y
    x1, y1 = side_cx - bw // 2, side_cy - bh // 2
    x2, y2 = x1 + bw, y1 + bh
    draw.rounded_rectangle((x1, y1, x2, y2), radius=rad, fill=badge_bg)
    draw.text(((x1 + x2) / 2, (y1 + y2) / 2), dir_ru,
              fill=WHITE, font=f_side, anchor="mm")
    cur_x += bw + GAP

    # Разделитель "|"
    draw.text((cur_x, row_y), "|", fill=DIV_COL, font=f_div, anchor="lm")
    cur_x += div_w + GAP

    # Leverage
    lev_text = f"{leverage}X"
    draw.text((cur_x, row_y), lev_text, fill=WHITE, font=f_lev, anchor="lm")

    # ── PnL ───────────────────────────────────────────────────────────────────
    roi_text = f"{roi_percent:+.2f}%"
    _draw_pnl_value(
        draw, roi_text,
        cfg["pnl_pos"], W, H,
        fp_b, sz["pnl"], cfg["pnl_max_width"],
        pnl_color,
    )

    # ── Разделительная линия ──────────────────────────────────────────────────
    sep_y  = _px(cfg["separator_y"], H)
    sep_x1 = _px(cfg["separator_x1"], W)
    sep_x2 = _px(cfg["separator_x2"], W)
    draw.line(
        [(sep_x1, sep_y), (sep_x2, sep_y)],
        fill=cfg["separator_color"],
        width=cfg["separator_thick"],
    )

    # ── Trade details ─────────────────────────────────────────────────────────
    _draw_trade_details(
        draw, W, H, cfg,
        exit_label,  _format_price(exit_price),
        entry_label, _format_price(entry_price),
        f_label, f_value, GRAY, WHITE,
    )

    # ── Футер: иконка + username + дата ──────────────────────────────────────
    bingx_icon_path = os.path.join(BASE_DIR, "assets", "bingx", "icon.png")
    icon_size = cfg["footer_icon_size"]
    icon_cx = _px(cfg["footer_icon_pos"][0], W)
    icon_cy = _px(cfg["footer_icon_pos"][1], H)
    if os.path.exists(bingx_icon_path):
        icon = Image.open(bingx_icon_path).convert("RGBA")
        icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
        _paste_centered(img, icon, icon_cx, icon_cy)
        draw = ImageDraw.Draw(img)

    if username:
        draw.text(
            (_px(cfg["footer_user_pos"][0], W), _px(cfg["footer_user_pos"][1], H)),
            username, fill=WHITE, font=f_fuser, anchor="lm",
        )
    if timestamp:
        draw.text(
            (_px(cfg["footer_date_pos"][0], W), _px(cfg["footer_date_pos"][1], H)),
            timestamp, fill=GRAY, font=f_fdate, anchor="lm",
        )

    # ── QR + реферальный блок ────────────────────────────────────────────────
    qr_data = referral if referral else "D1BFA4"
    qr_img = _make_qr(qr_data, cfg["qr_size"])
    qr_cx = _px(cfg["qr_pos"][0], W)
    qr_cy = _px(cfg["qr_pos"][1], H)
    _paste_centered(img, qr_img, qr_cx, qr_cy)
    draw = ImageDraw.Draw(img)

    if referral:
        draw.text(
            (_px(cfg["ref_label_pos"][0], W), _px(cfg["ref_label_pos"][1], H)),
            "Реферальный код", fill=GRAY, font=f_rlabel, anchor="lm",
        )
        draw.text(
            (_px(cfg["ref_code_pos"][0], W), _px(cfg["ref_code_pos"][1], H)),
            referral, fill=WHITE, font=f_rcode, anchor="lm",
        )

    # ── Сохраняем ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
