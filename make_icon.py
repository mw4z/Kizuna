"""Build the Kizuna app icons from icon-source.png (the ribbon K artwork):
app.png (window/taskbar icon and top-bar logo, served by status.py) and app.ico (desktop shortcut).
The rounded square is found by brightness, cropped, and everything outside its rounded corners made transparent."""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
src = Image.open(os.path.join(HERE, "icon-source.png")).convert("RGB")
box = src.convert("L").point(lambda v: 255 if v > 22 else 0).getbbox()   # the tile is lighter than the black backdrop
assert box, "no icon tile found"
side = max(box[2] - box[0], box[3] - box[1])
tile = src.crop(box).resize((side, side), Image.LANCZOS).resize((1024, 1024), Image.LANCZOS).convert("RGBA")
mask = Image.new("L", (4096, 4096), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, 4095, 4095), radius=int(4096 * 0.215), fill=255)
tile.putalpha(mask.resize((1024, 1024), Image.LANCZOS))
tile.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "app.png"), optimize=True)
tile.save(os.path.join(HERE, "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok", box)
