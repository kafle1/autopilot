"""The background program: starts runs on time, keeps services up, serves the dashboard."""
import base64
import json
import os
import re
import platform
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import engines, proc, runner, spec, web

TICK = 5
ATTACH_MAX = 25 << 20
STATE = spec.RUN / "state.json"
DASHBOARD = ("The owner wants their own web page for it, to see what it did and control it. Serve a small, clean page from "
             "127.0.0.1 on a free port with an always-on program and put its address in url. If this autopilot itself runs on "
             "a schedule, keep it that way and add a second autopilot in a new folder next to this one, named like {name}-dashboard "
             "(at most 50 lowercase letters, digits and dashes, and not a folder that exists). Give it keepalive = true and "
             "run = the command that serves the page, make the page show the files this one writes, and put the same url in both.")


def log(text):
    with open(spec.RUN / "daemon.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


@dataclass
class Entry:
    mtime: int = 0
    job: spec.Job | None = None  # the last version that could be read
    error: str | None = None


class Daemon:
    def __init__(self):
        self.mu = threading.RLock()
        self.jobs = {}
        self.gone = set()  # being deleted, so nothing starts them again
        self.backoff = {}
        self.problems = []
        self.beat = time.time()
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        self.slot, self.paused, self.paused_all = s.get("slot", {}), set(s.get("paused", [])), s.get("paused_all", False)

    def save(self):
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"slot": self.slot, "paused": sorted(self.paused), "paused_all": self.paused_all}), encoding="utf-8")
        os.replace(tmp, STATE)

    def scan(self):
        seen = {}
        for d in spec.HOME.iterdir():
            if not d.is_dir() or not spec.NAME.fullmatch(d.name):
                continue
            try:
                mtime = (d / "autopilot.md").stat().st_mtime_ns
            except OSError:
                if os.path.exists(d / ".instruction"):  # the AI is still writing it, or failed to
                    seen[d.name] = Entry()
                continue
            old = self.jobs.get(d.name)
            if old and old.mtime == mtime and not old.error:
                seen[d.name] = old
                continue
            try:
                seen[d.name] = Entry(mtime, spec.load(d))
            except (OSError, UnicodeDecodeError, spec.SpecError) as e:
                seen[d.name] = Entry(mtime, old.job if old else None, str(e))
        self.jobs = seen

    def tick(self):
        now = time.time()
        with self.mu:
            self.scan()
            for name, e in self.jobs.items():
                if name in self.gone or not e.job:
                    continue
                info = runner.lock(name).held()
                if info is not None and info.get("trigger") == "build":
                    continue
                paused = self.paused_all or name in self.paused
                if e.job.keepalive:
                    self.keep(name, info is not None, paused, now)
                    continue
                if not (e.job.every or e.job.crons):
                    continue
                slot = self.slot.get(name, 0 if e.job.every else None)
                if slot is None:  # a new schedule counts from now, so it doesn't fire for times already past
                    self.slot[name] = now
                    self.save()
                elif e.job.next_after(slot) <= now:  # missed runs while asleep fold into one catch-up
                    if info is None and not paused:
                        runner.start(name, "schedule")
                    self.slot[name] = now
                    self.save()

    def keep(self, name, running, paused, now):
        b = self.backoff.setdefault(name, {"delay": 0, "down": None, "up": None})
        if running or paused:
            b["down"] = None
            return
        if b["down"] is None:
            b["down"] = now
            if b["up"] is not None:  # a service that keeps crashing gets restarted less and less often
                b["delay"] = 5 if now - b["up"] >= 60 else min(max(b["delay"] * 2, 5), 300)
        if now - b["down"] >= b["delay"]:
            runner.start(name, "service")
            b["up"], b["down"] = now, None

    def next_run(self, name, job):
        if job.keepalive or self.paused_all or name in self.paused or not (job.every or job.crons):
            return None
        slot = self.slot.get(name)
        return time.time() if slot is None and job.every else job.next_after(slot or time.time())

    def snapshot(self):
        with self.mu:
            jobs = []
            for name, e in sorted(self.jobs.items()):
                if name in self.gone:
                    continue
                info = runner.lock(name).held()
                last = (runner.runs(name, 1) or [None])[0]
                job, paused = e.job, name in self.paused or self.paused_all
                error = e.error or (None if job or info is not None else (last or {}).get("summary") or "autopilot.md is missing")
                status = ("running" if info is not None else "paused" if paused else "broken" if error
                          else {"ok": "ok", "failed": "failed", "timeout": "failed"}.get((last or {}).get("status"), "idle"))
                jobs.append({"name": name, "kind": job.kind if job else "ai", "status": status,
                             "building": bool(info) and info.get("trigger") == "build",
                             "trigger": job.trigger if job else "by hand", "next": self.next_run(name, job) if job else None,
                             "running_since": (info or {}).get("started"), "last": last, "error": error,
                             "url": job.url if job else None, "about": job.about if job else "", "paused": name in self.paused})
            cfg = spec.config()
            names = engines.order()
            host = cfg.get("remote_host")
            return {
                "device": {"name": platform.node().removesuffix(".local"), "os": spec.OS,
                           "remote_url": f"https://{host}" if host else None, "paused_all": self.paused_all,
                           "problems": self.problems + ([] if any(engines.find(n) for n in engines.ORDER) else
                                                        ["No AI tool is installed. Install Claude Code, Codex or opencode, then run: autopilot setup"])},
                "engines": [{"name": n, "found": bool(engines.find(n))} for n in names + [n for n in engines.ORDER if n not in names]],
                "alerts": {"subscribe_url": f"https://ntfy.sh/{cfg['ntfy_topic']}" if cfg.get("ntfy_topic") else None},
                "jobs": jobs,
            }

    def folder(self, name):
        if not isinstance(name, str) or name not in self.jobs or name in self.gone:
            raise web.Fail(404, f"there is no autopilot called {name!r}")
        return spec.HOME / name

    def action(self, name, what):
        folder = self.folder(name)
        job = self.jobs[name].job
        if what == "run":
            if runner.lock(name).held() is not None:
                raise web.Fail(409, "it is already running")
            if not job:
                raise web.Fail(400, self.jobs[name].error or "it has no autopilot.md yet")
            runner.start(name, "service" if job.keepalive else "manual")
        elif what in ("stop", "restart"):
            runner.stop(name)
            with self.mu:
                self.backoff.pop(name, None)  # a service comes back on the next tick
            if what == "restart" and job and not job.keepalive:
                runner.start(name, "manual")
        elif what in ("pause", "resume"):
            with self.mu:
                (self.paused.add if what == "pause" else self.paused.discard)(name)
                self.backoff.pop(name, None)
                self.save()
            if what == "pause":
                runner.stop(name)
        elif what == "delete":
            with self.mu:
                self.gone.add(name)
            runner.stop(name)
            try:
                shutil.rmtree(folder)
            finally:
                with self.mu:
                    self.gone.discard(name)
                    self.slot.pop(name, None)
                    self.paused.discard(name)
                    self.jobs.pop(name, None)
                    self.save()
        else:
            raise web.Fail(400, f"unknown action {what!r}")

    def pause_all(self, paused):
        with self.mu:
            self.paused_all = bool(paused)
            self.save()
            names = list(self.jobs)
        if paused:
            stops = [threading.Thread(target=runner.stop, args=(n,)) for n in names]
            for t in stops:
                t.start()
            for t in stops:
                t.join()

    def build(self, instruction, name=None, new_name=None, files=None, dashboard=None):
        if not isinstance(instruction, str) or not instruction.strip():
            raise web.Fail(400, "write what you want it to do")
        if len(instruction) > 100000:
            raise web.Fail(400, "that is too long, keep it under 100,000 characters")
        if dashboard is not None and not isinstance(dashboard, bool):
            raise web.Fail(400, "dashboard must be true or false")
        files = attachments(files)
        new = not name
        with self.mu:
            if name:
                folder = self.folder(name)
            else:
                taken = {d.name for d in spec.HOME.iterdir()}
                if new_name is None:
                    name = spec.slug(instruction, taken)
                elif not isinstance(new_name, str) or not spec.NAME.fullmatch(new_name):
                    raise web.Fail(400, "a name can only use lowercase letters, digits and dashes")
                elif new_name in taken:
                    raise web.Fail(409, f"there is already an autopilot called {new_name!r}")
                else:
                    name = new_name
                folder = spec.HOME / name
                folder.mkdir()
                self.jobs[name] = Entry()
            try:
                saved = save_attachments(folder / "context", files)
                note = "\n\nThe owner attached these files for reference. Open and look at each one first:\n" + "\n".join(saved) if saved else ""
                page = "\n\n" + DASHBOARD.format(name=name) if dashboard else ""
                (folder / ".instruction").write_text(instruction.strip() + page + note + "\n", encoding="utf-8")
            except OSError as e:
                if new:  # don't leave a half-made autopilot behind
                    shutil.rmtree(folder, ignore_errors=True)
                    self.jobs.pop(name, None)
                raise web.Fail(500, f"could not save the attached files: {e}") from None
        runner.start(name, "build")
        return name

    def save_spec(self, name, text):
        folder = self.folder(name)
        if not isinstance(text, str):
            raise web.Fail(400, "text is missing")
        try:
            spec.parse(text, folder)
        except spec.SpecError as e:
            raise web.Fail(400, str(e)) from None
        tmp = folder / ".autopilot.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, folder / "autopilot.md")


def attachments(files):
    """Decode pasted or picked files first, so a bad one fails before anything is written."""
    if files is None:
        return []
    if not isinstance(files, list) or len(files) > 20 or not all(
            isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("data"), str) for f in files):
        raise web.Fail(400, "attach at most 20 files")
    out, total = [], 0
    for f in files:
        try:
            data = base64.b64decode(f["data"], validate=True)
        except ValueError:
            raise web.Fail(400, f"could not read the file {f['name']!r}") from None
        total += len(data)
        if total > ATTACH_MAX:
            raise web.Fail(413, "the files add up to more than 25 MB")
        out.append((re.sub(r"[^\w.-]+", "-", Path(f["name"]).name)[-80:].strip(".-") or "file", data))
    return out


def save_attachments(folder, files):
    saved = []
    for name, data in files:
        folder.mkdir(exist_ok=True)
        path, n = folder / name, 2
        while path.exists():  # a second pasted image.png must not replace the first
            path, n = folder / f"{Path(name).stem}-{n}{Path(name).suffix}", n + 1
        path.write_bytes(data)
        saved.append(str(path))
    return saved


def watchdog(d):
    last = time.time()
    while True:
        time.sleep(10)
        now = time.time()
        if now - last > 30:  # the computer was asleep, not stuck
            d.beat = now
        elif now - d.beat > 120:
            log("the main loop is stuck, restarting")
            os._exit(1)
        last = now


def main():
    spec.ensure_home()
    lk = proc.Lock(spec.RUN / "daemon.lock")
    if not lk.acquire():
        print("autopilot is already running in the background.")
        return
    lk.write(pid=os.getpid(), started=time.time(), python=sys.executable)
    logfile = spec.RUN / "daemon.log"
    if logfile.exists() and logfile.stat().st_size > 5 << 20:
        logfile.write_text("", encoding="utf-8")
    log(f"started, pid {os.getpid()}")
    d = Daemon()
    threading.Thread(target=web.serve, args=(d,), daemon=True).start()
    threading.Thread(target=watchdog, args=(d,), daemon=True).start()
    while True:
        try:
            d.tick()
        except Exception:
            log(traceback.format_exc())
        d.beat = time.time()
        time.sleep(TICK)
