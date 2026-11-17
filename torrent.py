"""Download a torrent with libtorrent, logging peers/progress every 30 s.

Usage: torrent.py file.torrent outdir [max_minutes]
"""
import sys
import time

import libtorrent as lt

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://nyaa.tracker.wf:7777/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
]

ses = lt.session({"listen_interfaces": "0.0.0.0:6881,[::]:6881",
                  "enable_dht": True, "enable_lsd": True,
                  # never ask the router to forward ports (Tenda sits in the STC DMZ): outgoing peers suffice
                  "enable_upnp": False, "enable_natpmp": False,
                  "dht_bootstrap_nodes": "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881,dht.libtorrent.org:25401"})
ti = lt.torrent_info(sys.argv[1])
p = lt.add_torrent_params()
p.ti = ti
p.save_path = sys.argv[2]
p.trackers = TRACKERS
import re
pat = re.compile(sys.argv[4]) if len(sys.argv) > 4 else None
if pat:
    fs = ti.files()
    p.file_priorities = [4 if pat.search(fs.file_path(i)) else 0 for i in range(fs.num_files())]
    print("selected", sum(1 for x in p.file_priorities if x), "files", flush=True)
h = ses.add_torrent(p)
limit = float(sys.argv[3]) * 60 if len(sys.argv) > 3 else 1e12
t0 = time.time()
while True:
    s = h.status()
    print(f"{(time.time()-t0)/60:6.1f} min  {s.progress*100:5.1f}%  peers {s.num_peers} "
          f"seeds {s.num_seeds} (swarm seeds {s.list_seeds}, peers {s.list_peers})  "
          f"{s.download_rate/1e6:.2f} MB/s  ", flush=True)
    if s.is_seeding or s.progress >= 1 or (pat and s.state == lt.torrent_status.finished):
        print("DONE", flush=True)
        break
    if time.time() - t0 > limit:
        print("TIMEOUT", flush=True)
        sys.exit(1)  # A partial torrent must never be delivered as a completed episode.
    time.sleep(30)
