# Prompt for Claude Code — kill stale/duplicate processes on bot startup AND on stop

Paste this into Claude Code in the bot's project folder (works for both the
local copy and the VPS copy — apply the same change to both, or run it on
the VPS where the bot actually trades live).

**Scope: this needs to run at TWO points, not just one:**
1. **On `start_bot()`** — clean up any orphaned processes left over from a
   previous session before launching new ones (covered below).
2. **On `stop_bot()`** — today, `stop_bot()` only calls `.terminate()` on
   the 5 process handles this *specific UI instance* happens to remember
   (`self.bot_proc`, `self.dashboard_proc`, `self.web_server_proc`,
   `self.tunnel_proc`, `self.backup_proc`). If the UI was restarted or
   crashed earlier in the session, those handles can be `None` even while
   the real OS processes are still running — so clicking "Stop Bot" can
   silently leave processes alive, which the user has no way to detect
   from the UI (status labels just go blank). `stop_bot()` should use the
   exact same OS-wide sweep as the startup cleanup, so "Stop Bot" actually
   guarantees everything is dead, not just "everything this UI remembers."

---

## Problem

`strategy_config_ui.py`'s `start_bot()` only refuses to start a second bot
if `self.bot_proc` (an in-memory handle in the *current* UI process) is
still alive. If the UI window is closed and reopened, or the UI crashes
and is relaunched, those handles reset to `None` — but the *actual*
Windows processes from the previous session (the EA itself, the dashboard
`--watch` loop, the web server, the cloudflared tunnel, the backup watcher)
may still be running, orphaned, with nothing tracking them anymore.
Clicking "Start Bot" again then launches a **second, independent
`xauusd_mt5_strategy.py` process** alongside the orphaned first one — two
EAs scanning and potentially placing trades against the same MT5 account
at once. This is a real risk on a VPS that stays up for weeks, since a UI
restart (intentional or after a crash) is the most likely way to trigger it.

## What to build

Add a `kill_stale_processes()` step that runs automatically at the start
of `start_bot()`, **before** any new process is spawned. It should scan
the *entire* OS process table (not just this UI's own `self.*_proc`
handles) and kill anything whose command line matches one of this
project's own helper scripts — so a previous, orphaned session gets
cleaned up before a new one starts.

### Processes to match and kill (per the user's explicit scope choice)

- `xauusd_mt5_strategy.py` (the EA itself)
- `generate_dashboard.py` **only when invoked with `--watch`** (don't kill
  a one-off `python generate_dashboard.py` someone might be running by
  hand to generate a single snapshot)
- the dashboard web server script (`DASHBOARD_SERVER_SCRIPT_PATH`)
- the cloudflared tunnel executable (`CLOUDFLARED_EXE`, matched by exe
  name + `tunnel run {CLOUDFLARED_TUNNEL_NAME}` in its command line)
- `backup_restore.py` when invoked with `watch`

### Explicitly do NOT kill

- **MT5's own `terminal64.exe` / `terminal.exe`** — the user explicitly
  chose to scope this to the bot's own helper processes only, not MT5
  itself. Killing the terminal would close the user's charts/manual
  positions/other EAs too. Do not touch it even if it seems related.
- This UI process's own PID, obviously.
- Any python process that doesn't have one of the exact script paths
  above in its command line — never broadly kill `python.exe` by name
  alone, since the VPS may run other unrelated Python tools. Match on
  full command-line substring (the absolute or relative script path),
  not on process name.

## Implementation approach

Prefer `psutil` (cross-platform, gives you `.cmdline()` per process)
over shelling out to `tasklist`/`wmic`:

```python
def kill_stale_processes(self, log_fn=None):
    """Scans the OS process table and kills any leftover instance of this
    project's own helper scripts (EA, dashboard --watch, web server,
    cloudflared tunnel, backup watch) from a previous session that this
    UI process lost track of (e.g. after a UI restart/crash). Called from
    BOTH start_bot() (clean up before launching new ones) and stop_bot()
    (backstop sweep after the normal handle-based .terminate() loop).
    Never touches MT5's terminal.exe/terminal64.exe or unrelated
    processes -- matches strictly on full command-line script path. Logs
    every kill (PID + matched script) via log_fn if provided, never
    silent. Returns a list of (pid, cmdline_str) tuples for what it
    killed, so callers can report a count back to the user."""
    log_fn = log_fn or (lambda msg: None)
    killed = []
    try:
        import psutil
    except ImportError:
        log_fn("psutil not installed -- skipping stale-process cleanup (pip install psutil to enable)")
        return killed

    targets = [
        (BOT_SCRIPT_PATH, None),                       # any invocation
        (DASHBOARD_SCRIPT_PATH, "--watch"),            # only the watch loop
        (DASHBOARD_SERVER_SCRIPT_PATH, None),
        (BACKUP_SCRIPT_PATH, "watch"),
    ]
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == my_pid:
            continue
        cmdline = proc.info["cmdline"] or []
        cmdline_str = " ".join(cmdline)
        matched = False
        for script_path, required_arg in targets:
            if script_path and script_path in cmdline_str:
                if required_arg and required_arg not in cmdline_str:
                    continue
                matched = True
                break
        # cloudflared tunnel -- matched separately since it's not a python script
        if not matched and CLOUDFLARED_EXE.lower() in cmdline_str.lower() and CLOUDFLARED_TUNNEL_NAME in cmdline_str:
            matched = True
        if matched:
            log_fn(f"Killing stale process PID {proc.info['pid']}: {cmdline_str}")
            try:
                proc.kill()
                killed.append((proc.info["pid"], cmdline_str))
            except Exception as e:
                log_fn(f"  could not kill PID {proc.info['pid']}: {e}")
    return killed
```

Call it at the very top of `start_bot()`, before the existing
`if self.bot_proc is not None and self.bot_proc.poll() is None:` guard:

```python
def start_bot(self):
    if self.config_data.get("process_control", {}).get("kill_stale_on_start", True):
        killed = self.kill_stale_processes(log_fn=lambda msg: print(f"[start_bot] {msg}"))
        if killed:
            time.sleep(1)  # give the OS a moment to actually release the ports/handles
    if self.bot_proc is not None and self.bot_proc.poll() is None:
        ...
```

(`time.sleep(1)` is a small safety margin so a freshly-killed dashboard
web server actually releases its port before the new one tries to bind
it — adjust only if you see a real bind-error in testing.)

## Config flag (match the project's additive philosophy)

Add to `DEFAULT_CONFIG` in `strategy_config_ui.py`:

```python
"process_control": {
    "kill_stale_on_start": True,
},
```

And a checkbox near the Start/Stop Bot buttons:

```python
self.reg_bool(
    frame, "process_control", "kill_stale_on_start",
    "ฆ่าโปรเซสเก่าที่หลงเหลือทุกครั้งก่อนเริ่มบอท (EA/Dashboard/Web/Tunnel/Backup) — แนะนำให้เปิดไว้",
    row=<next available row>,
)
```

Default **True** (recommended on), but reversible without a code change —
matching `DAILY_FILTER_ENABLED` / `LOGIC_GROUP_SELECTION` /
`MYFXBOOK_ENABLED` etc.

## Wire it into `stop_bot()` too

Update `stop_bot()` to call the same `kill_stale_processes()` sweep
*after* its existing handle-based `.terminate()` loop, as a backstop —
keep the existing per-handle `.terminate()` first (it's the gentler,
graceful-shutdown path for processes this UI instance actually knows
about), then run the full OS-wide sweep to mop up anything orphaned:

```python
def stop_bot(self):
    for attr in ("bot_proc", "dashboard_proc", "web_server_proc", "tunnel_proc", "backup_proc"):
        proc = getattr(self, attr)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    self.bot_proc = None
    self.dashboard_proc = None
    self.web_server_proc = None
    self.tunnel_proc = None
    self.backup_proc = None

    # Backstop: also sweep the OS process table for anything this UI
    # instance lost track of (e.g. orphaned from an earlier UI crash/
    # restart) -- "Stop Bot" should guarantee everything is actually
    # dead, not just everything this particular window remembers.
    killed = self.kill_stale_processes(log_fn=lambda msg: print(f"[stop_bot] {msg}"))

    self.lbl_bot_status.config(text="EA Bot: ไม่ได้รัน")
    self.lbl_dash_status.config(text="   |   Dashboard (auto-refresh): ไม่ได้รัน")
    self.lbl_web_status.config(text=f"   |   Web (port {DASHBOARD_SERVER_PORT}): ไม่ได้รัน")
    self.lbl_tunnel_status.config(text="   |   Cloudflare Tunnel: ไม่ได้รัน")
    self.lbl_backup_status.config(text="   |   Auto-Backup: ไม่ได้รัน")
    self.btn_start.config(state="normal")
    self.btn_stop.config(state="disabled")
```

Make `kill_stale_processes()` return a count/list of what it killed (not
just log it), so `stop_bot()` can optionally show a one-line confirmation
if it actually had to clean up an orphan (e.g. a `messagebox.showinfo` or
a status-bar note: "พบและปิดโปรเซสที่หลงเหลือ N รายการ" / "found and closed N
leftover process(es)") — this surfaces the orphan situation to the user
instead of silently fixing it, since an orphan happening at all is a sign
something (a crash, a force-quit) went wrong earlier and is worth noticing.

Do NOT call `kill_stale_processes()` unconditionally on every `stop_bot()`
without the `process_control.kill_stale_on_start` style flag in mind —
reuse the same config flag (rename it something like
`"kill_stale_on_start_and_stop"` if you want one flag governing both call
sites, or add a second flag if the user might want them independent;
default both to `True`, but make this controllable, consistent with the
project's "everything is a reversible flag" philosophy).

## Also add a manual "Force Kill Stale Processes" button (optional but recommended)

A standalone button next to Start/Stop Bot that just calls
`kill_stale_processes()` directly, with a confirmation dialog and a
results summary (how many processes were killed, listing PID + matched
script) — useful for manually cleaning up after a crash without having
to also restart the bot at the same moment.

## Dependency

This needs `psutil` (`pip install psutil`). It's a new dependency for
this project — note it in `claude_code_vps_setup.md` / wherever the
Windows VPS install steps live, and make `kill_stale_processes()` degrade
gracefully (skip with a logged warning, never crash `start_bot()`) if
`psutil` isn't installed yet, consistent with this project's
"never block the EA" philosophy used everywhere else (Telegram, macro
data fetches, Myfxbook sentiment, etc.).

## Verification before calling this done

1. Syntax-check the edited file (`ast.parse` or `py_compile`).
2. Manually simulate the orphan scenario: start the bot, then kill the UI
   process itself (not via Stop Bot) so `self.bot_proc` is lost, relaunch
   the UI, click Start Bot again, and confirm via Task Manager / `tasklist`
   that only ONE `xauusd_mt5_strategy.py` process remains running
   afterward, not two.
3. Simulate the same orphan scenario but this time click **Stop Bot** on
   the fresh UI instance (which has no tracked handles) and confirm via
   Task Manager / `tasklist` that the orphaned EA/dashboard/web/tunnel/
   backup processes from the killed UI's session are ALL actually gone
   afterward — not just that the status labels reset to "ไม่ได้รัน".
4. Confirm MT5's own terminal process is untouched throughout both tests.
5. Confirm the checkbox round-trips correctly through save/load
   (`strategy_config.json`'s new `"process_control"` section).
6. Report whether `psutil` was already installed in this environment or
   needs adding to the VPS setup steps.
