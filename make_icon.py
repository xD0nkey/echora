"""Generate echora.ico (same design as the tray icon: dot + echo waves)."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse((8, 8, S - 8, S - 8), fill=(59, 130, 246))
c = S // 2
for r in (40, 72, 104):
    d.arc((c - r, c - r, c + r, c + r), -50, 50, fill="white", width=16)
d.ellipse((56, 104, 104, 152), fill="white")
img.save("echora.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
