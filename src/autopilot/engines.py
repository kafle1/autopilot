"""Find, test and run the AI tools, moving to the next one when a run fails."""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from . import proc, spec

ORDER = ("claude", "codex", "opencode")
SCRUB = re.compile(r"^(ANTHROPIC_|OPENAI_|CLAUDE_CODE_USE_)|^(CLAUDECODE|CODEX_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|OPENROUTER_API_KEY|AWS_BEARER_TOKEN_BEDROCK)$")
# opencode hangs forever on a provider error such as a used-up plan, so its own error log line ends the run
OPENCODE_FATAL = re.compile(r'level=ERROR .*message="stream error".* small=false')
MODEL_FLAG = {"claude": "--model", "codex": "-m", "opencode": "-m"}
LOGIN = {"claude": "claude, then type /login", "codex": "codex login", "opencode": "opencode auth login"}
USED_UP = re.compile(r"limit|quota|exceeded", re.I)  # still logged in, the plan just ran out for now


def env(extra=None):
    """The environment for every job: paid-API keys removed so only subscription logins get used."""
    cfg = spec.config()
    e = {k: v for k, v in os.environ.items() if not SCRUB.match(k)}
    if cfg.get("path"):
        e["PATH"] = cfg["path"]
    e.setdefault("LANG", "en_US.UTF-8")
    e.setdefault("PYTHONUTF8", "1")  # windows python writes cp1252 to a pipe and dies on the first emoji
    e.update(extra or {})
    return e


def find(name):
    return shutil.which(name, path=env()["PATH"])


def order():
    cfg = spec.config()
    return [e for e in cfg.get("engines", ORDER) if e in ORDER]


def online(deadline):
    """Wait up to 10 minutes for the internet, so a run right after wake doesn't fail for nothing."""
    end = min(deadline, time.time() + 600)
    while True:
        for host in ("1.1.1.1", "8.8.8.8", "github.com"):
            try:
                socket.create_connection((host, 443), 3).close()
                return True
            except OSError:
                pass
        if time.time() >= end:
            return False
        time.sleep(10)


def _render_claude(line, out, result):
    try:
        ev = json.loads(line)
    except ValueError:
        out(line)
        return
    if ev.get("type") == "assistant":
        for part in ev.get("message", {}).get("content", []):
            if part.get("type") == "text" and part["text"].strip():
                out(part["text"].strip())
            elif part.get("type") == "tool_use":
                args = part.get("input") or {}
                what = next((str(args[k]) for k in ("command", "file_path", "url", "pattern", "query", "description") if k in args), "")
                out(f"> {part.get('name')} {what}"[:300])
    elif ev.get("type") == "result":
        result.update(ev)


def _codex_api_key():
    try:
        r = subprocess.run([find("codex"), "login", "status"], capture_output=True, text=True, timeout=30, env=env())
        return "api key" in (r.stdout + r.stderr).lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def attempt(name, prompt, cwd, deadline, out, safe=False):
    """One try with one engine. Returns (ok, summary)."""
    exe = find(name)
    if not exe:
        return False, f"{name} is not installed"
    cfg = spec.config()
    model = cfg.get("models", {}).get(name)
    flags = list(cfg.get("engine_args", {}).get(name, [])) + ([MODEL_FLAG[name], model] if model else [])
    e = env()
    result, lines, tmp, on_line = {}, [], None, None
    if name == "claude":
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--add-dir", str(cwd)] + flags
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"] if safe \
            else ["--dangerously-skip-permissions"]
        e["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"  # don't sit waiting on background shells after the answer
        on_line = lambda s: _render_claude(s, out, result)
    elif name == "codex":
        if _codex_api_key():
            return False, "codex is logged in with an API key, which bills per use. Run: codex login"
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        cmd = [exe, "exec", "--skip-git-repo-check", "-C", str(cwd), "-o", tmp] + flags
        cmd += ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"] if safe \
            else ["--dangerously-bypass-approvals-and-sandbox"]
        cmd.append("-")
    else:
        if safe:
            return False, "opencode can't run safe autopilots. Install Claude Code or Codex, or remove safe = true"
        fd, tmp = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = [exe, "run", "Follow the instructions in the attached file exactly.", "-f", tmp,
               "--dir", str(cwd), "--auto", "--print-logs", "--log-level", "ERROR"] + flags
        prompt = None
    if on_line is None:
        def on_line(s):
            if s.strip():
                lines.append(s)
                out(s)
            return name == "opencode" and bool(OPENCODE_FATAL.search(s))
    try:
        code = proc.run(cmd, deadline, on_line, stdin=prompt, cwd=cwd, env=e)
        if code is None:
            return False, "ran out of time"
        if name == "claude":
            text = str(result.get("result") or "")
            ok = code == 0 and result.get("type") == "result" and not result.get("is_error")
        elif name == "codex":
            text = Path(tmp).read_text(encoding="utf-8", errors="replace")
            ok = code == 0
        else:
            text = "\n".join(lines)
            ok = code == 0
        if not ok and not text.strip():
            text = "\n".join(lines)  # codex prints its error, the answer file stays empty
        last = next((s.strip() for s in reversed(text.splitlines()) if s.strip()), "")
        if m := re.search(r'error\.error="([^"]+)"', last):  # opencode buries the reason in a long log line
            last = m.group(1)
        return ok, last[:500] if ok else (last[:500] or f"{name} stopped with code {code}")
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def run(prompt, cwd, deadline, out, safe=False):
    """Try each engine in the saved order until one succeeds. Returns (ok, engine, summary)."""
    names = order()
    if not names:
        return False, None, "no AI tool is set up. Run: autopilot setup"
    if not online(deadline):
        out("== no internet for 10 minutes, trying anyway")
    last = (False, None, "not enough time left to try")
    for name in names:
        if deadline - time.time() < 60:
            break
        out(f"== trying {name}")
        ok, summary = attempt(name, prompt, cwd, deadline, out, safe)
        if ok:
            return True, name, summary
        out(f"== {name} failed: {summary}")
        last = (False, name, summary)
        if name == names[0]:  # the failed try may have acted before it died
            prompt += ("\n\nAn earlier try at this stopped partway. Before you do anything, check what it already did "
                       "(files it wrote, messages it sent), so nothing happens twice.")
    return last


def advice(name, why):
    return f"{why.rstrip('.')}." + ("" if USED_UP.search(why) else f" To fix it, run: {LOGIN[name]}")


def test(name):
    """A tiny real request, to prove the tool is installed and logged in."""
    with tempfile.TemporaryDirectory() as d:
        ok, summary = attempt(name, "Reply with just the word OK.", Path(d), time.time() + 120, lambda s: None)
    return ok, summary
