from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "store-assets" / "extension-icon-source-1024.png"
ICON_DIR = ROOT / "extension" / "threads-link-cleaner" / "icons"


def vertical_gradient(size, top, bottom):
    image = Image.new("RGB", (size, size), top)
    pixels = image.load()
    for y in range(size):
        ratio = y / (size - 1)
        color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        for x in range(size):
            pixels[x, y] = color
    return image.convert("RGBA")


def capsule_layer(color, box, radius, width, angle):
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(box, radius=radius, outline=color, width=width)
    return layer.rotate(angle, resample=Image.Resampling.BICUBIC, center=(SIZE // 2, SIZE // 2))


def draw_sparkle(draw, cx, cy, radius, color):
    points = [
        (cx, cy - radius),
        (cx + radius * 0.28, cy - radius * 0.28),
        (cx + radius, cy),
        (cx + radius * 0.28, cy + radius * 0.28),
        (cx, cy + radius),
        (cx - radius * 0.28, cy + radius * 0.28),
        (cx - radius, cy),
        (cx - radius * 0.28, cy - radius * 0.28),
    ]
    draw.polygon(points, fill=color)


def build_icon():
    image = vertical_gradient(SIZE, (255, 132, 111), (255, 184, 103))

    # 柔和光暈，讓中央主體在小尺寸仍突出。
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((145, 160, 879, 894), fill=(255, 232, 165, 92))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    image.alpha_composite(glow)

    # 主體陰影。
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.ellipse((235, 330, 800, 770), fill=(101, 65, 82, 72))
    shadow = shadow.filter(ImageFilter.GaussianBlur(42))
    image.alpha_composite(shadow)

    # 兩個互相扣住的圓潤鏈結。
    blue = capsule_layer((41, 199, 232, 255), (235, 355, 645, 595), 120, 94, -27)
    mint = capsule_layer((91, 225, 183, 255), (380, 355, 790, 595), 120, 94, 27)
    image.alpha_composite(blue)
    image.alpha_composite(mint)

    # 在交疊處補一小段青藍鏈節，增加「互扣」的前後層次。
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.arc((310, 375, 660, 690), start=210, end=312, fill=(41, 199, 232, 255), width=94)
    image.alpha_composite(overlay)

    # 可愛表情放在薄荷綠鏈節的右上段。
    face = ImageDraw.Draw(image)
    eye = (33, 73, 96, 255)
    face.ellipse((607, 390, 633, 421), fill=eye)
    face.ellipse((659, 390, 685, 421), fill=eye)
    face.arc((621, 407, 674, 453), start=15, end=165, fill=eye, width=10)

    # 奶油黃色小閃光與泡泡，帶出「清理完成」。
    draw_sparkle(face, 785, 267, 78, (255, 239, 143, 255))
    draw_sparkle(face, 220, 718, 39, (255, 246, 187, 230))
    face.ellipse((809, 385, 842, 418), fill=(255, 238, 151, 220))

    # 小高光，不使用大面積白色。
    highlight = (217, 252, 250, 210)
    face.rounded_rectangle((333, 318, 422, 345), radius=14, fill=highlight)
    face.rounded_rectangle((533, 318, 602, 342), radius=12, fill=highlight)

    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(SOURCE, "PNG", optimize=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        resized = image.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
        resized.save(ICON_DIR / f"icon{size}.png", "PNG", optimize=True)

    print(SOURCE)
    for size in (16, 32, 48, 128):
        print(ICON_DIR / f"icon{size}.png")


if __name__ == "__main__":
    build_icon()
