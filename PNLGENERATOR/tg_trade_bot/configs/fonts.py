FONTS = {
    "bybit": {
        "files": {
            "regular": "fonts/SF_Pro_Display_Regular.otf",
            "bold": "fonts/SF_Pro_Display_Semibold.otf",
        },
        "sizes": {
            "symbol": 54,
            "pnl": 42,
            "leverage": 22,
            "qty": 37,
            "entry": 37,
            "mark": 37,
            "liq": 37,
            "badge": 42,
        },
        "badge_style": "filled",
    },
    "bingx": {
        "files": {
            "regular": "fonts/SF_Pro_Display_Regular.otf",
            "bold": "fonts/SF_Pro_Display_Semibold.otf",
            "badge": "fonts/SF_Pro_Display_Semibold.otf",  # бейджи жирнее как в оригинале
        },
        "sizes": {
            "symbol": 54,
            "pnl": 54,
            "leverage": 40,
            "qty": 52,
            "entry": 52,
            "mark": 52,
            "liq": 52,
            "badge": 26,
            "badge_text_offset_y": -2,

            # Semibold шире → pad_x=8 даёт Лонг(130+16=146px), pad_y=9 + min_h=70
            "badge_pad_green_x": 8,
            "badge_pad_green_y": 9,
            "badge_min_green_w": 100,
            "badge_min_green_h": 70,

            "badge_pad_red_x": 8,
            "badge_pad_red_y": 9,
            "badge_min_red_w": 100,
            "badge_min_red_h": 70,
           

        },
        "badge_style": "filled",
    },
    "custom_bybit": {
        "files": {
            "regular": "fonts/SF_Pro_Display_Regular.otf",
            "bold": "fonts/SF_Pro_Display_Semibold.otf",
        },
        "sizes": {
            "username": 36,
            "symbol": 58,
            "pnl": 120,
            "entry": 48,
            "exit": 48,
            "leverage_text": 40,

        },
        "badge_style": "outline",
    },
    "custom_bingx": {
        "files": {
            "regular": "fonts/SF_Pro_Display_Regular.otf",
            "bold": "fonts/SF_Pro_Display_Semibold.otf",
        },
        "sizes": {
            "username": 50,
            "symbol": 66,
            "pnl": 200,
            "entry": 54,
            "exit": 54,
            "leverage_text": 66,
            "side_text": 66,
        },
        "badge_style": "filled",
    },
}
