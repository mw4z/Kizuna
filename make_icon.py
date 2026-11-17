"""Draw the Anime FYA app icon -> app.ico (shortcut) + app.png (window/taskbar icon served by status.py)."""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
grad = Image.new("RGBA", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        px[x, y] = (int(36 + (91 - 36) * t), int(180 + (110 - 180) * t), int(160 + (255 - 160) * t), 255)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((16, 16, S - 16, S - 16), 112, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
d.polygon([(196, 130), (196, 310), (346, 220)], fill=(255, 255, 255, 255))          # play
for i, (w, a) in enumerate(((300, 255), (210, 200))):                                # subtitle lines
    y = 362 + i * 52
    d.rounded_rectangle(((S - w) / 2, y, (S + w) / 2, y + 26), 13, fill=(255, 255, 255, a))
img.save(os.path.join(HERE, "app.png"))
img.save(os.path.join(HERE, "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok")
