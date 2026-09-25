"""Processes and locks that behave the same on Mac, Linux and Windows."""
import json
import os
import re
import signal
import subprocess
import threading
import time

WIN = os.name == "nt"
if WIN:
    import ctypes
    import msvcrt
else:
    import fcntl

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")
SECRET = re.compile(rb"(?i)((?:password|passwd|secret|api_?key|access_?token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+")


spawned = None  # a runner sets this to record each child it starts, so a stuck run can still be killed


class Stopped(Exception):
    """Raised in the main thread when someone asks this process to stop."""


def on_stop():
    def handler(*_):
        for s in ("SIGTERM", "SIGHUP", "SIGINT"):
            if hasattr(signal, s):
                signal.signal(getattr(signal, s), signal.SIG_IGN)  # cleanup must not be interrupted twice
        raise Stopped()
    for s in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, s):
            signal.signal(getattr(signal, s), handler)


def spawn(cmd, **kw):
    """Start cmd in its own process group, so its whole tree can be killed later."""
    if WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    kw.setdefault("stdin", subprocess.DEVNULL)
    p = subprocess.Popen(cmd, **kw)
    if spawned:
        spawned(p.pid)
    return p


def alive(pid):
    if not pid:
        return False
    if WIN:  # os.kill(pid, 0) would terminate the process on Windows
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid, grace=10, popen=None):
    """Ask the group to stop, then force it after `grace` seconds."""
    if not pid:
        return
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=60)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end:
        if popen:
            popen.poll()  # reap it, or the zombie keeps the group looking alive
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def clean(line):
    return SECRET.sub(rb"\1[redacted]", ANSI.sub(b"", line))


def run(cmd, deadline, on_line, stdin=None, **kw):
    """Run cmd, pass each output line to on_line, kill the tree at the deadline.

    Returns the exit code, or None when it ran out of time. on_line may return True to kill it early."""
    p = spawn(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    killed = threading.Event()

    def pump():
        for raw in iter(lambda: p.stdout.readline(16 << 20), b""):  # one endless line must not eat all the memory
            if on_line(clean(raw).rstrip(b"\n").decode("utf-8", "replace")) and not killed.is_set():
                killed.set()
                threading.Thread(target=kill_tree, args=(p.pid, 5, p), daemon=True).start()

    def feed():
        try:
            p.stdin.write(stdin.encode())
            p.stdin.close()
        except OSError:
            pass

    threads = [threading.Thread(target=pump, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    code = None
    try:
        while code is None and time.time() < deadline:
            try:
                code = p.wait(timeout=min(max(0, deadline - time.time()), 86400))  # windows can't wait 49 days or more in one go
            except subprocess.TimeoutExpired:
                pass
    finally:
        if p.poll() is None:
            kill_tree(p.pid, 5, p)
        else:
            kill_tree(p.pid, 1)  # leftovers in its group, if any
    threads[0].join(5)
    return 1 if killed.is_set() and code is not None else code


class Lock:
    """An exclusive lock on a file that also records who holds it. Freed by the OS when the holder dies."""

    def __init__(self, path):
        self.path, self.f = path, None

    def acquire(self, wait=0.0):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        f = os.fdopen(fd, "r+")
        end = time.time() + wait
        while True:
            try:
                if WIN:
                    f.seek(1 << 20)  # lock past the data so others can still read who holds it
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.f = f
                return True
            except OSError:
                if time.time() >= end:
                    f.close()
                    return False
                time.sleep(0.05)

    def write(self, **info):
        self.f.seek(0)
        self.f.truncate()
        self.f.write(json.dumps(info))
        self.f.flush()

    def release(self):
        if self.f:
            self.f.seek(0)
            self.f.truncate()
            if WIN:
                self.f.seek(1 << 20)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            self.f.close()
            self.f = None

    def held(self):
        """Who holds it right now, or None when nobody does."""
        if not self.path.exists():
            return None
        probe = Lock(self.path)
        if probe.acquire():
            probe.release()
            return None
        try:
            return json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return {}
