# utils/draw_text.py

from PIL import ImageFont


def px(val, size):
    return int(val * size)


def draw_text(
    draw,
    layout,
    key,
    text,
    font_path,
    font_size,
    color,
    img_w,
    img_h
):
    cfg = layout[key]

    x = px(cfg["x"], img_w)
    y = px(cfg["y"], img_h)

    x += cfg.get("dx", 0)
    y += cfg.get("dy", 0)

    font = ImageFont.truetype(font_path, font_size)

    anchor = cfg.get("anchor", "la")

    draw.text(
        (x, y),
        text,
        fill=color,
        font=font,
        anchor=anchor
    )


