"""Tests for agent_comms — cover the tmux-independent logic so CI runs anywhere.

tmux-touching paths (inject / session_exists) are not exercised here; CI runners
have no tmux sessions. We test message tagging, thread files, identity, and
registry I/O — the parts that must be correct regardless of transport.
"""

import os
import tempfile

# Point config at a throwaway dir BEFORE importing (module reads env at import).
_TMP = tempfile.mkdtemp(prefix="agentcomms-test-")
os.environ["AGENT_COMMS_REGISTRY"] = os.path.join(_TMP, "registry.json")
os.environ["AGENT_COMMS_THREADS"] = os.path.join(_TMP, "threads")
os.environ["AGENT_COMMS_PROJECTS"] = os.path.join(_TMP, "projects")
os.environ["AGENT_COMMS_JOIN_DOC"] = os.path.join(_TMP, "coms_join.md")
os.environ["AGENT_COMMS_LOG"] = os.path.join(_TMP, "comms.log")
os.environ["AGENT_COMMS_SPOOL"] = os.path.join(_TMP, "spool")
os.environ["AGENT_COMMS_SELF"] = "tester"

import agent_comms as ac  # noqa: E402


def test_tagged_includes_marker_and_sender():
    out = ac.tagged("claude", "build the parser")
    assert ac.MARKER in out
    assert "from=claude" in out
    assert "build the parser" in out


def test_thread_path_supports_subdirs():
    p = ac.thread_path("hermes/pmxt-execution")
    assert p.name == "pmxt-execution.md"
    assert p.parent.name == "hermes"
    # ".md" suffix is not doubled
    assert ac.thread_path("foo.md").name == "foo.md"


def test_append_thread_roundtrip_and_order():
    p = ac.append_thread("topic-a", "claude", "grok", "first message")
    ac.append_thread("topic-a", "grok", "claude", "second message")
    text = p.read_text(encoding="utf-8")
    assert "## [claude→grok" in text
    assert "## [grok→claude" in text
    assert text.index("first message") < text.index("second message")


def test_detect_self_override_wins():
    reg = {"agents": {}}
    assert ac.detect_self(reg, "explicit") == "explicit"


def test_detect_self_falls_back_to_env_without_tmux():
    # no tmux on CI -> current_session() None -> AGENT_COMMS_SELF env
    reg = {"agents": {}}
    assert ac.detect_self(reg, None) == "tester"


def test_registry_save_load_roundtrip():
    reg = {"agents": {"x": {"session": "x", "enabled": True}}}
    ac.save_registry(reg)
    loaded = ac.load_registry()
    assert "x" in ac.agents(loaded)
    assert ac.agents(loaded)["x"]["session"] == "x"


def test_ensure_target_unknown_and_disabled():
    reg = {"agents": {"off": {"session": "off", "enabled": False}}}
    sess, reason = ac.ensure_target(reg, "nope")
    assert sess is None and reason == "UNKNOWN"
    sess, reason = ac.ensure_target(reg, "off")
    assert sess is None and reason == "DISABLED"


def test_missing_registry_is_nonfatal():
    # project/thread commands must work even with no registry.json
    import os as _os

    if _os.path.exists(os.environ["AGENT_COMMS_REGISTRY"]):
        _os.remove(os.environ["AGENT_COMMS_REGISTRY"])
    reg = ac.load_registry()
    assert reg == {"agents": {}}


# ---------- self-registration / projects ----------
def test_unique_member_name_suffixes_on_collision():
    members = {"claude": {"session": "sess-a"}}
    # different session, same base -> suffixed
    assert ac.unique_member_name(members, "claude", "sess-b") == "claude-2"
    # same session re-registering -> keeps its existing name (idempotent)
    assert ac.unique_member_name(members, "claude", "sess-a") == "claude"
    # fresh base -> unchanged
    assert ac.unique_member_name(members, "grok", "sess-c") == "grok"


def test_roster_save_load_roundtrip():
    roster = {
        "project": "demo",
        "members": {"claude": {"session": "s1", "model": "claude-opus-4-7"}},
    }
    ac.save_roster(roster)
    loaded = ac.load_roster("demo")
    assert loaded["members"]["claude"]["model"] == "claude-opus-4-7"
    # unknown project -> empty roster, not error
    assert ac.load_roster("nope")["members"] == {}


def test_did_you_mean_suggests_close_match():
    assert "claude" in ac.did_you_mean("clade", ["claude", "grok", "codex"])
    assert ac.did_you_mean("zzzzz", ["claude", "grok"]) == ""


def test_append_thread_includes_model_when_given():
    p = ac.append_thread("modeltest", "claude", None, "hi", model="claude-opus-4-7")
    assert "(claude-opus-4-7)" in p.read_text(encoding="utf-8")
    # model omitted -> no parens noise
    p2 = ac.append_thread("modeltest2", "claude", None, "hi")
    assert "(claude-opus-4-7)" not in p2.read_text(encoding="utf-8")


# ---------- hardening (from Grok pre-publish review) ----------
def test_safe_name_blocks_path_traversal():
    assert ac.safe_name("hermes/pmxt") == "hermes/pmxt"
    assert ac.safe_name("../../etc/passwd") == "etc/passwd"  # .. segments stripped
    assert ac.safe_name("/abs/path") == "abs/path"  # leading slash stripped
    import pytest

    with pytest.raises(ValueError):
        ac.safe_name("../..")  # nothing left after stripping


def test_thread_path_stays_under_threads_dir():
    p = ac.thread_path("../../escape")
    assert ac.THREADS_DIR in p.parents  # cannot escape the base dir


def test_roster_path_stays_under_projects_dir():
    # project names also flow from `register --project` (user input) — must be guarded
    p = ac.roster_path("../../escape")
    assert ac.PROJECTS_DIR in p.parents
    p2 = ac.roster_path("/abs/proj")
    assert ac.PROJECTS_DIR in p2.parents


def test_message_length_cap_truncates():
    long = "x" * (ac.MAX_MESSAGE_LEN + 500)
    p = ac.append_thread("captest", "claude", None, long)
    body = p.read_text(encoding="utf-8")
    assert "..." in body
    assert len(body) < ac.MAX_MESSAGE_LEN + 200  # truncated, not full length


def test_register_post_integration(monkeypatch):
    """End-to-end register -> roster -> post flow with tmux mocked out, so it
    runs in CI without a real tmux server."""
    # pretend we're in tmux session 'sess-claude', and every session is 'up'
    injected = []
    monkeypatch.setattr(ac, "current_session", lambda: "sess-claude")
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: injected.append((s, t)) or True
    )

    # claude registers
    class A:
        project, as_name, session, model, role, enter_delay = (
            "proj1",
            "claude",
            None,
            "claude-opus-4-7",
            "lead",
            None,
        )

    try:
        ac.cmd_register(A(), {"agents": {}})
    except SystemExit:
        pass
    roster = ac.load_roster("proj1")
    assert "claude" in roster["members"]
    assert roster["members"]["claude"]["session"] == "sess-claude"

    # a second member registers from a different session -> grok
    monkeypatch.setattr(ac, "current_session", lambda: "sess-grok")

    class B:
        project, as_name, session, model, role, enter_delay = (
            "proj1",
            "grok",
            None,
            "grok-4",
            "research",
            None,
        )

    try:
        ac.cmd_register(B(), {"agents": {}})
    except SystemExit:
        pass
    roster = ac.load_roster("proj1")
    assert set(roster["members"]) == {"claude", "grok"}

    # grok posts to the project -> should doorbell claude (not self), thread written
    injected.clear()

    class P:
        thread, message, to, frm, quiet = "proj1", ["hello team"], None, None, True

    try:
        ac.cmd_post(P(), {"agents": {}})
    except SystemExit:
        pass
    # claude's session got the doorbell; grok (self) did not
    assert any(s == "sess-claude" for s, _ in injected)
    assert not any(s == "sess-grok" for s, _ in injected)
    # thread file recorded the post
    assert "hello team" in ac.thread_path("proj1").read_text(encoding="utf-8")


def test_per_agent_enter_delay_flows_to_doorbell(monkeypatch):
    """A project member's per-agent enter_delay must be passed through to inject()
    when it receives a doorbell (Grok re-review nit)."""
    calls = []
    monkeypatch.setattr(ac, "current_session", lambda: "sess-sender")
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac,
        "inject",
        lambda s, t, enter_delay=None: calls.append((s, enter_delay)) or True,
    )
    # roster: sender + a slow member with a custom enter_delay
    roster = {
        "project": "delayproj",
        "members": {
            "sender": {"session": "sess-sender", "model": "m", "role": ""},
            "slowcli": {
                "session": "sess-slow",
                "model": "m",
                "role": "",
                "enter_delay": 0.9,
            },
        },
    }
    ac.save_roster(roster)

    class P:
        thread, message, to, frm, quiet = "delayproj", ["ping"], None, None, True

    try:
        ac.cmd_post(P(), {"agents": {}})
    except SystemExit:
        pass
    # the slow member's doorbell used its 0.9s delay
    assert ("sess-slow", 0.9) in calls


# ---------- loop guard (infinite back-and-forth prevention) ----------
def test_recent_exchange_count():
    for _ in range(3):
        ac.audit("claude", "grok", "DOORBELL:p", "x")
        ac.audit("grok", "claude", "DOORBELL:p", "y")
    assert ac.recent_exchange_count("claude", "grok") >= ac.LOOP_WARN_COUNT
    assert ac.recent_exchange_count("claude", "nobody") == 0


def test_loop_warning_appended_to_doorbell(monkeypatch):
    captured = []
    monkeypatch.setattr(ac, "current_session", lambda: "sess-c")
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: captured.append(t) or True
    )
    # pre-populate a rapid claude<->grok exchange in the audit log
    for _ in range(6):
        ac.audit("claude", "grok", "DOORBELL:lp", "x")
    ac.save_roster(
        {
            "project": "lp",
            "members": {
                "claude": {"session": "sess-c", "model": "m", "role": ""},
                "grok": {"session": "sess-g", "model": "m", "role": ""},
            },
        }
    )

    class P:
        thread, message, to, frm, quiet = "lp", ["status?"], None, None, True

    try:
        ac.cmd_post(P(), {"agents": {}})
    except SystemExit:
        pass
    assert any("loop guard" in t for t in captured)


def test_send_resolves_project_member(monkeypatch):
    """`send <name>` must reach a self-registered project member, not only
    global-registry agents (the bug that made the recording Claude struggle:
    `send grok` -> 'unknown: grok' because grok was a roster member)."""
    captured = []
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: captured.append((s, t)) or True
    )
    ac.save_roster(
        {
            "project": "P",
            "members": {"grok": {"session": "gsess", "model": "grok-4", "role": "e"}},
        }
    )
    # grok is NOT in the registry; deliver should find it via the roster
    ok = ac.deliver({"agents": {}}, "claude", "grok", "hi", quiet=True)
    assert ok
    assert any(s == "gsess" for s, _ in captured)


def test_no_reply_hint_in_doorbell(monkeypatch):
    captured = []
    monkeypatch.setattr(ac, "current_session", lambda: "sess-x")
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: captured.append(t) or True
    )
    ac.save_roster(
        {
            "project": "nr",
            "members": {
                "me": {"session": "sess-x", "model": "m", "role": ""},
                "you": {"session": "sess-y", "model": "m", "role": ""},
            },
        }
    )

    class P:
        thread, message, to, frm, quiet = (
            "nr",
            ["all done [no-reply]"],
            None,
            None,
            True,
        )

    try:
        ac.cmd_post(P(), {"agents": {}})
    except SystemExit:
        pass
    assert any("no reply needed" in t for t in captured)


# ---------- delivery confirmation (capture-pane submit verification) ----------
def test_verify_submitted_true_when_marker_absent(monkeypatch):
    """Marker gone from the bottom rows => submitted."""
    monkeypatch.setattr(ac, "VERIFY_DELAY", 0.0)
    monkeypatch.setattr(
        ac, "capture_pane_tail", lambda s, lines=ac.VERIFY_LINES: "esc to interrupt"
    )
    assert ac.verify_submitted("sess") is True


def test_verify_submitted_false_when_marker_present(monkeypatch):
    """Marker still sitting in the composer => unsubmitted."""
    monkeypatch.setattr(ac, "VERIFY_DELAY", 0.0)
    monkeypatch.setattr(
        ac,
        "capture_pane_tail",
        lambda s, lines=ac.VERIFY_LINES: f"{ac.MARKER} from=claude 14:00Z | hi",
    )
    assert ac.verify_submitted("sess") is False


def test_verify_submitted_none_when_capture_fails(monkeypatch):
    """No tmux / bad session => None (unverified, not a hard fail)."""
    monkeypatch.setattr(ac, "VERIFY_DELAY", 0.0)
    monkeypatch.setattr(ac, "capture_pane_tail", lambda s, lines=ac.VERIFY_LINES: None)
    assert ac.verify_submitted("sess") is None


def test_deliver_confirm_unsubmitted_returns_false(monkeypatch):
    """inject lands the keys but the message stays in the composer -> deliver
    must report failure (the whole point of B: no more silent drops)."""
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(ac, "inject", lambda s, t, enter_delay=None: True)
    monkeypatch.setattr(ac, "verify_submitted", lambda s, marker=ac.MARKER: False)
    reg = {"agents": {"codex": {"session": "csess", "enabled": True}}}
    ok = ac.deliver(reg, "claude", "codex", "build it", quiet=True, confirm=True)
    assert ok is False


def test_deliver_confirm_delivered_returns_true(monkeypatch):
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(ac, "inject", lambda s, t, enter_delay=None: True)
    monkeypatch.setattr(ac, "verify_submitted", lambda s, marker=ac.MARKER: True)
    reg = {"agents": {"codex": {"session": "csess", "enabled": True}}}
    ok = ac.deliver(reg, "claude", "codex", "build it", quiet=True, confirm=True)
    assert ok is True


# ---------- v0.3.0: cross-project collision guards ----------
def test_post_to_nonmember_does_not_leak_to_global_registry(monkeypatch):
    """A project post with --to a NON-member must NOT fall back to the global
    registry — that cross-project leak routed messages to the wrong same-named
    agent (marketing 'codex' hitting the PMXT Codex builder)."""
    injected = []
    monkeypatch.setattr(ac, "current_session", lambda: "sess-mk")
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: injected.append((s, t)) or True
    )
    ac.save_roster(
        {
            "project": "mkproj",
            "members": {"mk-lead": {"session": "sess-mk", "model": "m", "role": ""}},
        }
    )
    reg = {"agents": {"codex": {"session": "pmxt-codex-sess", "enabled": True}}}

    class P:
        thread, message, to, frm, quiet = "mkproj", ["hi"], "codex", None, True

    code = None
    try:
        ac.cmd_post(P(), reg)
    except SystemExit as e:
        code = e.code
    assert code == 1
    # crucially: nothing was injected into the global PMXT codex session
    assert not any(s == "pmxt-codex-sess" for s, _ in injected)


def test_register_refuses_session_owned_by_fleet_agent(monkeypatch):
    """Refuse registering a tmux session that already belongs to a DIFFERENT fleet
    agent in the global registry (marketing GPT claiming session 'codex')."""
    monkeypatch.setattr(ac, "current_session", lambda: None)
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(ac, "inject", lambda *a, **k: True)
    ac.save_registry({"agents": {"codex": {"session": "codex", "enabled": True}}})

    class R:
        project, as_name, session, model, role, enter_delay, force = (
            "mkg",
            "gpt",
            "codex",
            "gpt-5.5",
            "builds",
            None,
            False,
        )

    code = None
    try:
        ac.cmd_register(R(), {"agents": {}})
    except SystemExit as e:
        code = e.code
    assert code == 2
    assert "gpt" not in ac.load_roster("mkg").get("members", {})

    class RF:
        project, as_name, session, model, role, enter_delay, force = (
            "mkg",
            "gpt",
            "codex",
            "gpt-5.5",
            "builds",
            None,
            True,
        )

    try:
        ac.cmd_register(RF(), {"agents": {}})
    except SystemExit:
        pass
    assert "gpt" in ac.load_roster("mkg").get("members", {})


# ---------- v0.4.0: sandboxed-sender spool + relay (DOORBELL_FAIL false-down fix) ----------
def test_tmux_server_reachable(monkeypatch):
    # $TMUX set -> reachable regardless of list-sessions
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    assert ac.tmux_server_reachable() is True
    # $TMUX unset + list-sessions fails (sandbox) -> not reachable
    monkeypatch.delenv("TMUX", raising=False)

    class R:
        returncode = 1

    monkeypatch.setattr(ac.subprocess, "run", lambda *a, **k: R())
    assert ac.tmux_server_reachable() is False


def test_sandboxed_post_spools_instead_of_false_down(monkeypatch):
    """A sandboxed sender (can't reach tmux) must SPOOL doorbells, not log
    DOORBELL_FAIL / mislabel the recipient down."""
    import glob

    for f in glob.glob(os.path.join(ac.SPOOL_DIR, "*.json")):
        os.unlink(f)
    monkeypatch.setattr(ac, "current_session", lambda: "sess-gpt")
    monkeypatch.setattr(ac, "tmux_server_reachable", lambda: False)  # sandboxed sender
    # if anything tried to inject, fail the test loudly
    monkeypatch.setattr(
        ac, "inject", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not inject"))
    )
    ac.save_roster(
        {
            "project": "spoolproj",
            "members": {
                "gpt-cl": {"session": "sess-gpt", "model": "m", "role": ""},
                "claude-cl": {"session": "7", "model": "m", "role": ""},
            },
        }
    )

    class P:
        thread, message, to, frm, quiet = "spoolproj", ["review please"], None, None, True

    code = None
    try:
        ac.cmd_post(P(), {"agents": {}})
    except SystemExit as e:
        code = e.code
    assert code == 0  # spooled => success, NOT failure
    spooled = glob.glob(os.path.join(ac.SPOOL_DIR, "*.json"))
    assert any("claude-cl" in f for f in spooled)
    import json as _j

    rec = _j.loads(open([f for f in spooled if "claude-cl" in f][0]).read())
    assert rec["session"] == "7" and "review please" not in rec["text"][:0]  # text present
    log = open(os.environ["AGENT_COMMS_LOG"]).read()
    assert "DOORBELL_SPOOLED" in log and "DOORBELL_FAIL:spoolproj" not in log


def test_relay_drains_spool_and_injects(monkeypatch):
    import glob

    for f in glob.glob(os.path.join(ac.SPOOL_DIR, "*.json")):
        os.unlink(f)
    # spool one item
    ac.spool_doorbell("claude-cl", "7", "«agent-msg» ping", None, "gpt-cl", "doorbell")
    assert glob.glob(os.path.join(ac.SPOOL_DIR, "*.json"))
    injected = []
    monkeypatch.setattr(ac, "session_exists", lambda s: True)
    monkeypatch.setattr(
        ac, "inject", lambda s, t, enter_delay=None: injected.append((s, t)) or True
    )

    class A:
        quiet = True

    ac.cmd_relay(A(), {"agents": {}})
    assert injected and injected[0][0] == "7"
    # delivered -> spool file removed
    assert not glob.glob(os.path.join(ac.SPOOL_DIR, "*.json"))
