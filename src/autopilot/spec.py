"""The autopilot.md format, cron lines, durations and the shared config."""
import datetime as dt
import json
import os
import re
import platform
import secrets
import ssl
import sys
import threading
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("AUTOPILOT_HOME") or Path.home() / "autopilot").expanduser()
RUN = HOME / ".run"
CONFIG = HOME / "config.toml"
REPO = "kafle1/autopilot"
PORT = 8700
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
KEYS = {"about", "schedule", "every", "keepalive", "run", "dir", "timeout", "notify", "safe", "env", "url"}
NOTIFY = ("fail", "always", "result", "never")
ANDROID = hasattr(sys, "getandroidapilevel") or "com.termux" in os.environ.get("PREFIX", "")
OS = "Android" if ANDROID else {"Darwin": "macOS"}.get(platform.system(), platform.system())
FRONT = re.compile(r"\A﻿?\s*\+\+\+[ \t]*\r?\n(.*?)\r?\n?\+\+\+[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


class SpecError(Exception):
    pass


def ensure_home():
    RUN.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        HOME.chmod(0o700)  # logs and the sign-in secret live here


def config():
    try:
        return tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{CONFIG} has a mistake: {e}. Fix it, or delete it and run: autopilot setup") from None


def save_config(cfg):
    ensure_home()
    flat = [f"{k} = {_toml(v)}" for k, v in cfg.items() if not isinstance(v, dict)]
    for k, table in cfg.items():
        if isinstance(table, dict):
            flat += ["", f"[{k}]"] + [f"{kk} = {_toml(vv)}" for kk, vv in table.items()]
    tmp = CONFIG.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")  # two writers at once must not share one file
    tmp.write_text("\n".join(flat) + "\n", encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    os.replace(tmp, CONFIG)


TLS = ssl.create_default_context()
if not TLS.cert_store_stats()["x509_ca"] and os.path.exists("/etc/ssl/cert.pem"):
    TLS.load_verify_locations("/etc/ssl/cert.pem")  # python.org's mac build has no certificates until its installer script runs


def fetch(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=TLS)


def latest():
    """The newest release tag, like v0.2.0, or "" when there is none."""
    # the api allows 60 calls an hour per address, which a shared connection runs out of; this redirect has no limit
    with fetch(urllib.request.Request(f"https://github.com/{REPO}/releases/latest", method="HEAD"), 30) as r:
        return r.url.partition("/releases/tag/")[2]


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v))


def secret():
    cfg = config()
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_urlsafe(32)
        save_config(cfg)
    return cfg["secret"]


def port():
    return int(config().get("port", PORT))


def duration(value):
    if isinstance(value, int) or str(value).strip().isdigit():
        return int(value)
    m = re.fullmatch(r"\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*", str(value))
    if not m or not any(m.groups()):
        raise SpecError(f"can't read the time {value!r}, write it like 30s, 20m, 2h or 7d")
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def human(seconds):
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


_NAMES = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
_NAMES |= {n: i + 1 for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _field(text, lo, hi):
    out = set()
    for part in text.lower().split(","):
        body, _, step = part.partition("/")
        if body == "*":
            a, b = lo, hi
        else:
            a, _, b = body.partition("-")
            a = int(_NAMES.get(a, a))
            b = int(_NAMES.get(b, b)) if b else (hi if step else a)
        step = int(step) if step else 1
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(part)
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """Five-field cron in local time: minute hour day-of-month month day-of-week."""

    def __init__(self, text):
        parts = str(text).split()
        if len(parts) != 5:
            raise SpecError(f"a schedule needs 5 parts like \"0 8 * * *\", got {text!r}")
        try:
            limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
            self.minute, self.hour, self.dom, self.month, dow = (_field(p, *lim) for p, lim in zip(parts, limits))
        except ValueError:
            raise SpecError(f"can't read the schedule {text!r}") from None
        self.dow = {d % 7 for d in dow}
        self.any_day = parts[2].startswith("*") or parts[4].startswith("*")  # cron treats */2 like * here
        self.next(dt.datetime(2001, 1, 1))  # raises when it never happens

    def _day_ok(self, t):
        dom, dow = t.day in self.dom, t.isoweekday() % 7 in self.dow
        return dom and dow if self.any_day else dom or dow

    def next(self, after):
        t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(200_000):
            if t.month not in self.month or not self._day_ok(t):
                t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hour:
                t = (t + dt.timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minute:
                t += dt.timedelta(minutes=1)
            else:
                return t
        raise SpecError("that schedule never happens")


@dataclass
class Job:
    name: str
    folder: Path
    prompt: str = ""
    schedule: list = field(default_factory=list)
    crons: list = field(default_factory=list)
    every: int | None = None
    keepalive: bool = False
    run: str | None = None
    dir: Path | None = None
    timeout: int = 3600
    notify: str = "fail"
    safe: bool = False
    env: dict = field(default_factory=dict)
    url: str | None = None
    about: str = ""

    @property
    def kind(self):
        return "service" if self.keepalive else "command" if self.run else "ai"

    @property
    def trigger(self):
        if self.keepalive:
            return "always on"
        if self.every:
            return "every " + human(self.every)
        return ", ".join(self.schedule) or "by hand"

    def next_after(self, epoch):
        if self.every:
            return epoch + self.every
        if self.crons:
            t = dt.datetime.fromtimestamp(epoch)
            while True:  # when clocks go back an hour happens twice, so take the first time still ahead
                t = min(c.next(t) for c in self.crons)
                stamps = {t.timestamp(), t.replace(fold=1).timestamp()}
                real = [s for s in stamps if dt.datetime.fromtimestamp(s) == t] or [max(stamps)]  # a skipped time runs an hour later
                ahead = [s for s in real if s > epoch]
                if ahead:
                    return min(ahead)
        return None


def _want(s, key, kind, what):
    if key in s and not isinstance(s[key], kind):
        raise SpecError(f"{key} must be {what}")


def parse(text, folder):
    m = FRONT.match(text)
    if not m:
        raise SpecError("the file must start with its settings between two +++ lines")
    try:
        s = tomllib.loads(m[1])
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"settings: {e}") from None
    unknown = sorted(set(s) - KEYS)
    if unknown:
        raise SpecError("unknown setting: " + ", ".join(unknown))
    if sum(bool(s.get(k)) for k in ("schedule", "every", "keepalive")) > 1:
        raise SpecError("use only one of schedule, every or keepalive")
    _want(s, "schedule", (str, list), "a cron line or a list of them")
    for key in ("run", "dir", "url", "notify", "about"):
        _want(s, key, str, "text in quotes")
    for key in ("keepalive", "safe"):
        _want(s, key, bool, "true or false")
    _want(s, "env", dict, 'a table like { NAME = "value" }')

    job = Job(folder.name, folder, m[2].strip())
    sched = s.get("schedule") or []
    job.schedule = [sched] if isinstance(sched, str) else [str(x) for x in sched]
    job.crons = [Cron(x) for x in job.schedule]
    if s.get("every") not in (None, ""):  # every = 0 must fail loudly, not quietly mean "by hand"
        job.every = duration(s["every"])
        if job.every < 10:
            raise SpecError("every must be at least 10s")
    job.keepalive = s.get("keepalive", False)
    job.run = (s.get("run") or "").strip() or None
    if job.keepalive and not job.run:
        raise SpecError("keepalive needs a run command")
    if not job.run and not job.prompt:
        raise SpecError("add a run command, or instructions for the AI under the settings")
    job.dir = Path(os.path.expanduser(s.get("dir", ".")))
    if not job.dir.is_absolute():
        job.dir = folder / job.dir
    try:
        os.listdir(job.dir)  # is_dir() passes even where macOS blocks reading
    except PermissionError:
        hint = (" On a Mac: System Settings, Privacy & Security, Full Disk Access, add "
                + os.path.realpath(sys.executable)) if OS == "macOS" else ""
        raise SpecError(f"this computer blocks autopilot from the folder {job.dir}.{hint}") from None
    except OSError:
        raise SpecError(f"the folder {s.get('dir')} does not exist") from None
    job.timeout = duration(s.get("timeout", "1h"))
    if job.timeout < 10:
        raise SpecError("timeout must be at least 10s")
    job.notify = s.get("notify", "fail")
    if job.notify not in NOTIFY:
        raise SpecError("notify must be one of " + ", ".join(NOTIFY))
    job.safe = s.get("safe", False)
    job.env = {str(k): str(v) for k, v in s.get("env", {}).items()}
    job.url = s.get("url")
    job.about = s.get("about", "").strip()
    return job


def load(folder):
    return parse((folder / "autopilot.md").read_text(encoding="utf-8"), folder)


FILLER = set("""a an the and or to of for my me i it is be with at on in by from into then please every each once twice
minute minutes hour hours day days daily week weeks weekly month months monthly morning evening night noon today tomorrow am pm
add make send check find get keep run write create tell give show look one line new called named file txt md csv json""".split())


def slug(text, taken):
    words = re.findall(r"[a-z0-9]+", text.lower())
    words = [w for w in words if w not in FILLER and not w.isdigit()]
    base = "-".join(words[:4])[:40].strip("-") or "autopilot"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


FORMAT = """Write the file autopilot.md in exactly this format:

+++
about = "Sends me the top tech headlines every morning"
schedule = "0 8 * * *"
timeout = "30m"
notify = "result"
+++
Instructions for the AI, written for an assistant who knows nothing else.

Settings (all optional except about):
- about = one short line in plain words saying what it does. The dashboard shows it, so always write it.
- At most one trigger: schedule = a cron line in local time (or a list of cron lines), every = "30m" style, or keepalive = true for a program that must always run. No trigger means it only runs when started by hand.
- run = "a shell command". Leave it out when the AI should do the job; then the text under the settings is what the AI gets on every run.
- dir = the folder it works in (default: its own folder).
- timeout = how long one run may take (default "1h").
- notify = "fail" (alert when it breaks, the default), "result" (send the last line of the output as the alert, unless that line is NONE), "always" or "never".
- safe = true when the job only needs to read the web and edit files in its folder.
- env = { NAME = "value" } for settings its program needs.
- url = a link to show on the dashboard, when the job serves its own page."""

BUILD = """You are setting up an "autopilot": a job that runs by itself on this computer, in the background, with nobody watching.

{task}

Facts:
- This computer: {os}. Home folder: {home}. Now: {now}.
- The autopilot's folder: {folder}. Put every file you make there, unless the owner names another place.
- AI tools on this computer, in backup order: {engines}.{android}

{format}

How to decide:
- The job needs judgment, reading the web, or writing: leave out run, and put clear step-by-step instructions under the settings. They must say what to check first, so a repeated run never does the same thing twice.
- The job is fixed and mechanical: write a small script in the folder and point run at it. A script that needs the AI can call: autopilot ai prompt.md --dir . --timeout 30m
- The owner wants to hear the outcome: use notify = "result" and make the instructions end with one short line, the news, or exactly NONE when there is nothing new.

Rules:
- Everything must run unattended: no questions, no prompts, no waiting for input.
- Test any script once, in a way with no real effect: do not send messages, apply, buy, post or delete anything real while testing.
- Do not run the real job now. The scheduler starts it.
- Never write passwords or keys into files unless the owner gave them to you. If something is missing, like a login, write what the owner must do into NEEDS.md in the folder.
- Always write autopilot.md, even when something is missing. Then its runs must check for the missing thing first and stop with one short line saying what the owner still has to do, so it starts working by itself once they do it.
- Your very last line must be one short sentence saying what you set up and when it runs."""

ANDROID_TIP = """
- This is an Android phone running Termux. If Termux:API is installed, termux-* commands can read and send SMS, get the location, show notifications, use the camera and more."""


def build_prompt(folder, instruction, change, engines, os_name, android=False):
    if change:
        task = (f"The autopilot in {folder} already exists. Read its autopilot.md and the files next to it, "
                f"then change it as the owner asks. Keep everything they did not ask to change.\n\n"
                f"What the owner wants changed:\n{instruction}")
    else:
        task = f"What the owner wants:\n{instruction}"
    return BUILD.format(task=task, os=os_name, home=Path.home(), now=dt.datetime.now().strftime("%A %d %B %Y %H:%M"),
                        folder=folder, engines=", ".join(engines) or "none", android=ANDROID_TIP if android else "",
                        format=FORMAT)
