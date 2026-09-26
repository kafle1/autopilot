"""Phone alerts through ntfy.sh: free, no account, a private random topic."""
import secrets
import urllib.request

from . import spec


def new_topic():
    return "ap-" + secrets.token_hex(16)


def send(title, message):
    cfg = spec.config()
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if cfg.get("remote_host"):
        headers["Click"] = f"https://{cfg['remote_host']}/"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message[:1000].encode(), headers=headers)
    try:
        spec.fetch(req, 15).close()
    except OSError:
        pass  # an alert is never worth failing a job for


def after_run(job, status, summary, previous):
    """Decide from the job's notify setting whether this run is news."""
    if job.notify == "never":
        return
    bad = status in ("failed", "timeout")
    if job.notify == "always":
        send(f"{job.name}: {status}", summary or status)
    elif bad and previous not in ("failed", "timeout"):
        send(f"{job.name} failed", summary or status)
    elif status == "ok" and previous in ("failed", "timeout"):
        send(f"{job.name} works again", summary or "The last run went fine.")
    elif status == "ok" and job.notify == "result" and summary and summary.strip() != "NONE":
        send(job.name, summary)
