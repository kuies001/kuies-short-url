from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import shutil

SIZE = 1024
ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = ROOT / "extension" / "threads-link-cleaner" / "icons"
SOURCE = ROOT / "store-assets" / "extension-icon-kuies-1024.png"
ARCHIVE = ROOT / "store-assets" / "archive" / "icons-v1.6.7"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf"


def mix(a, b, t):
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))


def fit_font(text, max_width, start_size=280):
    size = start_size
    while size > 20:
        font = ImageFont.truetype(FONT, size)
        box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font, stroke_width=0)
        if box[2] - box[0] <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(FONT, size)


ICON_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE.mkdir(parents=True, exist_ok=True)
for size in (16, 32, 48, 128):
    old = ICON_DIR / f"icon{size}.png"
    backup = ARCHIVE / old.name
    if old.exists() and not backup.exists():
        shutil.copy2(old, backup)

# 柔和鼠尾草綠至薄荷藍綠漸層，明確不是黑／白底。
top = (224, 244, 185)
bottom = (151, 218, 199)
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
pix = img.load()
for y in range(SIZE):
    c = mix(top, bottom, y / (SIZE - 1))
    for x in range(SIZE):
        # 中央略亮，維持彩色底但讓文字更清楚。
        distance = abs(x - SIZE / 2) / (SIZE / 2)
        lift = max(0.0, 1.0 - distance) * 0.035
        pix[x, y] = tuple(min(255, round(v + (255 - v) * lift)) for v in c) + (255,)

draw = ImageDraw.Draw(img)

# 背景幾何裝飾：低對比圓點與圓角塊，避免縮小後搶走品牌字樣。
draw.ellipse((70, 70, 270, 270), fill=(255, 232, 153, 90))
draw.ellipse((790, 760, 1045, 1015), fill=(88, 186, 177, 65))
# 不放空白膠囊或白色裝飾，避免被誤認成未完成的標籤。

text = "KUIES"
font = fit_font(text, 820, 280)
probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
box = probe.textbbox((0, 0), text, font=font, stroke_width=0)
tw, th = box[2] - box[0], box[3] - box[1]
# Arial Italic 的 bbox 左側與上方有偏移，依實際 bbox 置中。
x = (SIZE - tw) / 2 - box[0]
y = 365 - box[1]

# 柔和陰影增加 16px 圖示可讀性，禁止黑色硬外框。
shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
sd = ImageDraw.Draw(shadow)
sd.text((x + 15, y + 20), text, font=font, fill=(50, 105, 72, 72))
shadow = shadow.filter(ImageFilter.GaussianBlur(14))
img = Image.alpha_composite(img, shadow)
draw = ImageDraw.Draw(img)

# 保留原圖草綠字色，改用較深色確保小尺寸對比。
draw.text((x, y), text, font=font, fill=(102, 157, 55, 255))
# 字面高光，讓商標更活潑但仍保持平面設計。
draw.text((x - 2, y - 3), text, font=font, fill=(126, 178, 61, 255), stroke_width=1, stroke_fill=(126, 178, 61, 255))

# 右下保留原圖 2×2 線條裝飾意象，放大並簡化成可辨識品牌符號。
accent = (66, 158, 148, 230)
base_x, base_y, cell, gap = 696, 650, 82, 14
for row in range(2):
    for col in range(2):
        x0 = base_x + col * (cell + gap)
        y0 = base_y + row * (cell + gap)
        draw.rounded_rectangle((x0, y0, x0 + cell, y0 + cell), radius=15, outline=accent, width=10)
# 四格內用簡潔圓點、山形與笑臉，縮小後仍有節奏感。
draw.ellipse((base_x + 24, base_y + 22, base_x + 58, base_y + 56), outline=accent, width=9)
draw.polygon([
    (base_x + gap + cell + 18, base_y + 60),
    (base_x + gap + cell + 41, base_y + 23),
    (base_x + gap + cell + 66, base_y + 60),
], outline=accent)
draw.ellipse((base_x + 24, base_y + cell + gap + 24, base_x + 58, base_y + cell + gap + 58), fill=accent)
face_x = base_x + gap + cell
draw.ellipse((face_x + 22, base_y + cell + gap + 25, face_x + 34, base_y + cell + gap + 37), fill=accent)
draw.ellipse((face_x + 51, base_y + cell + gap + 25, face_x + 63, base_y + cell + gap + 37), fill=accent)
draw.arc((face_x + 22, base_y + cell + gap + 31, face_x + 64, base_y + cell + gap + 65), 15, 165, fill=accent, width=8)

# 左上小閃光，呼應短網址完成與清理感。
spark = (255, 221, 111, 245)
cx, cy = 205, 675
draw.polygon([(cx, cy - 58), (cx + 17, cy - 17), (cx + 58, cy), (cx + 17, cy + 17), (cx, cy + 58), (cx - 17, cy + 17), (cx - 58, cy), (cx - 17, cy - 17)], fill=spark)

img = img.convert("RGB")
SOURCE.parent.mkdir(parents=True, exist_ok=True)
img.save(SOURCE, quality=96)
for size in (16, 32, 48, 128):
    out = img.resize((size, size), Image.Resampling.LANCZOS)
    out.save(ICON_DIR / f"icon{size}.png", optimize=True)
    print(ICON_DIR / f"icon{size}.png")
print(SOURCE)
