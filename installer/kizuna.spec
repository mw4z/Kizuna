# PyInstaller recipe for Kizuna.exe (one folder). Build from the repository root:
#     python -m PyInstaller installer/kizuna.spec --noconfirm
# The scripts are bundled as source too: Kizuna starts its helper processes as "Kizuna.exe <script>.py ..."
# (see kizuna_app.py), and the pages, fonts and icons are served from the same folder.
import glob
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
scripts = [p for p in glob.glob(os.path.join(ROOT, "*.py")) if not os.path.basename(p).startswith("test_")]
data = [(p, ".") for p in scripts]
data += [(p, ".") for pattern in ("*.html", "*.css", "kizuna*.js", "app.png", "app.ico", "subtitle_policy.example.json")
         for p in glob.glob(os.path.join(ROOT, pattern))]
data += [(p, "fonts") for p in glob.glob(os.path.join(ROOT, "fonts", "*.ttf"))]
data += [(p, os.path.join("fonts", "licenses")) for p in glob.glob(os.path.join(ROOT, "fonts", "licenses", "*.txt"))]
modules = [os.path.splitext(os.path.basename(p))[0] for p in scripts if os.path.basename(p) != "kizuna_app.py"]

a = Analysis([os.path.join(ROOT, "kizuna_app.py")], pathex=[ROOT], datas=data,
             hiddenimports=modules + collect_submodules("winrt") + ["libtorrent", "cv2", "numpy", "psutil", "watchdog"],
             excludes=["tkinter", "matplotlib", "PIL.ImageQt"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Kizuna", icon=os.path.join(ROOT, "app.ico"),
          console=False, version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Kizuna")
