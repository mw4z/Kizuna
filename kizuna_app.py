"""Entry point of the packaged Kizuna.exe (PyInstaller, see installer/).

    Kizuna.exe                    start Kizuna (the watcher supervisor: folder watch, pages, queue, cleanup)
    Kizuna.exe --open             open the Kizuna window (Edge app mode, falls back to the default browser)
    Kizuna.exe <script>.py args   run one of Kizuna's own bundled scripts: this is how Kizuna starts its helper
                                  processes (agent.py, processing_worker.py, torrent.py, watcher.py --child), since
                                  they all launch [sys.executable, <script path>, ...]. Only scripts shipped next to
                                  this file can run, never a path from elsewhere.
"""
import os
import runpy
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://localhost:8097/"


def bundled(name):
    path = os.path.join(HERE, os.path.basename(name))
    if not (path.lower().endswith(".py") and os.path.isfile(path)):
        raise SystemExit(f"Kizuna: unknown script {os.path.basename(name)!r}")
    return path


def open_window():
    for edge in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                 os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if os.path.exists(edge):
            subprocess.Popen([edge, f"--app={URL}", "--window-size=1180,900"])
            return
    webbrowser.open(URL)


def main(argv):
    if argv and argv[0].lower().endswith(".py"):
        script = bundled(argv[0])
        sys.argv = [script, *argv[1:]]
        sys.path.insert(0, HERE)
        runpy.run_path(script, run_name="__main__")
    elif argv[:1] == ["--open"]:
        open_window()
    else:
        sys.argv = [bundled("watcher.py"), *argv]
        sys.path.insert(0, HERE)
        runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
