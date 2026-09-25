"""Register the background program so it starts with the computer and comes back if it dies."""
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from . import proc, spec

LABEL = "io.github.kafle1.autopilot"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
UNIT = Path.home() / ".config/systemd/user/autopilot.service"
BOOT = Path.home() / ".termux/boot/autopilot"
TASK = "autopilot"
LOOP = spec.RUN / "boot.pid"


def _run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _env():
    return {"AUTOPILOT_HOME": str(spec.HOME)} if os.environ.get("AUTOPILOT_HOME") else {}


def install():
    """Install or refresh the background program, and (re)start it."""
    spec.ensure_home()
    cmd = [sys.executable, "-m", "autopilot", "daemon"]
    if spec.ANDROID:
        BOOT.parent.mkdir(parents=True, exist_ok=True)
        env = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in _env().items())
        # the loop ends once uninstall deletes this file
        BOOT.write_text(f"#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\n{env}echo $$ > {shlex.quote(str(LOOP))}\n"
                        f"while [ -f {shlex.quote(str(BOOT))} ]; do {shlex.join(cmd)} >/dev/null 2>&1; sleep 5; done\n")
        BOOT.chmod(0o755)
        _stop_loop()
        stop_daemon()
        proc.spawn(["sh", str(BOOT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        log = str(spec.RUN / "daemon.log")
        PLIST.write_bytes(plistlib.dumps({
            "Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True, "KeepAlive": True,
            "ProcessType": "Interactive", "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": {"PATH": spec.config().get("path", os.environ.get("PATH", ""))} | _env()}))
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
        for _ in range(20):  # bootout returns before the old one has fully gone
            if _run(["launchctl", "bootstrap", domain, str(PLIST)], check=False).returncode == 0:
                return
            time.sleep(0.5)
        _run(["launchctl", "bootstrap", domain, str(PLIST)])
    elif os.name == "nt":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = escape(str(pyw if pyw.exists() else sys.executable))
        user = escape(os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", ""))
        start = time.strftime("%Y-%m-%dT%H:%M:%S")
        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
    <TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>-m autopilot daemon</Arguments><WorkingDirectory>{escape(str(spec.HOME))}</WorkingDirectory></Exec></Actions>
</Task>"""
        # the 5-minute trigger restarts it after a crash; a second copy just exits
        xml_path = spec.RUN / "task.xml"
        xml_path.write_text(xml, encoding="utf-16")
        stop_daemon()
        _run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml_path), "/F"])
        _run(["schtasks", "/Run", "/TN", TASK])
    else:
        UNIT.parent.mkdir(parents=True, exist_ok=True)
        env = "".join(f'Environment="{k}={v}"\n' for k, v in _env().items())
        # KillMode=process lets running jobs finish when the background program restarts
        UNIT.write_text(f"[Unit]\nDescription=autopilot\n\n[Service]\nExecStart={shlex.join(cmd)}\n{env}"
                        f'Environment="PATH={spec.config().get("path", os.environ.get("PATH", ""))}"\n'
                        f"Restart=always\nRestartSec=5\nKillMode=process\n\n[Install]\nWantedBy=default.target\n")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "autopilot"])
        _run(["systemctl", "--user", "restart", "autopilot"])
        # without linger, jobs stop when the owner logs out
        _run(["loginctl", "enable-linger", os.environ.get("USER", "")], check=False)


def _stop_loop():
    try:
        pid = int(LOOP.read_text())
        if str(BOOT) in Path(f"/proc/{pid}/cmdline").read_text(errors="replace"):  # the pid may belong to something else by now
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def installed():
    if spec.ANDROID:
        return BOOT.exists()
    if sys.platform == "darwin":
        return PLIST.exists()
    if os.name == "nt":
        return _run(["schtasks", "/Query", "/TN", TASK], check=False).returncode == 0
    return UNIT.exists()


def stop_daemon():
    info = proc.Lock(spec.RUN / "daemon.lock").held()
    pid = (info or {}).get("pid")
    if not pid:
        return
    if os.name == "nt":
        _run(["taskkill", "/F", "/PID", str(pid)], check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(50):
        if proc.Lock(spec.RUN / "daemon.lock").held() is None:
            return
        time.sleep(0.1)


def uninstall():
    if spec.ANDROID:
        BOOT.unlink(missing_ok=True)
        _stop_loop()
    elif sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
        PLIST.unlink(missing_ok=True)
    elif os.name == "nt":
        _run(["schtasks", "/End", "/TN", TASK], check=False)
        _run(["schtasks", "/Delete", "/TN", TASK, "/F"], check=False)
    else:
        if UNIT.exists():  # if systemd can't be reached, killing the daemon below only makes systemd restart it
            _run(["systemctl", "--user", "disable", "--now", "autopilot"])
        UNIT.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    stop_daemon()
