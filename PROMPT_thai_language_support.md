# Prompt for Claude Code — make sure Thai is read/written correctly everywhere in the bot

Paste this into Claude Code in the bot's project folder (local and/or VPS
copy — the VPS is the one that actually runs 24/7 on a Windows console, so
it's the higher-priority target, but keep both in sync).

## Why this matters

This project already has Thai-language UI labels (`strategy_config_ui.py`)
and Thai status text on the dashboard, but Thai text support has never been
explicitly audited end-to-end. The places most likely to silently break are:
Windows console output (codepage mismatches cause crashes or garbled boxes),
Tkinter font rendering (the default Tk font on some Windows builds doesn't
include Thai glyphs, showing empty boxes instead of readable text), and any
file write that's missing an explicit UTF-8 encoding. None of these are
hypothetical — they're the standard gotchas anyone hits running Thai text
on a Windows box. Do a full audit and fix what's actually broken; don't
assume something is fine just because no one's reported it yet.

## 1. Tkinter UI (`strategy_config_ui.py`) — make sure Thai actually renders, not boxes

Today there's no explicit `font=` set anywhere in this file — every label
relies on Tk's bundled default font, which can fail to render Thai glyphs
(shows empty boxes / "tofu") depending on the Tcl/Tk build bundled with the
Python install on a given Windows machine.

Add an explicit, Thai-capable default font app-wide, applied once near
the top of the app's `__init__`/setup, e.g.:

```python
import tkinter.font as tkfont

def _setup_thai_font(self):
    """Windows ships 'Leelawadee UI' as the standard Thai-capable system
    font since Windows 10 -- covers Thai, English, and digits cleanly in
    one font so labels don't mix fallback fonts mid-string. Falls back to
    Tahoma (older but still has full Thai coverage) if Leelawadee UI isn't
    available (e.g. running this UI on macOS/Linux during dev), then to
    whatever Tk's default already was if neither exists."""
    candidates = ["Leelawadee UI", "Tahoma"]
    available = set(tkfont.families())
    chosen = next((f for f in candidates if f in available), None)
    if chosen:
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=chosen, size=10)
            except Exception:
                pass
        style = ttk.Style()
        style.configure(".", font=(chosen, 10))
```

Call `self._setup_thai_font()` right after the root window is created,
before any tabs/widgets are built. Verify by actually looking at the
rendered UI (or describing what you see) — Thai labels should show real
Thai characters, not empty boxes or "?" placeholders.

## 2. Console/log output (`xauusd_mt5_strategy.py`) — don't crash or garble on Thai

`setup_logging()`'s `console_handler = logging.StreamHandler()` (around
line 484) writes to whatever `sys.stdout`'s encoding already is. On a
Windows console NOT running in UTF-8 mode (the default codepage is
locale-dependent, e.g. cp874 for a Thai locale or cp1252 for an English
one), printing a Thai string can either:
- raise `UnicodeEncodeError` and crash the EA's logging call entirely, or
- print garbled/mojibake characters instead of real Thai.

The file handler already correctly specifies `encoding="utf-8"` — only
the console path is unguarded. Fix:

```python
import io

def _safe_console_stream():
    """Wraps stdout in a UTF-8 TextIOWrapper with errors='backslashreplace'
    so a Thai (or any non-ASCII) log message NEVER crashes the EA's logging
    call on a Windows console that isn't already running in UTF-8 mode --
    worst case it prints escaped bytes instead of a real character, but the
    process keeps running. Also recommend `chcp 65001` before launching the
    bot in PowerShell so the console renders Thai correctly instead of just
    not crashing."""
    try:
        return io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="backslashreplace")
    except Exception:
        return sys.stdout
```

Use `_safe_console_stream()` when constructing `console_handler`. Also
add a one-line note to `claude_code_vps_setup.md` (or wherever the VPS
PowerShell launch steps live): run `chcp 65001` once per PowerShell
session before starting the bot, so Thai actually displays correctly in
the console window, not just safely.

## 3. File I/O audit — every write that could ever contain Thai text must be UTF-8 + `ensure_ascii=False`

Audit every `open(..., "w")` and `json.dump(...)` call across
`xauusd_mt5_strategy.py`, `strategy_config_ui.py`, `macro_data.py`,
`league.py`, `strategy_simulator.py`, `generate_dashboard.py`,
`backup_restore.py`. For each:

- File opens must specify `encoding="utf-8"` explicitly — never rely on
  the OS default (which is NOT UTF-8 by default on Windows).
- `json.dump`/`json.dumps` calls must pass `ensure_ascii=False` if the
  data being written could ever contain Thai text (strategy notes,
  config labels, UI-entered text) — without it, Thai characters get
  escaped to `\uXXXX` sequences. This doesn't break correctness (it still
  round-trips fine through `json.load`), but it makes the raw file
  unreadable to a human and should be fixed for consistency with the
  rest of this codebase (`macro_data.py` and `strategy_config_ui.py`
  already do this correctly — match that pattern).

One known gap already found: `xauusd_mt5_strategy.py`'s `json.dump(data,
f, indent=2, default=str)` (around line 1506, writes
`strategy_scores.json`) does not set `ensure_ascii=False`. Add it there.

## 4. Telegram alerts (`telegram_alert.py`) — verify, likely already fine

`send_message()` already does `.encode("utf-8")` on the outgoing payload,
which is correct — Telegram's API expects UTF-8. No code change expected
here, but add one verification step: send (or dry-run) a test message
containing a literal Thai sentence and confirm it doesn't get mangled
either in the API request or when displayed in Telegram. If anything's
wrong here, it's far more likely in how the message *string* was built
upstream (e.g. an f-string in an account that prints to a non-UTF-8
console first) than in this file.

## 5. Dashboard HTML (`generate_dashboard.py`) — verify, likely already fine

Already has `<meta charset="UTF-8">` and `font-family` already lists
`"Noto Sans Thai"`. Just confirm the generated `dashboard.html` actually
renders Thai status text correctly in a browser (it should, given the
existing setup) — no change expected unless testing reveals otherwise.

## Verification checklist before calling this done

1. Run the UI (`strategy_config_ui.py`) and visually confirm every
   existing Thai label renders as real Thai text, not boxes.
2. Type a Thai sentence into a text `Entry` field (e.g. nothing currently
   takes free-text Thai input except maybe nowhere yet — if no field
   currently accepts free Thai text, skip this sub-step and note it).
3. Start the bot on a Windows console WITHOUT `chcp 65001` run first, and
   confirm a log line containing Thai text does not crash the process
   (worst case: escaped bytes printed, not a traceback).
4. Confirm `strategy_scores.json` and `strategy_config.json` round-trip
   correctly (`json.load` after `json.dump`) and contain readable Thai
   (not `\uXXXX` escapes) after the `ensure_ascii=False` fix.
5. `ast.parse`/`py_compile` every edited file.
6. Report back exactly which of the 5 areas above already worked
   correctly vs. which needed a real fix — don't assume problems existed
   where testing shows they didn't.
