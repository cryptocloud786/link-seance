#!/usr/bin/env python3
"""Render a captured (ANSI-coloured) Link Séance session to a PNG screenshot.

    FORCE_COLOR=1 python link_seance.py examples/bookmarks-2009.md > session.ans
    python tools/render_terminal.py session.ans examples/demo.png
"""
import re
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SIZE, COLS, PAD = 15, 118, 26
BG, FG = (22, 16, 24), (232, 222, 206)
PALETTE = {"31": (230, 96, 110), "32": (126, 201, 138), "33": (242, 170, 70),
           "35": (196, 140, 235), "2": (140, 126, 120)}
ANSI = re.compile(r"\x1b\[([\d;]*)m")


def segments(line):
    """Split a line into (text, colour, bold) runs."""
    pos, color, bold = 0, FG, False
    for m in ANSI.finditer(line):
        if m.start() > pos:
            yield line[pos:m.start()], color, bold
        codes = m.group(1).split(";")
        if codes == ["0"] or codes == [""]:
            color, bold = FG, False
        for cd in codes:
            if cd == "1":
                bold = True
            elif cd in PALETTE:
                color = PALETTE[cd]
        pos = m.end()
    if pos < len(line):
        yield line[pos:], color, bold


def wrap(line):
    plain = ANSI.sub("", line)
    if len(plain) <= COLS:
        return [line]
    indent = " " * (len(plain) - len(plain.lstrip()) + 2)
    return [ln for ln in textwrap.wrap(line, COLS, subsequent_indent=indent,
                                       break_long_words=False)]


def main(src, dst):
    raw = open(src, encoding="utf-8").read()
    raw = re.sub(r"^llama_kv_cache.*\n", "", raw, flags=re.M)
    lines = []
    for ln in raw.rstrip("\n").split("\n"):
        lines += wrap(ln.replace("🕯️", "\u2020").replace("🌼", "*"))
    f, fb = ImageFont.truetype(FONT, SIZE), ImageFont.truetype(FONT_B, SIZE)
    cw = f.getlength("M")
    lh = int(SIZE * 1.45)
    w = int(cw * COLS + PAD * 2)
    h = int(lh * len(lines) + PAD * 2 + 34)
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 30], fill=(40, 30, 42))
    for i, col in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([14 + i * 22, 9, 26 + i * 22, 21], fill=col)
    d.text((w / 2 - 110, 7), "link-seance — bash", font=f, fill=(170, 150, 140))
    y = 30 + PAD
    for ln in lines:
        x = PAD
        for text, color, bold in segments(ln):
            for ch in text:
                if ch == "\u2020":  # draw a little candle instead of an emoji
                    cx = x + cw / 2
                    d.rectangle([cx - 3, y + 8, cx + 3, y + lh - 3], fill=(240, 228, 200))
                    d.ellipse([cx - 3, y, cx + 3, y + 9], fill=(242, 170, 70))
                else:
                    d.text((x, y), ch, font=fb if bold else f, fill=color)
                x += cw
        y += lh
    img.save(dst)
    print(f"wrote {dst} ({w}x{h})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
