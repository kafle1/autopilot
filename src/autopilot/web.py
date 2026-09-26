"""The dashboard and its API. Listens on this computer only; phones reach it through Tailscale."""
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import urllib.parse
from importlib import resources
from pathlib import Path

from . import engines, runner, spec

TEXT = {".md", ".txt", ".csv", ".json", ".toml"}
MAX = 1 << 20
UPLOAD = 36 << 20  # 25 MB of attachments once base64 grows them by a third
MANIFEST = json.dumps({"name": "Autopilot", "short_name": "Autopilot", "start_url": "/", "display": "standalone",
                       "background_color": "#ffffff", "theme_color": "#ffffff",
                       "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Pairing:
    """One-time 6-digit codes that sign a browser in. Five wrong tries cancel them all."""

    def __init__(self):
        self.mu, self.codes, self.wrong = threading.Lock(), {}, 0

    def new(self):
        with self.mu:
            code, expires = f"{secrets.randbelow(10 ** 6):06d}", time.time() + 600
            self.codes[code], self.wrong = expires, 0
            return code, expires

    def use(self, code):
        with self.mu:
            now = time.time()
            self.codes = {c: e for c, e in self.codes.items() if e > now}
            match = next((c for c in self.codes if hmac.compare_digest(c, str(code))), None)
            if match:
                del self.codes[match]
                return True
            self.wrong += 1
            if self.wrong >= 5:
                self.codes.clear()
            return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "autopilot"
    timeout = 60  # a client that opens a connection and goes quiet would hold a thread forever
    daemon = None
    pairing = Pairing()

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json", extra=()):
        if ctype == "application/json":
            body = json.dumps(body)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def remote(self):
        host = spec.config().get("remote_host")
        return host if host and host in (self.headers.get("Host"), self.headers.get("X-Forwarded-Host")) else None

    def host_ok(self):
        # the Host check stops other websites from reaching this server through DNS tricks
        p, host = spec.port(), spec.config().get("remote_host")
        allowed = {f"127.0.0.1:{p}", f"localhost:{p}"} | ({host} if host else set())
        fwd = self.headers.get("X-Forwarded-Host")
        return self.headers.get("Host") in allowed and (fwd is None or fwd in allowed)

    def signed_in(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        return "ap" in jar and hmac.compare_digest(jar["ap"].value, spec.secret())

    def cookie(self):
        secure = "; Secure" if self.remote() else ""
        return ("Set-Cookie", f"ap={spec.secret()}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000{secure}")

    def handle_any(self, method):
        try:
            if not self.host_ok():
                raise Fail(403, "wrong address")
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            body = self.body(UPLOAD if self.signed_in() else MAX) if method == "POST" else None
            route = (method, url.path)
            if route == ("GET", "/"):
                return self.reply(200, resources.files("autopilot").joinpath("ui.html").read_bytes(), "text/html")
            if route == ("GET", "/icon.svg"):
                return self.reply(200, resources.files("autopilot").joinpath("icon.svg").read_bytes(), "image/svg+xml")
            if route == ("GET", "/manifest.json"):
                return self.reply(200, MANIFEST, "application/manifest+json")
            if route == ("GET", "/pair"):
                ok = self.pairing.use(q.get("code", ""))
                return self.reply(302, "", "text/plain", [("Location", "/")] + ([self.cookie()] if ok else []))
            if route == ("POST", "/api/pair"):
                if not self.pairing.use(body.get("code", "")):
                    raise Fail(403, "that code did not work. Make a new one on the computer")
                return self.reply(200, {"ok": True}, extra=[self.cookie()])
            if not url.path.startswith("/api/"):
                raise Fail(404, "not found")
            if not self.signed_in():
                raise Fail(401, "sign in")
            return self.reply(200, *self.api(route, q, body))
        except Fail as e:
            self.reply(e.code, {"error": str(e)})
        except spec.SpecError as e:
            self.reply(500, {"error": str(e)})
        except Exception:
            from .daemon import log
            log(traceback.format_exc())
            self.reply(500, {"error": "autopilot hit an error, see ~/autopilot/.run/daemon.log"})

    def body(self, limit):
        # the custom header forces browsers to ask first, so other sites can't post here
        if self.headers.get("X-Autopilot") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
            raise Fail(403, "missing X-Autopilot header")
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Fail(400, "bad Content-Length") from None
        if not 0 <= size <= limit + 4096:  # a negative size would read until the client hangs up
            raise Fail(413, "too big")
        try:
            data = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            raise Fail(400, "the body must be JSON") from None
        if not isinstance(data, dict):
            raise Fail(400, "the body must be a JSON object")
        return data

    def api(self, route, q, body):
        d = self.daemon
        match route:
            case ("GET", "/api/state"):
                return (d.snapshot(),)
            case ("GET", "/api/job"):
                folder = d.folder(q.get("name"))
                md = folder / "autopilot.md"
                return ({"name": folder.name, "text": md.read_text(encoding="utf-8") if md.exists() else "",
                         "files": self.files(folder, d.jobs[folder.name].job), "runs": runner.runs(folder.name),
                         "asked": runner.asked(folder)},)
            case ("GET", "/api/log"):
                logs = d.folder(q.get("name")) / "logs"
                run = q.get("run")
                if run and not re.fullmatch(runner.RUN_ID, run):
                    raise Fail(400, "bad run id")
                found = sorted(logs.glob("*.log")) if logs.is_dir() else []
                path = logs / f"{run}.log" if run else (found[-1] if found else None)
                if not path or not path.exists():
                    return ("No runs yet.", "text/plain")
                with open(path, "rb") as f:
                    f.seek(max(0, path.stat().st_size - (256 << 10)))
                    return (f.read(), "text/plain")
            case ("GET", "/api/file"):
                path = self.resolve(q)
                if not path.is_file() or path.stat().st_size > MAX:
                    raise Fail(404, "that file is missing or too big to open here")
                return (path.read_bytes(), "text/plain")
            case ("POST", "/api/file"):
                path, text = self.resolve(body), body.get("text")
                if path.suffix.lower() not in TEXT or not isinstance(text, str) or len(text.encode()) > MAX:
                    raise Fail(400, "only text files under 1 MB can be saved here")
                tmp = path.with_name(f".{path.name}.tmp")  # a failed write must not leave the file cut in half
                tmp.write_text(text, encoding="utf-8")
                if path.exists():
                    shutil.copymode(path, tmp)
                os.replace(tmp, path)
                return ({"ok": True},)
            case ("POST", "/api/save"):
                d.save_spec(body.get("name"), body.get("text"))
                return ({"ok": True},)
            case ("POST", "/api/action"):
                d.action(body.get("name"), body.get("action"))
                return ({"ok": True},)
            case ("POST", "/api/update"):
                if not d.update:
                    raise Fail(400, "you already have the newest version")
                d.upgrade()
                return ({"ok": True},)
            case ("POST", "/api/pause_all"):
                d.pause_all(body.get("paused"))
                return ({"ok": True},)
            case ("POST", "/api/build"):
                return ({"name": d.build(body.get("instruction"), body.get("name"), body.get("new_name"), body.get("files"), body.get("dashboard"))},)
            case ("POST", "/api/engines"):
                order = body.get("order")
                if not isinstance(order, list) or not order or len(set(order)) != len(order) or not set(order) <= set(engines.ORDER):
                    raise Fail(400, "pick one or more of " + ", ".join(engines.ORDER))
                cfg = spec.config()
                cfg["engines"] = order
                spec.save_config(cfg)
                return ({"ok": True},)
            case ("POST", "/api/pair/new"):
                code, expires = self.pairing.new()
                host = spec.config().get("remote_host")
                return ({"code": code, "expires": expires, "url": f"https://{host}" if host else None},)
        raise Fail(404, "not found")

    @staticmethod
    def files(folder, job):
        out = []
        for where, base in (("folder", folder), ("dir", job.dir if job and job.dir != folder else None)):
            if base:
                try:
                    out += [{"where": where, "path": p.name, "size": p.stat().st_size} for p in sorted(base.iterdir())
                            if p.is_file() and not p.name.startswith(".") and p.name != "autopilot.md"]
                except OSError:
                    pass
        return out

    def resolve(self, q):
        folder = self.daemon.folder(q.get("name"))
        job = self.daemon.jobs[folder.name].job
        base = {"folder": folder, "dir": job.dir if job else None}.get(q.get("where"))
        rel = q.get("path")
        if not base or not isinstance(rel, str) or not rel:
            raise Fail(400, "bad file")
        base = base.resolve()
        path = (base / rel).resolve()
        if not path.is_relative_to(base) or path == base or "logs" in path.relative_to(base).parts[:1]:
            raise Fail(403, "that file is outside this autopilot")
        return path

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


def serve(daemon):
    Handler.daemon = daemon
    from .daemon import log
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", spec.port()), Handler)
    except OSError as e:  # nothing can show a problem while the dashboard is down, so the log is where setup looks
        log(f"The dashboard can't start: port {spec.port()} is taken ({e.strerror}). Set another port in {spec.CONFIG}")
        return
    except spec.SpecError as e:
        log(f"The dashboard can't start: {e}")
        return
    server.daemon_threads = True
    server.serve_forever()
