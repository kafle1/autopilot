# autopilot

You tell it in plain English what to do and when. The AI on your computer
builds it, and it runs by itself in the background, even after a restart. You
watch and control everything from one page, and you can open that page from
your phone too.

## What you need

- A computer: Mac, Windows, or Linux.
- At least one paid AI plan: Claude Pro or Max (for Claude Code), ChatGPT Plus
  or Pro (for Codex), or OpenCode Go.

autopilot never uses pay-per-use API keys, so you will not get a surprise
bill. If you have more than one AI plan, autopilot moves to the next one on
your list when the first one hits its usage limit.

## Install

Mac or Linux, open Terminal and paste this:

```
curl -LsSf https://github.com/kafle1/autopilot/releases/latest/download/install.sh | sh
```

Windows, open PowerShell and paste this:

```
powershell -ExecutionPolicy ByPass -c "irm https://github.com/kafle1/autopilot/releases/latest/download/install.ps1 | iex"
```

Setup runs right after install. It checks which AI tools work on your
computer, asks if you want alerts on your phone and if you want the
ready-made PhD fellowship finder, starts the background program so it also
runs after every restart, and opens the dashboard.
Your autopilots live in a folder called `autopilot` in your home folder.

## Make your first autopilot

Run `autopilot open` to open the dashboard in your browser, then tap "New
autopilot" and type what you want in plain English. Or type it straight into
the terminal:

```
autopilot new "every Monday at 8am, find new fully funded PhD fellowships abroad in computer science and send me the best five"
```

```
autopilot new "every 30 minutes, check that mysite.com loads and alert my phone if it is down"
```

The AI takes a minute or two to build it, then it switches itself on. If it
needs something only you can give, like a login, it writes what it needs in
a file called `NEEDS.md`, which you can open from the dashboard.

## Use it from your phone

1. Install Tailscale (it's free) on your computer and on your phone.
2. Sign in to both with the same account.
3. On your computer, run `autopilot phone`.
4. On your phone, open the address it printed and type in the 6-digit code.
5. Tap "Add to Home Screen" so it acts like an app.

Lost your phone? Run `autopilot phone --off`. It signs out every browser.

For alerts on your phone, install the ntfy app. autopilot sends alerts
through it when something fails, finishes, or needs you.

## Android phone as a worker (experimental)

Your phone can also run autopilots itself, not just watch them.

1. Install Termux, Termux:API, and Termux:Boot, all from F-Droid (not the
   Play Store version, it's out of date).
2. Open Termux and run the same install command from the "Install" section
   above.

This is not tested on a real phone yet, and the AI tools may not all run on
Android. An iPhone cannot run background jobs at all, so an iPhone can only
open the dashboard and control a computer, never run autopilots itself.

## Good to know

- Autopilots run while your computer is awake. If it's asleep when a job was
  due, autopilot catches up on it as soon as the computer wakes.
- An autopilot has the same access to your computer as you do. If one reads a
  web page, that page could try to trick it into doing something you didn't
  ask for. Use `safe = true` (see below) for any autopilot that only needs to
  read the web, not run commands.
- Keep passwords and other secrets out of your instructions. Put them in
  `env` instead (see below).

## The autopilot.md file

Every autopilot is one file, `autopilot.md`, inside its own folder at
`~/autopilot/<name>/`. The top holds settings between `+++` lines, and
everything below that is plain-English instructions for the AI (skip the
instructions if you use `run` instead).

Settings, all optional:

- `schedule`: when to run it, as a cron line (e.g. `"0 9 * * *"` for 9am
  daily), or a list of cron lines. Times are your computer's local time.
- `every`: run on a fixed interval instead of a schedule, e.g. `"20m"`,
  `"2h"`, `"7d"`.
- `keepalive`: set to `true` for a service that should always be running (use
  with `run`), instead of something that runs on a schedule.
- `run`: a shell command to run instead of asking the AI. Leave it out and
  the AI reads the instructions below the settings.
- `dir`: which folder the job runs in. Defaults to the autopilot's own
  folder.
- `timeout`: how long it's allowed to run before autopilot stops it.
  Defaults to `"1h"`.
- `notify`: when to alert you. `"fail"` (default) alerts when it starts
  failing and when it recovers. `"always"` alerts every run. `"result"` sends
  you the last line of what it produced, unless that line is exactly `NONE`.
  `"never"` stays quiet.
- `safe`: set to `true` so the AI can't run commands, can only change files
  in its own folder, and can't change its own settings. It can still read
  your files and the web, so keep secrets somewhere it has no reason to look.
- `env`: extra environment variables for the job, as `key = "value"` pairs.
- `url`: a link shown on the dashboard, for an autopilot that serves its own
  page.

Three small examples:

An AI job that runs every morning:

```
+++
schedule = "0 8 * * *"
timeout = "30m"
notify = "result"
+++
Check my inbox for anything from a recruiter and summarize it in one line per email.
```

A command job that runs on a timer:

```
+++
every = "15m"
run = "curl -sf https://example.com/health || exit 1"
notify = "fail"
+++
```

A keepalive service:

```
+++
keepalive = true
run = "python3 server.py"
url = "http://127.0.0.1:8787"
+++
```

## Commands

```
autopilot setup                     first-time setup (--yes asks nothing, keeps your settings)
autopilot open                      open the dashboard in the browser, already signed in
autopilot list                      every autopilot with status and next run
autopilot new "instruction"         the AI builds a new autopilot from plain English
autopilot change NAME "instruction" the AI changes an existing autopilot
autopilot run|stop|restart|pause|resume NAME
autopilot logs NAME [-f]            print the latest log, -f keeps following it
autopilot ai PROMPT_FILE [--dir D] [--timeout 45m] [--safe]   run the AI with backups, for scripts
autopilot phone [--off]             use the dashboard from your phone through Tailscale
autopilot doctor                    check everything and say what to fix
autopilot update                    install the newest release
autopilot uninstall                 remove the background program (keeps ~/autopilot)
autopilot daemon                    the background program itself (started for you)
```

## How it works

One background program stays alive through launchd on Mac, systemd on Linux,
Task Scheduler on Windows, or Termux:Boot on Android. It serves a dashboard
on `127.0.0.1:8700` and runs each autopilot in its own process group with a
timeout, so a stuck job can't hang forever. Logs land in
`~/autopilot/<name>/logs`. It tries your AI tools in the order you set, and
moves to the next one if one is unavailable or out of usage. No dependencies
beyond Python's standard library.

## Uninstall

```
autopilot uninstall
uv tool uninstall autopilot
```

The first command removes the background program. The second removes
autopilot itself (on Android, use `pip uninstall autopilot` instead). Your autopilots and their files stay in `~/autopilot`
until you delete that folder yourself.

## License

MIT, see [LICENSE](LICENSE).
