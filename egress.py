"""Filtering forward proxy for every connection ffmpeg/ffprobe/urllib make to add-on media (audit #8).

A source URL is only checked once by stremio.safe_url, but ffmpeg then follows redirects, reads HLS
playlists, fetches every segment and AES key, and resolves DNS again by itself. Routing all of that
through this proxy checks each connection at the moment it is made:

  * the proxy resolves the host itself, requires EVERY address to be public (ipaddress.is_global),
    then connects to exactly that address, so a DNS answer that changes between check and connect
    (rebinding) can't reach the LAN;
  * redirects, playlists, segments and keys are new requests, so each one passes the same check;
  * the only non-public target allowed is Stremio's local torrent server 127.0.0.1:11470.

ffmpeg uses it via the http_proxy environment variable (plain http: absolute-URI requests; https: the
tls protocol tunnels with CONNECT through "httpproxy"). urllib uses it through AlwaysProxy.
"""
import ipaddress
import os
import re
import socket
import socketserver
import threading
import time
import urllib.parse
import urllib.request

LOCAL_ALLOWED = {("127.0.0.1", 11470), ("localhost", 11470)}
MAX_HEAD = 16384


def local_media_path(path, query=""):
    """Stremio's server also offers URL-forwarding endpoints (proxy, HLS of any mediaURL) that could reach the LAN;
    only its plain torrent file stream /<infohash>/<file index> is ever needed."""
    return not query and re.fullmatch(r"/[0-9a-fA-F]{40}/\d+", path or "") is not None


class Denied(Exception):
    pass


def default_policy(host, port):
    """-> IP address to connect to, or raise Denied. Resolves once; the caller connects to this IP."""
    host = host.strip("[]").lower()
    if (host, port) in LOCAL_ALLOWED:
        return "127.0.0.1"
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise Denied(f"cannot resolve {host}") from None
    ips = [i[4][0] for i in infos]
    if not ips or any(not ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips):
        raise Denied(f"{host}:{port} is not a public address")
    return ips[0]


def _read_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise Denied("connection closed before the request head")
        data += chunk
        if len(data) > MAX_HEAD:
            raise Denied("request head too large")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("latin-1").split("\r\n"), rest


IDLE = 300          # a tunnel closes only after this long with no data in EITHER direction


def _pipe(a, b, seen):
    """Copy a -> b. A download leaves the client->server side silent for hours, so a recv timeout there only ends
    the tunnel when the other direction has been quiet too (seen[0]: last time any data moved). A peer that stops
    reading still ends it: sendall gives up after IDLE seconds."""
    try:
        while True:
            try:
                chunk = a.recv(65536)
            except TimeoutError:
                if time.monotonic() - seen[0] < IDLE:
                    continue
                break
            if not chunk:
                break
            seen[0] = time.monotonic()
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(60)
        try:
            lines, rest = _read_head(client)
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":                          # https (and anything tunnelled)
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port)
                if (host.lower(), port) in LOCAL_ALLOWED:
                    raise Denied("no tunnels to Stremio's local server")
                ip = self.server.policy(host, port)
                upstream = socket.create_connection((ip, port), timeout=30)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:                                                    # plain http: absolute-URI request
                u = urllib.parse.urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    raise Denied("only absolute http:// requests are proxied")
                port = u.port or 80
                if (u.hostname.lower(), port) in LOCAL_ALLOWED and not local_media_path(u.path, u.query):
                    raise Denied("only torrent file streams on Stremio's local server")
                ip = self.server.policy(u.hostname, port)
                upstream = socket.create_connection((ip, port), timeout=30)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:"))]
                upstream.sendall("\r\n".join([f"{method} {path} {version}"] + headers + ["Connection: close", "", ""])
                                 .encode("latin-1") + rest)
        except (Denied, ValueError, OSError) as e:
            self.server.denied.append(str(e))
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        client.settimeout(IDLE)
        upstream.settimeout(IDLE)
        seen = [time.monotonic()]
        t = threading.Thread(target=_pipe, args=(upstream, client, seen), daemon=True)
        t.start()
        _pipe(client, upstream, seen)
        t.join(IDLE + 10)
        upstream.close()


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, policy=default_policy):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.policy = policy
        self.denied = []                       # recent refusals (tests read this; capped below)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


_proxy = None
_lock = threading.Lock()


def proxy():
    """The process-wide filtering proxy (started on first use, 127.0.0.1 on a random port)."""
    global _proxy
    with _lock:
        if _proxy is None:
            _proxy = Proxy()
        del _proxy.denied[:-50]
        return _proxy


def ffmpeg_env(p=None):
    """Environment for ffmpeg/ffprobe: every http/https connection goes through the proxy."""
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "no_proxy")}
    env["http_proxy"] = (p or proxy()).url
    return env


# Protocols ffmpeg may use. httpproxy is what tls uses to tunnel through the proxy; no file/udp/rtmp/etc.
PROTOCOLS = "http,https,tcp,tls,crypto,httpproxy"


class AlwaysProxy(urllib.request.BaseHandler):
    """urllib handler that sends every http/https request through the proxy (no OS bypass list).

    Works like urllib's own ProxyHandler: the proxy is set at OPEN time, after the Host header was taken
    from the real URL (setting it earlier sent "Host: 127.0.0.1:<proxy>" and servers answered 400)."""
    handler_order = 100                          # runs before HTTPHandler/HTTPSHandler open the connection

    def __init__(self, p=None):
        self.p = p

    def _open(self, req):
        if not getattr(req, "_via_egress", False):
            req._via_egress = True
            req.set_proxy((self.p or proxy()).url.removeprefix("http://"), "http")
        return None                              # let the normal HTTP(S) handler connect (to the proxy)

    http_open = https_open = _open
