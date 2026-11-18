"""Sleeps until a new .mkv lands in the anime folder, then runs agent.py on it.

Event-driven folder watch via watchdog, plus DKB release checks every 20 minutes.
A new file is processed once its size has stopped changing for 60 s (download/OneDrive done).
If the episode has no official Arabic yet, it is retried hourly for up to 2 days.
At logon it also picks up episodes that arrived while the PC was off.

Hardened: one instance only (named mutex); every error, including in threads, goes to
log.txt; a failing file never stops the loop; the folder watch is restarted if it dies;
if the whole process dies, Task Scheduler restarts it (non-zero exit / crash).
"""
import json
import os
import subprocess
import sys
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent  # noqa: E402
import dkb    # noqa: E402
import shows  # noqa: E402

sys.stderr = sys.stdout = open(agent.LOG, "a", encoding="utf-8", buffering=1)   # pythonw has no console
threading.excepthook = lambda a: agent.log(f"watcher thread crashed: {a.exc_type.__name__}: {a.exc_value}")

FOLDER = agent.FOLDER
pending = {}          # path -> earliest time to process
first_seen = {}
cv = threading.Condition()


def wanted(path):
    fn = os.path.basename(path)
    return fn.lower().endswith(".mkv") and ".tmp." not in fn


def needs_work(path):
    fn = os.path.basename(path)
    try:
        st = json.load(open(agent.STATE, encoding="utf-8")) if os.path.exists(agent.STATE) else {}
        if fn in st.get("copy_failed", {}):
            return True
        video_tracks = agent.tracks(path)
        if agent.has_arabic_audio(video_tracks):
            destination = agent.library_target(fn)
            return bool(destination and not os.path.exists(destination))
        if "Detective Conan" in fn:
            return False
        if any(t["properties"].get("track_name") == "العربية" for t in video_tracks):
            destination = agent.library_target(fn)
            return st.get("failed", {}).get(fn) != os.path.getsize(path) and bool(
                fn in st.get("delivery", {}) or destination and not os.path.exists(destination))
        return st.get("failed", {}).get(fn) != os.path.getsize(path)
    except Exception:
        return True                      # still being written: keep it queued


def queue(path, delay=0):
    with cv:
        pending[path] = time.time() + delay
        first_seen.setdefault(path, time.time())
        cv.notify()


class Handler(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory and wanted(e.src_path):
            queue(e.src_path)

    def on_moved(self, e):
        if (not e.is_directory and wanted(e.dest_path) and not wanted(e.src_path)
                and ".tmp." not in os.path.basename(e.src_path)):
            queue(e.dest_path)            # renamed into place (downloaders, but not our own .tmp swap)


def settled(path, label=None):
    """size unchanged for 60 s and the file can be opened for reading (counts down in the window)."""
    try:
        a = os.path.getsize(path)
        for left in range(60, 0, -5):
            if label:
                agent.show(label, f"Making sure the download is complete ({left}s)", 2 + (60 - left) / 20)
            time.sleep(5)
        if os.path.getsize(path) != a:
            return False
        open(path, "rb").close()
        return True
    except OSError:
        return False


def worker():
    while True:
        try:
            step()
        except Exception as e:            # one bad file must never stop the watcher
            agent.log(f"watcher error: {e!r}")
            time.sleep(30)


AGENT_TIMEOUT = 3 * 3600


def run_agent(path, timeout=None):
    """Run agent.py on exactly this file (audit #1). -> exit code, or None after a timeout, in which case
    the agent and every child it started (mkvmerge, ffmpeg, torrent.py) are killed (audit #6)."""
    import psutil
    pr = subprocess.Popen([sys.executable, os.path.join(HERE, "agent.py"), path, "--now"], creationflags=agent.NO_CF)
    try:
        return pr.wait(timeout=timeout or AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            procs = psutil.Process(pr.pid).children(recursive=True) + [psutil.Process(pr.pid)]
        except psutil.NoSuchProcess:
            procs = []
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=30)
        return None


def step():
    if True:
        with cv:
            while not pending or min(pending.values()) > time.time():
                cv.wait(None if not pending else max(1, min(pending.values()) - time.time()))
            path = min(pending, key=pending.get)
            del pending[path]
        if not os.path.exists(path):
            return
        label = None
        if needs_work(path):
            s, e = agent.parse_name(os.path.basename(path))
            label = f"{s} {e}" if s else os.path.basename(path)[:60]
        if not settled(path, label):
            queue(path, 30)
            return
        if not needs_work(path):
            first_seen.pop(path, None)
            return
        agent.log(f"watcher: new file {os.path.basename(path)}")
        code = run_agent(path)
        if code is None:
            if time.time() - first_seen.get(path, time.time()) < 2 * 86400:
                agent.log(f"watcher: agent timed out on {os.path.basename(path)} after 3 h; stopped it, retrying in 20 min")
                queue(path, 1200)
            else:                                 # same 2-day limit as every other retry: never block the queue forever
                agent.log(f"watcher: agent keeps timing out on {os.path.basename(path)}; giving up after 2 days")
                first_seen.pop(path, None)
            return
        if code:
            agent.log(f"watcher: agent exited with {code}")
        if needs_work(path) and time.time() - first_seen.get(path, time.time()) < 2 * 86400:
            queue(path, 1200)             # no Arabic yet / transient error: try again in 20 min
        else:
            first_seen.pop(path, None)


def single_instance():
    import ctypes
    h = ctypes.windll.kernel32.CreateMutexW(None, False, r"Local\AnimeFYA-watcher")
    if ctypes.windll.kernel32.GetLastError() == 183:    # ERROR_ALREADY_EXISTS
        sys.exit(0)
    return h


def main():
    global MUTEX
    MUTEX = single_instance()             # held for the process lifetime
    while not os.path.isdir(FOLDER):      # OneDrive may mount the folder late after logon
        time.sleep(30)
    for fn in os.listdir(FOLDER):         # arrived while the PC was off
        p = os.path.join(FOLDER, fn)
        if wanted(p) and needs_work(p):
            queue(p)
    w = threading.Thread(target=worker, daemon=True)
    w.start()
    threading.Thread(target=dkb.run, args=(FOLDER, queue), daemon=True).start()
    threading.Thread(target=shows.run_stremio, daemon=True).start()   # Stremio: second source for weekly episodes
    import status                         # http://localhost:8097 status page, lives with the watcher
    threading.Thread(target=status.serve, args=(lambda: dict(pending),), daemon=True).start()
    import cleanup                        # recycle weekly episodes 1 day after they were fully watched
    threading.Thread(target=cleanup.run, daemon=True).start()
    import library_processing
    threading.Thread(target=library_processing.watch, daemon=True).start()
    agent.log("watcher: started")
    while True:                           # restart the folder watch if it ever dies
        obs = Observer()
        try:
            obs.schedule(Handler(), FOLDER, recursive=False)
            obs.start()
            while obs.is_alive() and w.is_alive():
                obs.join(60)
        except Exception as e:
            agent.log(f"watcher: folder watch error {e!r}")
        finally:
            obs.stop()
        if not w.is_alive():
            agent.log("watcher: worker died, exiting so Task Scheduler restarts us")
            sys.exit(1)
        agent.log("watcher: folder watch stopped, restarting it")
        time.sleep(10)
        for fn in os.listdir(FOLDER) if os.path.isdir(FOLDER) else []:   # catch anything missed meanwhile
            p = os.path.join(FOLDER, fn)
            if wanted(p) and needs_work(p):
                queue(p)


def supervise():
    """Parent process: keeps the real watcher alive, relaunching it 10 s after it dies for any reason."""
    global MUTEX
    import ctypes
    MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, r"Local\AnimeFYA-supervisor")
    if ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)
    while True:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *sys.argv[1:], "--child"],
                           creationflags=agent.NO_CF)
        agent.log(f"supervisor: watcher exited ({r.returncode}), restarting in 10 s")
        time.sleep(10)


if __name__ == "__main__":
    main() if "--child" in sys.argv else supervise()
