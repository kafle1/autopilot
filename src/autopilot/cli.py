"""The autopilot command."""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, daemon, engines, notify, proc, runner, service, spec

INSTALL = "Claude Code (github.com/anthropics/claude-code), Codex (github.com/openai/codex) or opencode (opencode.ai)"
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a system proxy must not see local calls


class Down(Exception):
    pass


def call(path, body=None, timeout=90):
    """Ask the background program. It owns all state, so every change goes through it."""
    req = urllib.request.Request(f"http://127.0.0.1:{spec.port()}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Cookie": f"ap={spec.secret()}", "X-Autopilot": "1", "Content-Type": "application/json"})
    try:
        with LOCAL.open(req, timeout=timeout) as r:
            data = r.read()
            return json.loads(data) if r.headers.get_content_type() == "application/json" else data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise Down(msg or f"the background program answered with error {e.code}") from None
    except OSError:
        raise Down("The background program is not running. Run: autopilot setup") from None


def when(epoch):
    t = dt.datetime.fromtimestamp(epoch)
    days = (t.date() - dt.date.today()).days
    return f"{'today' if days == 0 else 'tomorrow' if days == 1 else t.strftime('%a %d %b')} {t:%H:%M}"


def name_arg(text):
    if not spec.NAME.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not an autopilot name")
    return text


def setup(a):
    a.yes = a.yes or not (sys.stdin and sys.stdin.isatty())  # nobody there to answer, like an installer run by a script
    spec.ensure_home()
    cfg = spec.config()
    print("Setting up autopilot on this computer.\n")
    # background runs don't get the terminal's PATH, so save it, plus where the autopilot command lives
    parts = []
    for p in os.environ.get("PATH", "").split(os.pathsep) + cfg.get("path", "").split(os.pathsep) + [str(Path.home() / ".local" / "bin")]:
        if p and p not in parts and os.path.isdir(p):
            parts.append(p)
    cfg["path"] = os.pathsep.join(parts)
    spec.save_config(cfg)

    found = [n for n in engines.ORDER if engines.find(n)]
    if not found:
        print(f"No AI tool found. Install {INSTALL}, log in, then run autopilot setup again.\n")
    if a.yes:
        cfg.setdefault("engines", found)
    elif found:
        working = []
        for n in found:
            print(f"Testing {n}... ", end="", flush=True)
            ok, why = engines.test(n)
            print("works." if ok else f"not working: {engines.advice(n, why)}")
            if ok or engines.USED_UP.search(why):
                working.append(n)
        keep = [n for n in cfg.get("engines", []) if n in working]
        cfg["engines"] = keep + [n for n in working if n not in keep] or found
        print(f"AI tools, in backup order: {', '.join(cfg['engines'])}\n")

    if not a.yes and not cfg.get("ntfy_topic") and input("Get alerts on your phone when something breaks? [Y/n] ").strip().lower() in ("", "y", "yes"):
        cfg["ntfy_topic"] = notify.new_topic()
    spec.save_config(cfg)
    spec.secret()
    if cfg.get("ntfy_topic") and not a.yes:
        print(f"For alerts, install the free ntfy app on your phone and subscribe to:\n  https://ntfy.sh/{cfg['ntfy_topic']}\n")
    print("Starting the background program...")
    try:
        service.install()
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        sys.exit(f"Could not start it: {e}\nYou can start it by hand and leave it running: autopilot daemon")
    for _ in range(40):
        try:
            call("/api/state", timeout=5)
            break
        except Down:
            time.sleep(0.5)
    else:
        sys.exit(f"The background program did not start. Its log is {spec.RUN / 'daemon.log'}")
    print("It runs now, and starts by itself with the computer.\n")
    if not a.yes:
        print('Make your first autopilot from the dashboard, or type:\n  autopilot new "every morning, find ... and send me the best ones"\n')
        open_(a)


def open_(a):
    url = f"http://127.0.0.1:{spec.port()}/pair?code={call('/api/pair/new', {})['code']}"
    if not webbrowser.open(url):
        print(f"Open this in your browser within 10 minutes: {url}")


def list_(a):
    s = call("/api/state")
    for p in s["device"]["problems"]:
        print(f"! {p}")
    if not s["jobs"]:
        print('No autopilots yet. Make one: autopilot new "what you want done"')
    for j in s["jobs"]:
        status = "building" if j["building"] else j["status"]
        nxt = f"next {when(j['next'])}" if j["next"] else ""
        trigger = j["trigger"] if len(j["trigger"]) <= 24 else j["trigger"][:21] + "..."
        print(f"{j['name']:<30} {status:<9} {trigger:<24} {nxt}")
        detail = j["error"] or (j["last"] or {}).get("summary")
        if detail:
            print(f"    {detail[:160]}")


def build(a):
    name = call("/api/build", {"instruction": a.instruction} | ({"name": a.name} if getattr(a, "name", None) else {}))["name"]
    print(f"The AI is building {name!r} now. It switches itself on when ready.\nWatch it: autopilot logs {name} -f")


def action(a):
    call("/api/action", {"name": a.name, "action": a.cmd}, timeout=120)
    print({"run": f"Started {a.name}. Watch it: autopilot logs {a.name} -f",
           "stop": f"Stopped {a.name}. An always-on autopilot starts again by itself; to keep it off: autopilot pause {a.name}",
           "restart": f"Restarted {a.name}.",
           "pause": f"Paused {a.name}. It won't run until: autopilot resume {a.name}",
           "resume": f"{a.name} is back on."}[a.cmd])


def logs(a):
    folder = spec.HOME / a.name
    if not folder.is_dir():
        sys.exit(f"There is no autopilot called {a.name!r}.")
    found = sorted((folder / "logs").glob("*.log"))
    if not found:
        sys.exit("No runs yet.")
    with open(found[-1], "rb") as f:
        f.seek(max(0, found[-1].stat().st_size - (256 << 10)))
        while True:
            done = not a.follow or runner.lock(a.name).held() is None
            sys.stdout.write(f.read().decode("utf-8", "replace"))
            sys.stdout.flush()
            if done:
                break
            time.sleep(1)


def ai(a):
    proc.on_stop()
    try:
        prompt = Path(a.prompt_file).read_text(encoding="utf-8")
        deadline = time.time() + spec.duration(a.timeout)
    except (OSError, spec.SpecError) as e:
        sys.exit(str(e))
    folder = Path(a.dir).expanduser().resolve()
    try:
        ok, engine, summary = engines.run(prompt, folder, deadline, lambda s: print(s, flush=True), a.safe)
    except proc.Stopped:
        sys.exit(143)
    print(f"== {'done' if ok else 'failed'}{f' with {engine}' if engine else ''}" + (f": {summary}" if summary else ""), flush=True)
    sys.exit(0 if ok else 1)


def tailscale():
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    return shutil.which("tailscale") or (app if os.path.exists(app) else None)


def phone(a):
    ts = tailscale()
    if not ts:
        sys.exit("Tailscale is not installed. Install it from https://tailscale.com/download on this computer and on "
                 "your phone, sign in to the same account on both, then run: autopilot phone")
    cfg = spec.config()
    if a.off:
        subprocess.run([ts, "serve", "--https=443", "off"], capture_output=True, timeout=60)
        cfg.pop("remote_host", None)
        cfg.pop("secret", None)  # a new secret signs out every browser, a lost phone included
        spec.save_config(cfg)
        print("Phone access is off and every browser is signed out. To sign in here again: autopilot open")
        return
    r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=60)
    try:
        st = json.loads(r.stdout)
    except ValueError:
        st = {}
    if st.get("BackendState") != "Running":
        sys.exit("Tailscale is not connected. Open Tailscale, sign in, then run: autopilot phone")
    host = st.get("Self", {}).get("DNSName", "").rstrip(".")
    if not host:
        sys.exit("Tailscale has no name for this computer. In the Tailscale admin page, turn on MagicDNS, then run: autopilot phone")
    print("Sharing the dashboard with your Tailscale devices. If Tailscale asks you to turn on HTTPS, open its link and allow it.")
    if subprocess.run([ts, "serve", "--bg", str(spec.port())]).returncode:
        sys.exit("Tailscale could not share the dashboard. Fix what it says above, then run: autopilot phone")
    cfg["remote_host"] = host
    spec.save_config(cfg)
    print(f"\nOn your phone, turn Tailscale on and open:\n  https://{host}\n"
          f"Then enter this code within 10 minutes: {call('/api/pair/new', {})['code']}\n"
          "Tip: add it to your home screen so it opens like an app.")


def doctor(a):
    fixes = []

    def check(ok, good, fix):
        print(f"{'ok ' if ok else 'FIX'}  {good if ok else fix}")
        if not ok:
            fixes.append(fix)

    print(f"autopilot {__version__} on {spec.OS}, Python {sys.version.split()[0]}")
    check(spec.CONFIG.exists(), f"settings in {spec.CONFIG}", "not set up yet. Run: autopilot setup")
    check(service.installed(), "starts by itself with the computer", "the background program is not registered. Run: autopilot setup")
    try:
        s = call("/api/state", timeout=10)
    except Down as e:
        s = None
        check(False, "", str(e))
    for n in engines.ORDER:
        if engines.find(n):
            print(f"testing {n}...", end="\r", flush=True)  # same width as "ok   {n} works", so nothing is left behind
            ok, why = engines.test(n)
            used_up = not ok and engines.USED_UP.search(why)
            check(ok or used_up, f"{n} is signed in but its plan is used up for now: {why}" if used_up else f"{n} works",
                  f"{n} does not work: {engines.advice(n, why)}")
    if not any(engines.find(n) for n in engines.ORDER):
        check(False, "", f"no AI tool is installed. Install {INSTALL}")
    check(bool(spec.config().get("ntfy_topic")), "phone alerts are on", "phone alerts are off. Run: autopilot setup")
    if s:
        for j in s["jobs"]:
            if j["status"] in ("broken", "failed"):
                check(False, "", f"{j['name']}: {j['error'] or (j['last'] or {}).get('summary') or j['status']}")
    print("\nEverything looks fine." if not fixes else f"\n{len(fixes)} thing(s) to fix.")
    sys.exit(1 if fixes else 0)


def update(a):
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{spec.REPO}/releases/latest", timeout=30) as r:
            tag = json.load(r)["tag_name"]
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Could not check for updates: {e}")
    if tag.lstrip("v") == __version__:
        print(f"You have the newest version ({__version__}).")
        return
    src = f"https://github.com/{spec.REPO}/archive/refs/tags/{tag}.tar.gz"  # a git link would need git installed
    if spec.ANDROID:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", src]
    else:
        uv = shutil.which("uv") or shutil.which("uv", path=str(Path.home() / ".local" / "bin"))
        if not uv:
            sys.exit("uv is missing. Install autopilot again with the install command from the README.")
        cmd = [uv, "tool", "install", "--force", "--managed-python", "--python", "3.12", src]
    after = [sys.executable, "-m", "autopilot", "setup", "--yes"]
    print(f"Updating to {tag}...")
    if os.name == "nt":
        # windows can't replace files in use, so stop everything, and update once this command has exited
        subprocess.run(["schtasks", "/Change", "/TN", service.TASK, "/Disable"], capture_output=True)
        service.stop_daemon()
        stop_all()
        log = spec.RUN / "update.log"
        script = spec.RUN / "update.cmd"
        script.write_text("\r\n".join(["@echo off", "ping -n 4 127.0.0.1 >nul",  # timeout /t fails without a keyboard
                                       f'{subprocess.list2cmdline(cmd)} > "{log}" 2>&1',
                                       f'{subprocess.list2cmdline(after)} >> "{log}" 2>&1', ""]), encoding="utf-8")
        proc.spawn(["cmd", "/c", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"It finishes in the background in about a minute. If something goes wrong, see {log}")
        return
    if subprocess.run(cmd).returncode or subprocess.run(after).returncode:
        sys.exit("The update did not finish. The messages above say why.")
    print(f"Updated to {tag}.")


def stop_all():
    for d in spec.HOME.iterdir() if spec.HOME.is_dir() else []:
        if spec.NAME.fullmatch(d.name):
            runner.stop(d.name)


def uninstall(a):
    try:
        service.uninstall()
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        sys.exit(f"Could not remove it: {e}")
    stop_all()
    print(f"The background program is removed and nothing runs anymore. Your autopilots are still in {spec.HOME}.\n"
          "To remove the autopilot command too: uv tool uninstall autopilot")


def main():
    if sys.argv[1:2] == ["_job"] and len(sys.argv) == 4:  # started by the background program for one run
        return runner.main(sys.argv[2], sys.argv[3])
    for out in (sys.stdout, sys.stderr):
        if out:  # pythonw has none. a windows pipe is cp1252 and would crash on a job's emoji
            out.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="autopilot", description="Jobs that run by themselves, built by AI from plain English.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", metavar="command")
    s = sub.add_parser("setup", help="set up this computer (safe to run again)")
    s.add_argument("--yes", action="store_true", help="don't ask anything")
    s.set_defaults(fn=setup)
    sub.add_parser("open", help="open the dashboard, already signed in").set_defaults(fn=open_)
    sub.add_parser("list", help="every autopilot with its status and next run").set_defaults(fn=list_)
    s = sub.add_parser("new", help='the AI builds a new autopilot: autopilot new "what you want done"')
    s.add_argument("instruction")
    s.set_defaults(fn=build)
    s = sub.add_parser("change", help='the AI changes one: autopilot change NAME "what to change"')
    s.add_argument("name", type=name_arg)
    s.add_argument("instruction")
    s.set_defaults(fn=build)
    for cmd, text in (("run", "run it now"), ("stop", "stop it now"), ("restart", "stop it and start it again"),
                      ("pause", "stop it and don't start it again"), ("resume", "undo pause")):
        s = sub.add_parser(cmd, help=text)
        s.add_argument("name", type=name_arg)
        s.set_defaults(fn=action)
    s = sub.add_parser("logs", help="show its latest log")
    s.add_argument("name", type=name_arg)
    s.add_argument("-f", "--follow", action="store_true", help="keep showing new lines while it runs")
    s.set_defaults(fn=logs)
    s = sub.add_parser("ai", help="run the AI tools with backups, for scripts: autopilot ai prompt.md")
    s.add_argument("prompt_file")
    s.add_argument("--dir", default=".")
    s.add_argument("--timeout", default="1h")
    s.add_argument("--safe", action="store_true", help="no shell commands, and edits only in --dir")
    s.set_defaults(fn=ai)
    s = sub.add_parser("phone", help="use the dashboard from your phone, through Tailscale")
    s.add_argument("--off", action="store_true", help="turn phone access off and sign out every browser")
    s.set_defaults(fn=phone)
    sub.add_parser("doctor", help="check everything and say what to fix").set_defaults(fn=doctor)
    sub.add_parser("update", help="install the newest version").set_defaults(fn=update)
    sub.add_parser("uninstall", help="stop everything and remove the background program").set_defaults(fn=uninstall)
    sub.add_parser("daemon", help="the background program itself (started for you)").set_defaults(fn=lambda a: daemon.main())
    a = p.parse_args()
    if not a.cmd:  # plain "autopilot" does the obvious thing
        a.yes = False
        a.fn = open_ if spec.CONFIG.exists() else setup
    try:
        a.fn(a)
    except Down as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
