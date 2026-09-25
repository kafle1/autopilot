"""One run of one autopilot: lock, log, time limit, alert. Runs as its own process."""
import contextlib
import datetime as dt
import json
import os
import platform
import signal
import subprocess
import sys
import time
import traceback

from . import engines, notify, proc, spec

KEEP = 50
LOG_CAP = 10 << 20
RUN_ID = r"\d{8}-\d{6}"


def lock(name):
    return proc.Lock(spec.RUN / f"{name}.lock")


def start(name, trigger):
    """Start a run in the background. It survives the program that started it."""
    spec.ensure_home()
    err = open(spec.RUN / "daemon.log", "ab")
    try:
        return proc.spawn([sys.executable, "-m", "autopilot", "_job", name, trigger],
                          stdout=subprocess.DEVNULL, stderr=err, cwd=spec.HOME)
    finally:
        err.close()


def stop(name, wait=15):
    """Stop a run and wait until its lock is free."""
    lk = lock(name)
    info = lk.held()
    if info is None:
        return
    try:
        if proc.WIN:
            proc.kill_tree(info.get("pid"))
        elif info.get("pid"):
            os.kill(info["pid"], signal.SIGTERM)  # the runner kills its own child tree and records "stopped"
    except ProcessLookupError:
        pass
    end = time.time() + wait
    while time.time() < end and lk.held() is not None:
        time.sleep(0.2)
    info = lk.held()
    if info:  # it didn't listen, so force it
        proc.kill_tree(info.get("child"), 0)
        if proc.WIN:
            proc.kill_tree(info.get("pid"))
        elif info.get("pid"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(info["pid"], signal.SIGKILL)


def runs(name, limit=KEEP):
    """Finished and current runs, newest first."""
    logs = spec.HOME / name / "logs"
    out = []
    for f in sorted(logs.glob("*.json"), reverse=True)[:limit] if logs.is_dir() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        r["id"] = f.stem
        out.append(r)
    if any(r.get("status") == "running" for r in out):
        live = (lock(name).held() or {}).get("log")
        for r in out:
            if r.get("status") == "running" and r["id"] != live:
                r["status"] = "stopped"  # the run died without writing its end
    return out


class Log:
    def __init__(self, folder):
        self.dir = folder / "logs"
        self.dir.mkdir(exist_ok=True)
        self.id = time.strftime("%Y%m%d-%H%M%S")
        while (self.dir / f"{self.id}.log").exists():  # a "-2" suffix would sort before the first run
            time.sleep(1)
            self.id = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"{self.id}.log"
        self.f = open(self.path, "w+b")

    def __call__(self, line):
        self.f.write(line.encode("utf-8", "replace") + b"\n")
        self.f.flush()
        if self.f.tell() > LOG_CAP:  # a chatty service would fill the disk otherwise
            self.f.seek(-(1 << 20), 2)
            tail = self.f.read()
            self.f.seek(0)
            self.f.truncate()
            self.f.write(b"== older lines trimmed\n" + tail)

    def record(self, **info):
        tmp = self.dir / f"{self.id}.tmp"
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, self.dir / f"{self.id}.json")

    def prune(self):
        for f in sorted(self.dir.glob("*.json"), reverse=True)[KEEP:]:
            f.unlink(missing_ok=True)
            f.with_suffix(".log").unlink(missing_ok=True)


def paused(name):
    try:
        s = json.loads((spec.RUN / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(s.get("paused_all")) or name in s.get("paused", [])


def preamble(job):
    return (f"You are running unattended as the autopilot \"{job.name}\" on {platform.system()}. "
            f"Now: {dt.datetime.now():%A %d %B %Y %H:%M}. Nobody will answer questions, so decide and finish. "
            f"End with one short line that sums up the result.\n\n")


def main(name, trigger):
    proc.on_stop()
    lk = lock(name)
    if trigger == "build":
        stop(name)
    # the daemon briefly probes this lock, so give it a moment
    if not lk.acquire(wait=1):
        return
    if trigger in ("schedule", "service") and paused(name):  # paused while this was starting, and the stop found no lock yet
        lk.release()
        return
    folder = spec.HOME / name
    log = Log(folder)
    started = time.time()
    rec = {"trigger": trigger, "started": started, "ended": None, "status": "running", "engine": None, "summary": ""}
    lk.write(pid=os.getpid(), child=None, started=started, trigger=trigger, log=log.id)
    proc.spawned = lambda pid: lk.write(pid=os.getpid(), child=pid, started=started, trigger=trigger, log=log.id)
    log.record(**rec)
    previous = next((r["status"] for r in runs(name)[1:] if r.get("status") in ("ok", "failed", "timeout")), None)
    log(f"== started {dt.datetime.now():%Y-%m-%d %H:%M:%S} ({trigger})")
    job = None
    try:
        if trigger == "build":
            status, engine, summary = build(name, folder, started, log)
        else:
            job = spec.load(folder)
            status, engine, summary = execute(job, started, log)
    except proc.Stopped:
        status, engine, summary = "stopped", rec["engine"], "stopped by hand"
    except spec.SpecError as e:
        status, engine, summary = "failed", None, str(e)
    except Exception:
        log(traceback.format_exc())
        status, engine, summary = "failed", None, "autopilot itself hit an error, see the log"
    ended = time.time()
    log(f"== {status} after {spec.human(int(ended - started))}" + (f": {summary}" if summary else ""))
    log.record(**rec | {"ended": ended, "status": status, "engine": engine, "summary": summary})
    log.prune()
    lk.release()
    if job and status != "stopped":
        notify.after_run(job, status, summary, previous)


def settings(md):
    try:
        m = spec.FRONT.match(md if isinstance(md, str) else md.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return m and m[1]


def execute(job, started, log):
    deadline = started + (10 * 365 * 86400 if job.keepalive else job.timeout)
    if not job.run:
        md = job.folder / "autopilot.md"
        before = md.read_text(encoding="utf-8")
        try:
            ok, engine, summary = engines.run(preamble(job) + job.prompt, job.dir, deadline, log, job.safe, job.env)
        finally:
            if job.safe and settings(md) != settings(before):  # a page it read could tell it to turn safe off
                md.unlink(missing_ok=True)
                md.write_text(before, encoding="utf-8")
                log("== put autopilot.md back: a safe autopilot can't change its own settings")
        return ("ok" if ok else "timeout" if deadline - time.time() < 60 else "failed"), engine, summary

    last = [""]

    def line(s):
        log(s)
        if s.strip():
            last[0] = s.strip()

    code = proc.run(job.run, deadline, line, cwd=job.dir, env=engines.env(job.env), shell=True)
    if code is None:
        return "timeout", None, f"ran longer than {spec.human(job.timeout)}"
    if code == 0:
        return "ok", None, last[0][:500]
    return "failed", None, f"exit code {code}. {last[0][:400]}".strip()


def build(name, folder, started, log):
    """Let the AI write or change this autopilot from the owner's plain-English instruction."""
    instruction = (folder / ".instruction").read_text(encoding="utf-8").strip()
    md = folder / "autopilot.md"
    before = md.read_bytes() if md.exists() else None
    prompt = spec.build_prompt(folder, instruction, before is not None, engines.order(), platform.platform(terse=True), spec.ANDROID)
    deadline = started + 1800
    ok = False
    try:
        ok, engine, summary = engines.run(prompt, folder, deadline, log)
        if ok:
            try:
                spec.load(folder)
            except (OSError, spec.SpecError) as e:
                log(f"== autopilot.md has a problem, asking the AI to fix it: {e}")
                fix = prompt + f"\n\nYou already started. The file autopilot.md has this problem: {e}\nFix it, keep everything else."
                ok, engine, summary = engines.run(fix, folder, deadline, log)
                try:
                    spec.load(folder)
                except (OSError, spec.SpecError) as e:
                    ok, summary = False, f"the AI could not write a working autopilot.md: {e}"
        needs = folder / "NEEDS.md"
        if ok and needs.exists() and needs.stat().st_mtime >= started:
            summary = f"{summary}\nIt needs something from you first. Open NEEDS.md in its files.".strip()
        return ("ok" if ok else "failed"), engine, summary
    finally:
        if not ok:  # a failed or stopped build must not leave a half-made autopilot for the schedule to run
            if before is None:
                md.unlink(missing_ok=True)
            else:
                md.write_bytes(before)
            log("== undid its autopilot.md changes, so nothing half-made runs")
