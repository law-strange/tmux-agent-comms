#!/usr/bin/env python3
"""tmux-agent-comms — a discreet inter-CLI message bus over tmux.

Lets terminal-based AI agents/CLIs (Claude Code, Codex, Grok, Aider, etc.) that
each run in their own tmux session send messages to one another by injecting
text into the target session's input (tmux send-keys). Registry-driven: adding a
new CLI is a single entry, no code changes.

Design goals:
- Zero dependencies (Python 3.9+ stdlib only).
- Registry-driven: any CLI is added with one `add` command or one JSON entry.
- Sender auto-detection: a message is tagged with who sent it, derived from the
  caller's own tmux session — no need to pass --from when run inside a session.
- Discreet: minimal stdout, a private append-only audit log, no external network.
- Literal-safe injection: message text is sent with `send-keys -l` so contents
  are never interpreted as tmux key names.

Usage:
    agent_comms send <to> <message...>
    agent_comms broadcast <message...>
    agent_comms list
    agent_comms add <name> <tmux-session> [--color C] [--desc D] [--revive CMD]
    agent_comms remove <name>
    agent_comms enable|disable <name>
    agent_comms whoami
    agent_comms log [-n N]
    agent_comms init        # write a starter registry if none exists

Config (all overridable by env):
    AGENT_COMMS_REGISTRY   default ~/.config/agent-comms/registry.json
    AGENT_COMMS_LOG        default ~/.config/agent-comms/comms.log
    AGENT_COMMS_SELF       override sender identity
    AGENT_COMMS_TMUX       tmux binary (else autodetected via PATH)
    AGENT_COMMS_MARKER     message prefix marker (default «agent-msg»)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # Unix (Linux/macOS/WSL — all supported platforms). Absent only on native Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

# ---------- config ----------
CONFIG_DIR = Path(os.path.expanduser("~/.config/agent-comms"))
REGISTRY_PATH = Path(
    os.environ.get("AGENT_COMMS_REGISTRY", str(CONFIG_DIR / "registry.json"))
)
LOG_PATH = Path(os.environ.get("AGENT_COMMS_LOG", str(CONFIG_DIR / "comms.log")))
THREADS_DIR = Path(os.environ.get("AGENT_COMMS_THREADS", str(CONFIG_DIR / "threads")))
PROJECTS_DIR = Path(
    os.environ.get("AGENT_COMMS_PROJECTS", str(CONFIG_DIR / "projects"))
)
JOIN_DOC = Path(
    os.environ.get("AGENT_COMMS_JOIN_DOC", str(CONFIG_DIR / "coms_join.md"))
)
MARKER = os.environ.get("AGENT_COMMS_MARKER", "«agent-msg»")

# ---------- tunable constants (env-overridable) ----------
# Delay between sending the literal text and the Enter keypress. Required because
# some TUIs (e.g. Codex) batch via bracketed paste; without a gap the Enter is
# absorbed as a newline and the message never submits. Per-agent override via the
# registry/roster `enter_delay` field; this is the global default.
ENTER_DELAY = float(os.environ.get("AGENT_COMMS_ENTER_DELAY", "0.4"))
# Delivery confirmation: after injecting, capture the bottom N lines of the target
# pane and check whether the marker is still sitting there (= unsubmitted, stuck in
# the composer) vs gone (= submitted). A submit heuristic, NOT an IPC ack — it
# verifies the keystrokes landed + submitted, not that the agent read the message.
VERIFY_LINES = int(os.environ.get("AGENT_COMMS_VERIFY_LINES", "3"))
# Seconds to wait after Enter before capturing for verification (lets the TUI redraw).
VERIFY_DELAY = float(os.environ.get("AGENT_COMMS_VERIFY_DELAY", "0.3"))
# Max chars per injected message — guards against a buggy/abusive agent spamming
# a giant paste into another agent's pane. Longer messages are truncated.
MAX_MESSAGE_LEN = int(os.environ.get("AGENT_COMMS_MAX_MSG_LEN", "4000"))
# Append-lock acquisition timeout (seconds) before proceeding best-effort.
LOCK_TIMEOUT = float(os.environ.get("AGENT_COMMS_LOCK_TIMEOUT", "5"))
# Seconds to wait after running a revive_cmd before re-checking the session.
REVIVE_GRACE = float(os.environ.get("AGENT_COMMS_REVIVE_GRACE", "6"))
# Timeout (seconds) for a revive_cmd shell invocation.
REVIVE_TIMEOUT = float(os.environ.get("AGENT_COMMS_REVIVE_TIMEOUT", "30"))
# Loop guard (non-blocking): if the same two agents exchange at least this many
# doorbells within the window, the doorbell carries a "rapid back-and-forth"
# warning so the receiving agent knows to stop unless a reply is essential.
LOOP_WARN_COUNT = int(os.environ.get("AGENT_COMMS_LOOP_WARN_COUNT", "4"))
LOOP_WINDOW_SEC = float(os.environ.get("AGENT_COMMS_LOOP_WINDOW_SEC", "180"))

# Instructions an AI follows when the user pastes `read <coms_join.md>` into it.
JOIN_DOC_TEXT = """# Join the agent comms (instructions for the AI reading this)

You are an AI assistant running in a tmux session. The user wants you to join a
shared multi-agent comms channel so you can talk to other AI agents. Do this now:

1. Get the PROJECT name. If the user already gave you one (e.g. they said
   "register under demo"), just use it. Otherwise ask EXACTLY one short question
   and wait: **"What project should I register under?"** Take their one-word
   answer as-is. Do NOT brainstorm, suggest, or list candidate names — just use
   what they type.

2. Register yourself by running this shell command (fill in the blanks):
   ```
   {cmd} register --project PROJECT --as <your-name> --model <your-model> --role "<what you're good at>"
   ```
   - `<your-name>`: a short label, e.g. `claude`, `grok`, `codex`.
   - `<your-model>`: your actual model id if you know it (e.g. `claude-opus-4-7`,
     `gpt-5-codex`, `grok-4`); otherwise `unknown`.
   - `<role>`: one phrase on your strengths, e.g. "orchestration + reasoning",
     "web research", "multi-file builds".
   The tool auto-detects your tmux session and handles name collisions.
   If it replies "couldn't detect your tmux session" (some CLIs sandbox the
   shell env, e.g. Codex), re-run adding `--session <your tmux session name>`
   — ask the user for the session name, or they can find it with `tmux ls`.
   If messages later pile up in your input box unsent (slow TUI), re-register
   with a longer `--enter-delay 0.8`.

3. See who else is here:  `{cmd} roster PROJECT`

4. To message the project — prefer `post`, it threads + has loop protection:
   - Everyone:        `{cmd} post PROJECT "your message"`
   - One teammate:    `{cmd} post PROJECT "your message" --to <member>`
   (`{cmd} send <member> "..."` also works for a quick 1:1, but `post --to` is
   preferred — it logs to the shared thread and is loop-guarded.)

5. When you receive a line beginning with the comms marker, READ the referenced
   thread. Then follow the ETIQUETTE below to decide whether to reply.

6. If you ever switch to a different model, re-run the `register` command so the
   roster stays accurate.

## COMMS ETIQUETTE — READ THIS (it prevents infinite loops)

Treat this channel like **email/an inbox, not a chat. Silence is the default.**
Two eager agents that reply to every message will loop forever. So:

- **Reply ONLY if** the message asks you a direct question, needs an action from
  you, or you have substantive new information. Otherwise **do not reply at all.**
- **NEVER reply just to acknowledge** ("thanks", "ok", "got it", "sounds good",
  "great work"). An acknowledgement is a loop. Just stay silent.
- **Do not end your message with a question or prompt** unless you genuinely need
  an answer — that invites an unnecessary reply.
- If you DO need to send something that needs no response, end it with the tag
  **`[no-reply]`** so the recipient knows the thread is closed.
- If you receive a message tagged `[no-reply]`, or a doorbell that says
  **"(fyi)"**, do NOT reply.
- If you notice you and one other agent have gone back and forth more than ~2-3
  times, STOP — assume the human will steer; do not send another reply.

When in doubt: **don't reply.** A quiet channel is a healthy channel.

That's it -- you're now part of the project's agent comms.
"""


def tmux_bin() -> str:
    # PATH-resolved tmux first; bare "tmux" is the platform-neutral fallback.
    # Override with AGENT_COMMS_TMUX for a non-standard install.
    return os.environ.get("AGENT_COMMS_TMUX") or shutil.which("tmux") or "tmux"


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------- registry ----------
STARTER_REGISTRY = {
    "agents": {
        "claude": {
            "session": "claude",
            "color": "red",
            "enabled": True,
            "desc": "Claude Code",
            "revive_cmd": None,
        },
        "codex": {
            "session": "codex",
            "color": "blue",
            "enabled": True,
            "desc": "Codex CLI",
            "revive_cmd": None,
        },
        "grok": {
            "session": "grok",
            "color": "magenta",
            "enabled": True,
            "desc": "Grok CLI",
            "revive_cmd": None,
        },
    }
}


def load_registry() -> dict:
    """Non-fatal: a missing registry returns an empty one. The global registry is
    only needed for send/broadcast/list-style commands; project posts use rosters.
    Commands that need agents report 'unknown agent' on their own."""
    if not REGISTRY_PATH.exists():
        return {"agents": {}}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except Exception as e:
        sys.stderr.write(f"registry parse error: {e}\n")
        sys.exit(2)


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(REGISTRY_PATH)


def agents(reg: dict) -> dict:
    return reg.setdefault("agents", {})


# ---------- tmux ----------
def session_exists(session: str) -> bool:
    try:
        return (
            subprocess.run(
                [tmux_bin(), "has-session", "-t", session],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


def current_session() -> str | None:
    """Detect the tmux session this process is running inside."""
    try:
        r = subprocess.run(
            [tmux_bin(), "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        s = r.stdout.strip()
        return s or None
    except Exception:
        return None


def inject(session: str, text: str, enter_delay: float | None = None) -> bool:
    """Type `text` into the session's active pane, then submit with Enter.
    Uses send-keys -l (literal) so message content is never parsed as keys.

    A delay between the text and the Enter is REQUIRED for TUIs that use
    bracketed paste (e.g. Codex): without it the Enter gets batched into the
    paste and becomes a newline instead of a submit, so messages pile up in the
    composer unsent. The delay lets the Enter land as a discrete submit keypress.

    NOTE (known limitation): this delay is a heuristic. On a heavily loaded or
    slow machine, or with an unusually slow TUI, it can still be too short and
    the message won't submit. Bump the per-agent `enter_delay` (registry/roster)
    or AGENT_COMMS_ENTER_DELAY for that environment. We deliberately do NOT
    auto-retry a non-submit, because re-sending risks a double-submit."""
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 3] + "..."
    delay = ENTER_DELAY if enter_delay is None else enter_delay
    try:
        subprocess.run(
            [tmux_bin(), "send-keys", "-t", session, "-l", text],
            check=True,
            capture_output=True,
            timeout=10,
        )
        time.sleep(delay)
        subprocess.run(
            [tmux_bin(), "send-keys", "-t", session, "Enter"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except Exception:
        return False


def capture_pane_tail(session: str, lines: int = VERIFY_LINES) -> str | None:
    """Return the last `lines` non-empty rows of the target pane's visible
    content, or None if capture fails (no tmux / bad session)."""
    try:
        out = subprocess.run(
            [tmux_bin(), "capture-pane", "-t", session, "-p"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return None
    rows = [r for r in out.splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def verify_submitted(session: str, marker: str = MARKER) -> bool | None:
    """Heuristic delivery check. A SUBMITTED message clears the composer, so the
    marker is no longer pinned to the bottom input lines; an UNSUBMITTED message
    sits literally in the composer at the bottom of the pane. Returns:
      True  -> submitted (marker gone from the bottom rows)
      False -> still in composer (marker present at the bottom)
      None  -> couldn't capture (treat as unverified, not a hard failure).
    This verifies *submission*, not that the agent read/understood the message —
    a capture-pane heuristic, not an IPC acknowledgement. Widen/narrow the window
    with AGENT_COMMS_VERIFY_LINES if your TUI echoes the submitted line low."""
    time.sleep(VERIFY_DELAY)
    tail = capture_pane_tail(session)
    if tail is None:
        return None
    return marker not in tail


# ---------- identity ----------
def detect_self(reg: dict, override: str | None) -> str:
    if override:
        return override
    if os.environ.get("AGENT_COMMS_SELF"):
        return os.environ["AGENT_COMMS_SELF"]
    sess = current_session()
    if sess:
        for name, meta in agents(reg).items():
            if meta.get("session") == sess:
                return name
        return sess  # in tmux but not registered — use raw session name
    return "unknown"


# ---------- audit log ----------
def audit(sender: str, target: str, status: str, message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_utc()}\t{sender}→{target}\t{status}\t{message}\n")
    except OSError:
        pass


def recent_exchange_count(a: str, b: str, window_sec: float = LOOP_WINDOW_SEC) -> int:
    """How many DOORBELLs flew between `a` and `b` (either direction) in the last
    `window_sec`, per the audit log. Loop-guard signal; reads only the log tail."""
    if not LOG_PATH.exists():
        return 0
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - window_sec
    pair, n = {a, b}, 0
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 3 or not parts[2].startswith("DOORBELL"):
            continue
        if "→" not in parts[1]:
            continue
        frm, to = parts[1].split("→", 1)
        if {frm, to} != pair:
            continue
        try:
            t = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t >= cutoff:
            n += 1
    return n


# ---------- commands ----------
def tagged(sender: str, message: str) -> str:
    return f"{MARKER} from={sender} {datetime.now(timezone.utc).strftime('%H:%MZ')} | {message}"


def ensure_target(reg: dict, target: str) -> tuple[str | None, str]:
    """Resolve an agent to a live tmux session, reviving if needed.
    Returns (session, "OK") or (None, reason)."""
    meta = agents(reg).get(target)
    if meta is None:
        return None, "UNKNOWN"
    if not meta.get("enabled", True):
        return None, "DISABLED"
    sess = meta["session"]
    if not session_exists(sess):
        rev = meta.get("revive_cmd")
        if rev:
            # SECURITY: revive_cmd runs via the shell. It comes from the user's
            # own registry (trusted config) — never from a message. See the
            # README "Trust model" section. Failures are surfaced, not swallowed.
            try:
                r = subprocess.run(
                    rev,
                    shell=True,
                    timeout=REVIVE_TIMEOUT,
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0:
                    sys.stderr.write(
                        f"revive '{target}' failed rc={r.returncode}: {(r.stderr or '').strip()[:200]}\n"
                    )
                time.sleep(REVIVE_GRACE)
            except Exception as e:
                sys.stderr.write(f"revive '{target}' raised: {str(e)[:200]}\n")
        if not session_exists(sess):
            return None, "SESSION_DOWN"
    return sess, "OK"


def find_member_session(name: str):
    """Search all project rosters for a member named `name`. Self-registered
    agents live in rosters, NOT the global registry, so `send`/`--to` must look
    here too. Returns (session, enter_delay, project) for the first match."""
    if not PROJECTS_DIR.is_dir():
        return None, None, None
    for f in sorted(PROJECTS_DIR.glob("*.json")):
        if f.name.startswith("._"):
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        m = data.get("members", {}).get(name)
        if m and m.get("session"):
            return m["session"], m.get("enter_delay"), data.get("project", f.stem)
    return None, None, None


def all_member_names() -> list[str]:
    names: set[str] = set()
    if PROJECTS_DIR.is_dir():
        for f in PROJECTS_DIR.glob("*.json"):
            if f.name.startswith("._"):
                continue
            try:
                names.update(json.loads(f.read_text()).get("members", {}).keys())
            except Exception:
                pass
    return sorted(names)


def deliver(
    reg: dict,
    sender: str,
    target: str,
    message: str,
    quiet: bool,
    confirm: bool = False,
) -> bool:
    # 1) global registry; 2) fall back to project-roster members (self-reg flow).
    sess, reason = ensure_target(reg, target)
    delay = agents(reg).get(target, {}).get("enter_delay")
    if sess is None and reason == "UNKNOWN":
        msess, mdelay, _proj = find_member_session(target)
        if msess:
            delay = mdelay
            sess, reason = (
                (msess, "OK") if session_exists(msess) else (None, "SESSION_DOWN")
            )
    if sess is None:
        pool = list(agents(reg)) + all_member_names()
        sys.stderr.write(
            f"{reason.lower().replace('_', ' ')}: {target}{did_you_mean(target, pool)}\n"
        )
        audit(sender, target, reason, message)
        return False
    ok = inject(sess, tagged(sender, message), enter_delay=delay)
    status = "DELIVERED" if ok else "INJECT_FAIL"
    note = ""
    if ok and confirm:
        sub = verify_submitted(sess)
        if sub is False:
            ok, status = False, "UNSUBMITTED"
            note = " — text sits in composer; raise this agent's enter_delay"
        elif sub is None:
            status = "DELIVERED_UNVERIFIED"
            note = " (could not capture pane to confirm)"
    audit(sender, target, status, message)
    if not quiet:
        verb = {
            "DELIVERED": "delivered",
            "DELIVERED_UNVERIFIED": "sent (unverified)",
            "UNSUBMITTED": "UNSUBMITTED",
            "INJECT_FAIL": "FAILED",
        }[status]
        print(f"{verb}: {sender} -> {target} ({sess}){note}")
    return ok


# ---------- threads (md discussion files + doorbell) ----------
def safe_name(name: str) -> str:
    """Sanitize a thread/project name so it can't escape its base dir. Strips a
    trailing .md, rejects absolute paths + '..' segments (path traversal)."""
    n = name[:-3] if name.endswith(".md") else name
    n = n.strip().lstrip("/")
    parts = [seg for seg in n.split("/") if seg not in ("", ".", "..")]
    cleaned = "/".join(parts)
    if not cleaned:
        raise ValueError(f"invalid name: {name!r}")
    return cleaned


def thread_path(name: str) -> Path:
    """threads/<name>.md — name may contain '/' for workstream subdirs (sanitized)."""
    return THREADS_DIR / (safe_name(name) + ".md")


def append_thread(
    thread: str, sender: str, to: str | None, message: str, model: str | None = None
) -> Path:
    """Append a timestamped, tagged entry. Concurrency-safe via fcntl.flock, which
    auto-releases when the process exits — so a crashed writer can't strand the
    lock (the old mkdir approach could). On non-Unix (no fcntl) or lock timeout we
    proceed best-effort: a single append is small and usually atomic anyway."""
    p = thread_path(thread)
    p.parent.mkdir(parents=True, exist_ok=True)
    if len(message) > MAX_MESSAGE_LEN:
        message = message[: MAX_MESSAGE_LEN - 3] + "..."
    addr = f"{sender}→{to}" if to else sender
    if model and model != "unknown":
        addr += f" ({model})"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    entry = f"\n## [{addr} {stamp}]\n{message}\n"

    lockf = p.with_suffix(".md.lock")
    lf = None
    if fcntl is not None:
        try:
            lf = open(lockf, "w")
            deadline = time.time() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        break  # best-effort: proceed without the lock
                    time.sleep(0.05)
        except OSError:
            lf = None
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
                lf.close()
            except OSError:
                pass
    return p


def cmd_post(args, reg):
    sender = detect_self(reg, args.frm)
    msg = " ".join(args.message)
    my_session = current_session()

    # Project-aware: if a roster exists for this name, treat it as a project.
    roster = load_roster(args.thread)
    members = roster.get("members", {})

    # sender's model (from roster, by matching this session) for thread attribution
    model = None
    sender_member = None
    for mname, m in members.items():
        if m.get("session") and m.get("session") == my_session:
            model, sender_member = m.get("model"), mname
            break
    if sender_member:  # prefer the project member name over raw session name
        sender = sender_member

    p = append_thread(args.thread, sender, args.to, msg, model)
    # Base doorbell pointer. Per-target loop warning is appended in the loop below.
    base_pointer = f"{MARKER} {sender} posted in {args.thread} — review it ({p})"
    if "[no-reply]" in msg.lower():
        base_pointer += " (no reply needed)"

    # Resolve doorbell targets to (label, session, enter_delay) tuples.
    targets: list[tuple[str, str, float | None]] = []
    if args.to:
        if args.to in members:
            m = members[args.to]
            targets = [(args.to, m["session"], m.get("enter_delay"))]
        elif members:
            # PROJECT post + recipient is NOT a project member.
            # v0.3.0: do NOT silently fall back to the global registry. That
            # cross-project leak routed messages to the wrong same-named agent
            # (e.g. a marketing 'codex' post hitting the PMXT Codex builder).
            # A project post stays inside the project; to reach a fleet/other-
            # project agent, use `agent_comms send <name>` explicitly.
            sys.stderr.write(
                f"'{args.to}' is not a member of project '{args.thread}' "
                f"(members: {', '.join(members) or '(none)'}).\n"
                f'For a fleet/other-project agent use:  agent_comms send {args.to} "..."\n'
            )
            sys.exit(1)
        elif args.to in agents(reg):
            # No project roster for this thread -> registry-addressed post (back-compat).
            sess, reason = ensure_target(reg, args.to)
            if sess is None:  # disabled or session down — do NOT inject
                sys.stderr.write(f"{reason.lower().replace('_', ' ')}: {args.to}\n")
                audit(sender, args.to, reason, msg)
                sys.exit(1)
            am = agents(reg)[args.to]
            targets = [(args.to, sess, am.get("enter_delay"))]
        else:
            pool = list(members) + list(agents(reg))
            sys.stderr.write(
                f"unknown recipient '{args.to}'{did_you_mean(args.to, pool)}\n"
            )
            sys.exit(1)
    elif members:  # project post -> all members except self
        # NOTE: project membership is independent of the registry `enabled` flag.
        # Registering into a project = you are a participant, period. We do NOT
        # consult registry `enabled` here on purpose — disabling an agent in the
        # global registry shouldn't silently drop it from a project it joined.
        targets = [
            (n, m["session"], m.get("enter_delay"))
            for n, m in members.items()
            if n != sender_member
        ]
    else:  # fall back to registry broadcast (here `enabled` DOES gate)
        targets = [
            (n, m["session"], m.get("enter_delay"))
            for n, m in agents(reg).items()
            if m.get("enabled", True) and n != sender
        ]

    rung = 0
    for label, sess, delay in targets:
        pointer = base_pointer
        # Non-blocking loop guard: warn if sender<->label are ping-ponging.
        if recent_exchange_count(sender, label) >= LOOP_WARN_COUNT:
            pointer += (
                f" [⚠ loop guard: {sender}<->{label} have exchanged several"
                " messages rapidly — do NOT reply unless it's essential]"
            )
        if sess and session_exists(sess) and inject(sess, pointer, enter_delay=delay):
            if getattr(args, "confirm", False) and verify_submitted(sess) is False:
                audit(sender, label, f"DOORBELL_UNSUBMITTED:{args.thread}", msg)
                if not args.quiet:
                    sys.stderr.write(
                        f"  unsubmitted doorbell -> {label} ({sess}); raise its enter_delay\n"
                    )
            else:
                rung += 1
                audit(sender, label, f"DOORBELL:{args.thread}", msg)
        else:
            audit(sender, label, f"DOORBELL_FAIL:{args.thread}", msg)
    if not args.quiet:
        scope = "project" if members else "registry"
        verified = " (confirmed)" if getattr(args, "confirm", False) else ""
        print(f"posted to {p}  | doorbell {rung}/{len(targets)} ({scope}){verified}")
    sys.exit(0 if (rung or not targets) else 1)


def cmd_read(args, _reg):
    p = thread_path(args.thread)
    if not p.exists():
        sys.stderr.write(f"no thread: {p}\n")
        sys.exit(1)
    text = p.read_text(encoding="utf-8")
    if args.n:
        # print last N entries (split on the '## [' entry marker)
        parts = text.split("\n## [")
        tail = parts[-args.n :] if len(parts) > args.n else parts[1:]
        print("## [" + "\n## [".join(tail) if tail else text)
    else:
        print(text)


def cmd_threads(args, _reg):
    if not THREADS_DIR.is_dir():
        print("(no threads yet)")
        return
    files = sorted(THREADS_DIR.rglob("*.md"))
    if not files:
        print("(no threads yet)")
        return
    for f in files:
        name = str(f.relative_to(THREADS_DIR))[:-3]
        entries = f.read_text(encoding="utf-8").count("\n## [")
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime(
            "%m-%d %H:%MZ"
        )
        print(f"{name:<32}{entries:>4} entries   last {mtime}")


def cmd_send(args, reg):
    sender = detect_self(reg, args.frm)
    msg = " ".join(args.message)
    ok = deliver(reg, sender, args.to, msg, args.quiet, confirm=args.confirm)
    sys.exit(0 if ok else 1)


def cmd_broadcast(args, reg):
    sender = detect_self(reg, args.frm)
    msg = " ".join(args.message)
    targets = [
        n for n, m in agents(reg).items() if m.get("enabled", True) and n != sender
    ]
    fails = 0
    for t in targets:
        if not deliver(reg, sender, t, msg, args.quiet, confirm=args.confirm):
            fails += 1
    if not args.quiet:
        print(
            f"broadcast: {len(targets)-fails}/{len(targets)} delivered (from {sender})"
        )
    sys.exit(1 if fails else 0)


def cmd_list(args, reg):
    me = detect_self(reg, None)
    print(f"{'AGENT':<16}{'SESSION':<16}{'STATUS':<8}{'EN':<4}DESC")
    for name, m in agents(reg).items():
        up = "up" if session_exists(m["session"]) else "down"
        en = "y" if m.get("enabled", True) else "n"
        mark = " *self" if name == me else ""
        print(f"{name:<16}{m['session']:<16}{up:<8}{en:<4}{m.get('desc','')}{mark}")


def cmd_add(args, reg):
    a = agents(reg)
    a[args.name] = {
        "session": args.session,
        "color": args.color,
        "enabled": True,
        "desc": args.desc or "",
        "revive_cmd": args.revive,
    }
    save_registry(reg)
    print(f"added: {args.name} -> session '{args.session}'")


def cmd_remove(args, reg):
    a = agents(reg)
    if args.name in a:
        del a[args.name]
        save_registry(reg)
        print(f"removed: {args.name}")
    else:
        sys.stderr.write(f"not found: {args.name}\n")
        sys.exit(1)


def cmd_toggle(args, reg, enabled):
    a = agents(reg)
    if args.name not in a:
        sys.stderr.write(f"not found: {args.name}\n")
        sys.exit(1)
    a[args.name]["enabled"] = enabled
    save_registry(reg)
    print(f"{'enabled' if enabled else 'disabled'}: {args.name}")


def cmd_whoami(args, reg):
    print(detect_self(reg, args.frm))


def cmd_log(args, _reg):
    if not LOG_PATH.exists():
        print("(no comms log yet)")
        return
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    for ln in lines[-args.n :]:
        print(ln)


def cmd_init(args, _reg):
    if REGISTRY_PATH.exists() and not args.force:
        sys.stderr.write(f"registry already exists at {REGISTRY_PATH} (use --force)\n")
        sys.exit(1)
    save_registry(STARTER_REGISTRY)
    print(
        f"wrote starter registry -> {REGISTRY_PATH}\nedit it to match your tmux sessions."
    )


# ---------- projects: self-registration rosters ----------
def roster_path(project: str) -> Path:
    # safe_name() guards against path traversal (../, absolute) — a project name
    # comes from `register --project`, which is effectively user input.
    return PROJECTS_DIR / (safe_name(project) + ".json")


def load_roster(project: str) -> dict:
    p = roster_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"project": project, "members": {}}


def save_roster(roster: dict) -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    p = roster_path(roster["project"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roster, indent=2))
    tmp.replace(p)


def unique_member_name(members: dict, base: str, my_session: str) -> str:
    """If `base` is taken by a DIFFERENT session, suffix -2, -3, … . If the same
    session re-registers, reuse its existing name (idempotent)."""
    for name, m in members.items():
        if m.get("session") == my_session:
            return name  # this session already registered — keep its name
    if base not in members:
        return base
    i = 2
    while f"{base}-{i}" in members:
        i += 1
    return f"{base}-{i}"


def did_you_mean(name: str, candidates) -> str:
    """Cheap suggestion: closest candidate by simple ratio."""
    import difflib

    hit = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.5)
    return f" — did you mean '{hit[0]}'?" if hit else ""


def cmd_register(args, _reg):
    # Prefer explicit --session (some CLIs, e.g. Codex, run shell commands in a
    # sandbox that doesn't inherit $TMUX, so auto-detection fails even though the
    # agent IS in a tmux pane). Fall back to auto-detection.
    sess = args.session or current_session()
    if not sess:
        sys.stderr.write(
            "couldn't detect your tmux session (your CLI may sandbox shell env).\n"
            "Re-run with --session <your tmux session name>, e.g. --session codex.\n"
            "Find it with: tmux list-sessions\n"
        )
        sys.exit(2)
    # v0.3.0 guard: refuse to claim a tmux session that already belongs to a
    # DIFFERENT fleet agent in the global registry. That collision is how a
    # sandboxed CLI mis-detected its session and hijacked another agent (a
    # marketing GPT registering session 'codex' = the PMXT builder, so marketing
    # messages landed in PMXT). Pass --force to override if you really mean it.
    for gname, gmeta in agents(load_registry()).items():
        if gmeta.get("session") == sess and gname != (args.as_name or sess):
            if not getattr(args, "force", False):
                sys.stderr.write(
                    f"refusing: tmux session '{sess}' already belongs to fleet agent "
                    f"'{gname}'. You are likely in a different session.\n"
                    f"Check `tmux list-sessions`, re-run with the correct --session, "
                    f"or pass --force to override.\n"
                )
                sys.exit(2)
    project = args.project
    roster = load_roster(project)
    members = roster.setdefault("members", {})
    base = args.as_name or sess
    name = unique_member_name(members, base, sess)
    prior = members.get(name, {})
    existed = name in members
    entry = {
        "session": sess,
        "model": args.model or prior.get("model", "unknown"),
        "role": args.role or prior.get("role", ""),
        "joined": prior.get("joined", now_utc()),
        "last_active": now_utc(),
    }
    delay = (
        args.enter_delay if args.enter_delay is not None else prior.get("enter_delay")
    )
    if delay is not None:
        entry["enter_delay"] = delay
    members[name] = entry
    save_roster(roster)
    label = f"{name} ({members[name]['model']}"
    if members[name]["role"]:
        label += f", {members[name]['role']}"
    label += ")"
    # doorbell existing members (not self) that someone joined
    if not existed:
        note = f"{MARKER} {label} joined project '{project}' — roster: agent_comms roster {project}"
        for other, m in members.items():
            if other == name:
                continue
            s2 = m.get("session", "")
            if s2 and session_exists(s2):
                inject(s2, note, enter_delay=m.get("enter_delay"))
    print(
        f"{'updated' if existed else 'registered'}: {name} in project '{project}' (session={sess})"
    )
    print(f"teammates: {', '.join([n for n in members if n != name]) or '(none yet)'}")
    print(f'to message the project:  agent_comms post {project} "<your message>"')


def cmd_roster(args, _reg):
    roster = load_roster(args.project)
    members = roster.get("members", {})
    if not members:
        print(f"project '{args.project}': no members yet")
        return
    print(f"project '{args.project}' — {len(members)} member(s):")
    print(f"  {'MEMBER':<14}{'SESSION':<14}{'MODEL':<30}{'STATUS':<7}ROLE")
    for name, m in members.items():
        up = "up" if session_exists(m.get("session", "")) else "down"
        print(
            f"  {name:<14}{m.get('session',''):<14}{m.get('model','?'):<30}{up:<7}{m.get('role','')}"
        )


def cmd_invite(args, _reg):
    proj = f" --project {args.project}" if args.project else ""
    print("Paste this line into each AI CLI's tmux pane you want in the comms:\n")
    print(f"    read {JOIN_DOC}")
    print(f"\n(The agent will ask for the project name, then self-register{proj}.)")
    if not JOIN_DOC.exists():
        print(
            f"\nNOTE: {JOIN_DOC} not found — run 'agent_comms init-join' to write it."
        )


def resolve_cmd() -> str:
    """The invocation agents should use. Prefer a PATH-resolved `agent_comms`,
    else the absolute path of however THIS process was launched (works even when
    ~/bin isn't on the login-shell PATH)."""
    return shutil.which("agent_comms") or os.path.abspath(sys.argv[0])


def cmd_init_join(args, _reg):
    JOIN_DOC.parent.mkdir(parents=True, exist_ok=True)
    JOIN_DOC.write_text(JOIN_DOC_TEXT.format(cmd=resolve_cmd()))
    print(f"wrote join instructions -> {JOIN_DOC}")
    print(f"agents will be told to invoke: {resolve_cmd()}")


def cmd_discover(args, reg):
    """Propose registry entries from currently-running tmux sessions."""
    try:
        r = subprocess.run(
            [tmux_bin(), "list-sessions", "-F", "#S"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        sessions = [s for s in r.stdout.splitlines() if s.strip()]
    except Exception:
        sessions = []
    if not sessions:
        print("no live tmux sessions found (is tmux running?)")
        return
    a = agents(reg)
    known_sessions = {m.get("session") for m in a.values()}
    new = [s for s in sessions if s not in known_sessions]
    print(f"live tmux sessions: {', '.join(sessions)}")
    if not new:
        print("all already in your registry.")
        return
    if args.add:
        for s in new:
            a[s] = {
                "session": s,
                "color": "white",
                "enabled": True,
                "desc": "",
                "revive_cmd": None,
            }
        save_registry(reg)
        print(f"added {len(new)} agent(s) to registry: {', '.join(new)}")
    else:
        print(f"not in registry: {', '.join(new)}")
        print("re-run with --add to register them: agent_comms discover --add")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_comms", description="Discreet inter-CLI message bus over tmux."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a message to one agent")
    s.add_argument("to")
    s.add_argument("message", nargs="+")
    s.add_argument("--from", dest="frm", default=None)
    s.add_argument("-q", "--quiet", action="store_true")
    s.add_argument(
        "--no-confirm",
        dest="confirm",
        action="store_false",
        help="skip the capture-pane delivery check (faster, fire-and-forget)",
    )
    s.set_defaults(fn=cmd_send, confirm=True)

    b = sub.add_parser("broadcast", help="send to all enabled agents (except self)")
    b.add_argument("message", nargs="+")
    b.add_argument("--from", dest="frm", default=None)
    b.add_argument("-q", "--quiet", action="store_true")
    b.add_argument(
        "--confirm",
        action="store_true",
        help="verify each delivery via capture-pane (slower; one capture per target)",
    )
    b.set_defaults(fn=cmd_broadcast)

    po = sub.add_parser("post", help="append to a thread .md + ring the doorbell")
    po.add_argument("thread", help="thread name (may contain '/' for subdirs)")
    po.add_argument("message", nargs="+")
    po.add_argument(
        "--to", default=None, help="doorbell only this agent (else all but self)"
    )
    po.add_argument("--from", dest="frm", default=None)
    po.add_argument("-q", "--quiet", action="store_true")
    po.add_argument(
        "--confirm",
        action="store_true",
        help="verify each doorbell submitted via capture-pane (one capture per target)",
    )
    po.set_defaults(fn=cmd_post)

    rd = sub.add_parser("read", help="print a thread .md")
    rd.add_argument("thread")
    rd.add_argument("-n", type=int, default=0, help="last N entries (0=all)")
    rd.set_defaults(fn=cmd_read)

    th = sub.add_parser("threads", help="list discussion threads")
    th.set_defaults(fn=cmd_threads)

    # --- self-registration / projects ---
    rg = sub.add_parser(
        "register", help="self-register into a project (run from the agent's tmux pane)"
    )
    rg.add_argument("--project", required=True)
    rg.add_argument(
        "--as",
        dest="as_name",
        default=None,
        help="member label (default: your session name)",
    )
    rg.add_argument(
        "--session",
        default=None,
        help="tmux session name (use if auto-detect fails, e.g. Codex sandbox)",
    )
    rg.add_argument("--model", default=None, help="your model id, e.g. claude-opus-4-7")
    rg.add_argument("--role", default=None, help="one phrase on your strengths")
    rg.add_argument(
        "--enter-delay",
        dest="enter_delay",
        type=float,
        default=None,
        help="seconds between paste and Enter for YOUR pane (raise if your TUI doesn't submit, e.g. 0.8)",
    )
    rg.add_argument(
        "--force",
        action="store_true",
        help="override the guard that refuses a session already owned by another fleet agent",
    )
    rg.set_defaults(fn=cmd_register)

    ro = sub.add_parser("roster", help="show members of a project")
    ro.add_argument("project")
    ro.set_defaults(fn=cmd_roster)

    iv = sub.add_parser("invite", help="print the paste-line to add an agent to comms")
    iv.add_argument("--project", default=None)
    iv.set_defaults(fn=cmd_invite)

    ij = sub.add_parser("init-join", help="write the coms_join.md instruction file")
    ij.set_defaults(fn=cmd_init_join)

    dc = sub.add_parser(
        "discover", help="propose registry entries from live tmux sessions"
    )
    dc.add_argument(
        "--add", action="store_true", help="actually add the discovered sessions"
    )
    dc.set_defaults(fn=cmd_discover)

    for alias in ("list", "ls"):
        l = sub.add_parser(alias, help="list agents + live session status")
        l.set_defaults(fn=cmd_list)

    a = sub.add_parser("add", help="register a new CLI")
    a.add_argument("name")
    a.add_argument("session")
    a.add_argument("--color", default="white")
    a.add_argument("--desc", default="")
    a.add_argument(
        "--revive", default=None, help="shell cmd to launch the session if down"
    )
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="unregister a CLI")
    r.add_argument("name")
    r.set_defaults(fn=cmd_remove)

    en = sub.add_parser("enable")
    en.add_argument("name")
    en.set_defaults(fn=lambda args, reg: cmd_toggle(args, reg, True))
    di = sub.add_parser("disable")
    di.add_argument("name")
    di.set_defaults(fn=lambda args, reg: cmd_toggle(args, reg, False))

    w = sub.add_parser("whoami", help="print detected sender identity")
    w.add_argument("--from", dest="frm", default=None)
    w.set_defaults(fn=cmd_whoami)

    lg = sub.add_parser("log", help="tail the comms audit log")
    lg.add_argument("-n", type=int, default=20)
    lg.set_defaults(fn=cmd_log)

    i = sub.add_parser("init", help="write a starter registry")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)
    return p


def main() -> int:
    args = build_parser().parse_args()
    # These commands operate without an existing registry (fresh-user / project flow).
    NO_REGISTRY = {"init", "register", "roster", "invite", "init-join"}
    if args.cmd in NO_REGISTRY:
        reg = {"agents": {}}
    else:
        reg = load_registry()
    args.fn(args, reg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
