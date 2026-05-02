LAYOUT = {
    "bybit": {
        # --- Обычный скрин ---
        "symbol":    {"x": 0.05, "y": 0.16, "anchor": "lm", "dx": 0, "dy": 0},
        "leverage":  {"x": 0.05, "y": 0.24, "anchor": "lm"},
        "side_badge":{"x": 0.32, "y": 0.16, "anchor": "lm", "dx": 0, "dy": -2, "w": 140, "h": 50, "radius": 20},
        "pnl":       {"x": 0.95, "y": 0.24, "anchor": "rm", "dx": 0, "dy": 0},

        "qty":   {"x": 0.052, "y": 0.56, "anchor": "lm"},
        "entry": {"x": 0.29,  "y": 0.56, "anchor": "lm"},
        "mark":  {"x": 0.47,  "y": 0.56, "anchor": "lm"},
        "liq":   {"x": 0.95,  "y": 0.56, "anchor": "rm"},

        # Зоны очистки
        "clear_symbol":    {"x": 0.05, "y": 0.116, "w": 0.19, "h": 0.11},
        "clear_side_badge": {
            "x": 0.22, "y": 0.09, "w": 0.18, "h": 0.25,
            "bg_x": 0.10, "bg_y": 0.05,
        },
        "clear_leverage":  {"x": 0.05, "y": 0.23,  "w": 0.18, "h": 0.10},
        "clear_qty":       {"x": 0.05, "y": 0.522, "w": 0.18, "h": 0.10},
        "clear_entry":     {"x": 0.29, "y": 0.527, "w": 0.12, "h": 0.10},
        "clear_mark":      {"x": 0.44, "y": 0.527, "w": 0.18, "h": 0.10},
        "clear_liq":       {"x": 0.775,"y": 0.527, "w": 0.26, "h": 0.10},
        "clear_pnl":       {"x": 0.55, "y": 0.22,  "w": 0.42, "h": 0.18},
    },

    "bingx": {
        # --- Обычный скрин ---
        "symbol":    {"x": 0.055, "y": 0.12, "anchor": "lm", "dx": 0, "dy": 0},
        "leverage":  {"x": 0.30,  "y": 0.20, "anchor": "lm"},
        "side_badge":{"x": 0.10,  "y": 0.202, "anchor": "lm", "dx": 0, "dy": 0, "w": 150, "h": 70, "radius": 14},
        "pnl":       {"x": 0.98,  "y": 0.20, "anchor": "rm", "dx": 0, "dy": 0},

        # Иконка монеты
        "symbol_icon": {
            "x": 0.03, "y": 0.03,
            "size": 170, "gap": 1,
            "dx": 0, "dy": 0,
        },

        # Подписи внизу (y выровнено по реальным позициям значений в шаблоне)
        # Row1: template qty center y=407px (0.397), Row2: template entry center y=586px (0.572)
        "qty":    {"x": 0.05, "y": 0.397, "anchor": "lm"},
        "margin": {"x": 0.40, "y": 0.397, "anchor": "lm"},
        "entry":  {"x": 0.05, "y": 0.572, "anchor": "lm"},
        "mark":   {"x": 0.40, "y": 0.572, "anchor": "lm"},
        "liq":    {"x": 0.96, "y": 0.572, "anchor": "rm"},

        "risk": {
            "x": 0.96, "y": 0.397,
            "dx": 0, "dy": 0,
            "anchor": "rm",
        },

        # Серые боксы Кросс / 20x (Semibold: Кросс tw=164 pad_x=3→170px; 50X tw=108 pad_x=7→122px)
        "margin_mode": {
            "x": 0.22, "y": 0.202,
            "pad_x": 3, "pad_y": 9, "min_h": 70, "radius": 14,
        },
        "leverage_bingx": {
            "x": 0.33, "y": 0.202,
            "pad_x": 7, "pad_y": 9, "min_h": 70, "radius": 14,
        },

        # Очистка
        # clear_side_badge: w=0.38 (до x=614px, бейджи заканчиваются на x≈586)
        # bg берётся из правого фона строки бейджей (15,15,15), НЕ из угла (7,7,6)
        "clear_symbol":    {"x": 0.05, "y": 0.09,  "w": 0.32, "h": 0.05},
        "clear_side_badge": {
            "x": 0.02, "y": 0.15, "w": 0.38, "h": 0.10,
            "bg_x": 0.80, "bg_y": 0.20,
        },
        "clear_leverage":  {"x": 0.27, "y": 0.13,  "w": 0.13, "h": 0.14,
                            "bg_x": 0.80, "bg_y": 0.20},
        # Row1 values — три перекрывающих зоны покрывают всю ширину строки значений
        # bg: тёмный промежуток между бейджами и лейблами строки 1 (y≈28%)
        # y=0.371 (≈380px) — начинаем ДО данных шаблона (template Row1 values y=386)
        # h=0.105 → конец 0.476 (488px) — ОСТАНАВЛИВАЕМСЯ до лейблов "Цена входа" (y=490)
        "clear_qty": {
            "x": 0.02, "y": 0.371, "w": 0.35, "h": 0.105,
            "bg_x": 0.50, "bg_y": 0.28,
        },
        "clear_margin": {
            "x": 0.34, "y": 0.371, "w": 0.34, "h": 0.105,
            "bg_x": 0.50, "bg_y": 0.28,
        },
        "clear_risk": {
            "x": 0.63, "y": 0.371, "w": 0.37, "h": 0.105,
            "bg_x": 0.50, "bg_y": 0.28,
        },
        # Row2 values — три перекрывающих зоны покрывают всю ширину (y≈52-64%)
        # y=0.521 (≈534px) — начинаем ПОСЛЕ серых лейблов "Цена входа" (template y=490-533)
        # bg: тёмный промежуток между лейблами (y=533) и значениями (y=568)
        "clear_entry": {
            "x": 0.02, "y": 0.523, "w": 0.35, "h": 0.108,
            "bg_x": 0.50, "bg_y": 0.542,
        },
        "clear_mark": {
            "x": 0.34, "y": 0.523, "w": 0.32, "h": 0.108,
            "bg_x": 0.50, "bg_y": 0.542,
        },
        "clear_liq": {
            "x": 0.60, "y": 0.523, "w": 0.40, "h": 0.108,
            "bg_x": 0.50, "bg_y": 0.542,
        },
        # y=0.150 (154px) — starts AFTER header "Нереализованная П/В(USDT)" which ends at y=144px
        "clear_pnl":       {"x": 0.55, "y": 0.150, "w": 0.43, "h": 0.12},
    },
}

BYBIT_CUSTOM_LAYOUT = {
    "bybit": {
        # Username + avatar (top-left, just under BYBIT logo)
        "username":  {"x": 0.155, "y": 0.165, "anchor": "lm"},
        "symbol_icon": {
            "x": 0.075, "y": 0.165,
            "size": 56, "gap": 6,
            "dx": 0, "dy": 0,
        },
        # Symbol + side pill row (just above ROI label which sits at y≈0.324)
        "symbol":    {"x": 0.075, "y": 0.275, "anchor": "lm"},
        # Big ROI %  (template label "ROI" at y≈0.324, value sits BELOW it)
        "pnl":       {"x": 0.075, "y": 0.430, "anchor": "lm"},
        # Entry / current price values BELOW their respective labels
        # (template "Цена входа" at y≈0.505 → value at 0.560)
        # (template "Текущая цена" at y≈0.606 → value at 0.665)
        "entry":     {"x": 0.075, "y": 0.560, "anchor": "lm"},
        "exit":      {"x": 0.075, "y": 0.665, "anchor": "lm"},
        # Reference footer band:
        # - line 2 "более ____ в бонусах!" has a gap at x≈[160-260], y≈1080
        # - line 3 "Реферальный код: ___" has colon at x≈393, text-center y≈1126
        "price":     {"x": 0.23,  "y": 0.72, "anchor": "lm"},
        "bonus":     {"x": 0.190, "y": 0.913, "anchor": "lm"},
        "referral":  {"x": 0.480, "y": 0.951, "anchor": "lm"},

        # Side pill (Long 50.0X / Short 50.0X) — anchored next to symbol, sized dynamically
        "cross_leverage": {
            "x": 0.45, "y": 0.275,
            "w": 0.16, "h": 0.06,
            "pad_x": 22, "pad_y": 12,
            "radius": 50,
        },

        # No clear-zones — template is already clean of values
    },

    "bingx": {
        # Кастомный BingX
        "username": {"x": 0.15,  "y": 0.87, "anchor": "lm"},
        "referral": {"x": 0.72,  "y": 0.90, "anchor": "lm"},
        "datetime": {"x": 0.15,  "y": 0.90, "anchor": "lm"},
        "symbol":   {"x": 0.055, "y": 0.335,"anchor": "lm"},
        "pnl":      {"x": 0.05,  "y": 0.42, "anchor": "lm"},
        "entry":    {"x": 0.36,  "y": 0.592,"anchor": "lm"},
        "exit":     {"x": 0.26,  "y": 0.653,"anchor": "lm"},
        "price":    {"x": 0.22,  "y": 0.70, "anchor": "lm"},

        # Позиция Лонг/Шорт и плечо
        "side_position":     {"x": 0.33, "y": 0.355, "anchor": "lm"},
        "leverage_position": {"x": 0.48, "y": 0.355, "anchor": "lm"},

        "cross_leverage": {
            "x": 0.25, "y": 0.22,
            "w": 0.16, "h": 0.08,
            "pad_x": 12, "pad_y": 8,
            "radius": 65,
        },

        "lines": {
            "x": 0.065, "y": 0.335,
            "size": 80, "gap": 10, "spacing": 221,
            "dx": 0, "dy": 0,
            "side_dx": 8, "side_dy": 0,
            "lev_dx": 5,  "lev_dy": 0,
        },

        # Очистка
        "clear_entry":    {"x": 0.26, "y": 0.51, "w": 0.18, "h": 0.10},
        "clear_exit":     {"x": 0.43, "y": 0.51, "w": 0.18, "h": 0.10},
        "clear_pnl":      {"x": 0.53, "y": 0.20, "w": 0.40, "h": 0.18},
        "clear_leverage": {"x": 0.26, "y": 0.28, "w": 0.18, "h": 0.08},
    },
}
