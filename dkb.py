"""Fetch new weekly DKB episodes; only completed files enter the watched folder."""
import glob
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import agent
import shows

SHOWS = None        # tests may pin a tuple; normally the DKB-enabled shows in shows.json (edited on the page)
STATE = os.path.join(agent.HERE, "dkb-state.json")


def names():
    return SHOWS if SHOWS is not None else shows.dkb_names()


def episode(title):
    name, ep = agent.parse_name(title)
    show = next((s for s in names() if name and re.match(re.escape(s) + r"(?:$|[ :])", name)), None)
    return (show, ep) if show else (None, None)


def number(ep):
    return tuple(map(int, re.fullmatch(r"S(\d+)E(\d+)", ep).groups()))


class ReleaseLinks(HTMLParser):
    def __init__(self, followed_only=True):
        super().__init__()
        self.torrent = None
        self.releases = []
        self.followed_only = followed_only

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if re.fullmatch(r"https://nyaa\.si/download/\d+\.torrent", href):
            self.torrent = href
        elif href.startswith("magnet:?"):
            title = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("dn", [""])[0]
            if self.torrent and title.startswith("[DKB] ") and "[1080p]" in title and "[weekly]" in title:
                if not self.followed_only or episode(title)[0]:
                    self.releases.append((title, self.torrent))
            self.torrent = None
        else:
            self.torrent = None


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "AnimeFYA/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        if urllib.parse.urlsplit(response.url).hostname != urllib.parse.urlsplit(url).hostname:
            raise ValueError("unexpected download redirect")
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("response too large")
        return data


def listing(show):
    # shortcut: only DKB's recent results are checked; add pagination for long offline gaps.
    query = urllib.parse.urlencode({"w": 1, "query": show})
    parser = ReleaseLinks()
    parser.feed(fetch("https://dkb.lowpolyfire.com/index/releases?" + query).decode("utf-8"))
    found = [(t, u) for t, u in parser.releases if episode(t)[0] == show]
    if not found:
        raise ValueError(f"DKB returned no recognizable weekly releases for {show}")
    return found


def search_titles(name):
    """Every recent DKB 1080p weekly release title matching `name` (used to link a new show)."""
    parser = ReleaseLinks(followed_only=False)
    parser.feed(fetch("https://dkb.lowpolyfire.com/index/releases?" + urllib.parse.urlencode({"w": 1, "query": name}))
                .decode("utf-8"))
    return [t for t, _ in parser.releases]


def save(state):
    agent.write_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=2))   # read by other threads too


def candidates(state, show, releases):
    # Persist the initial snapshot before any downloads: installation never backfills.
    if show not in state["baseline"]:
        followed = shows.match(show) or {}
        if followed.get("from"):                    # the user picked a start episode on the page
            state["baseline"][show] = shows.tag(followed.get("season", 1), followed["from"] - 1)
        else:
            state["baseline"][show] = max((episode(t)[1] for t, _ in releases), key=number)
            agent.log(f"DKB: following {show} after {state['baseline'][show]} (no backfill)")
            return []
        agent.log(f"DKB: following {show} after {state['baseline'][show]}")
    return sorted(((t, u) for t, u in releases
                   if number(episode(t)[1]) > number(state["baseline"][show])
                   and f"{show}|{episode(t)[1]}" not in state["done"]),
                  key=lambda item: number(episode(item[0])[1]))


def existing(folder, wanted):
    paths = glob.glob(os.path.join(folder, "*.mkv"))
    paths += glob.glob(os.path.join(agent.LIBRARY, "*", "Season *", "*.mkv"))
    return next((p for p in paths if ".tmp." not in os.path.basename(p)
                 and episode(os.path.basename(p)) == wanted), None)


def torrent_file(ti, title):
    files = ti.files()
    if files.num_files() != 1:
        raise ValueError("expected a single-episode torrent")
    name = files.file_path(0)
    if (not name or name in (".", "..") or any(c in name for c in '<>:"/\\|?*')
            or name.endswith((" ", ".")) or not name.lower().endswith(".mkv")
            or not name.startswith("[DKB] ") or "[1080p]" not in name
            or episode(name) != episode(title) or files.file_size(0) <= 0 or files.symlink(0)):
        raise ValueError("unsafe or mismatched torrent filename")
    return name, files.file_size(0)


def download(title, url, folder):
    import libtorrent as lt

    if not re.fullmatch(r"https://nyaa\.si/download/\d+\.torrent", url):
        raise ValueError("unexpected torrent URL")
    data = fetch(url)
    ti = lt.torrent_info(lt.bdecode(data))
    name, size = torrent_file(ti, title)
    stage = os.path.join(agent.WORK, "dkb", url.rsplit("/", 1)[1][:-8])
    os.makedirs(stage, exist_ok=True)
    os.makedirs(folder, exist_ok=True)
    if shutil.disk_usage(stage).free < size * 2.1 + 500e6 or shutil.disk_usage(folder).free < size * 1.1 + 500e6:
        raise RuntimeError("not enough disk space for download and delivery")
    tf = os.path.join(stage, "episode.torrent")
    Path(tf).write_bytes(data)
    agent.log(f"DKB: downloading {title}")
    with open(agent.LOG, "a", encoding="utf-8") as log:
        subprocess.run([sys.executable, os.path.join(agent.HERE, "torrent.py"), tf, stage, "40", r"\.mkv$"],
                       stdout=log, stderr=log, creationflags=agent.NO_CF, timeout=45 * 60, check=True)
    source = os.path.join(stage, name)
    if os.path.getsize(source) != size or not any(t["type"] == "video" for t in agent.tracks(source)):
        raise RuntimeError("download verification failed")
    dst = os.path.join(folder, name)
    if not os.path.exists(dst):
        shutil.copy2(source, dst + ".part")
        try:
            os.rename(dst + ".part", dst)  # Windows refuses to overwrite an existing episode.
        except FileExistsError:
            os.remove(dst + ".part")
    shutil.rmtree(stage)
    agent.log(f"DKB: delivered {name}")
    return dst


def poll(folder, ready, initialize_only=False):
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)  # A damaged ledger must fail closed, never reset the baseline.
    else:
        state = {"baseline": {}, "done": {}}
    resets = shows.take_resets("dkb")
    if resets:
        for release in resets:                    # season or start episode changed on the page, or show re-added
            state["baseline"].pop(release, None)
        save(state)
    for show in names():
        try:
            releases = listing(show)
            pending = candidates(state, show, releases)
            save(state)
            agent.log(f"DKB: {show}: {len(pending)} new release(s); baseline {state['baseline'][show]}")
            if initialize_only:
                continue
            for title, url in pending:
                wanted = episode(title)
                key = "|".join(wanted)
                if key in state["done"]:
                    continue
                path = existing(folder, wanted)
                followed = shows.match(show)
                if not path and followed and shows.have_episode(followed, wanted[1]):
                    state["done"][key] = "from another source"      # Stremio was faster
                    save(state)
                    continue
                if not path and not shows.claim(show, wanted[1], "dkb"):
                    continue                                        # Stremio is fetching it right now
                try:
                    path = path or download(title, url, folder)
                    state["done"][key] = os.path.basename(path)
                    save(state)
                    if os.path.dirname(path) == folder:
                        ready(path)
                except Exception as e:
                    agent.log(f"DKB: retry next check for {title}: {e!r}")
                finally:
                    shows.release_claim(show, wanted[1])
        except Exception as e:
            agent.log(f"DKB: {show} check failed: {e!r}")
    return state


def run(folder, ready):
    while True:
        try:
            poll(folder, ready)
        except Exception as e:
            agent.log(f"DKB: check failed: {e!r}")
        import settings
        shows.wake_dkb.wait(settings.get("check_minutes") * 60)   # "Check now" on the page wakes it early
        shows.wake_dkb.clear()


if __name__ == "__main__":
    if sys.argv[1:] != ["--initialize"]:
        raise SystemExit("Usage: dkb.py --initialize (record current releases without downloading)")
    state = poll(agent.FOLDER, lambda path: None, initialize_only=True)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if any(show not in state["baseline"] for show in names()):
        raise SystemExit(1)
