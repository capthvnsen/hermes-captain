"""Captain plans stay on verified commands and never add a bypass flag."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import importlib
import importlib.util
import types

pkg = types.ModuleType("hermes_captain")
pkg.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
pkg.__package__ = "hermes_captain"
sys.modules["hermes_captain"] = pkg
importlib.import_module("hermes_captain.plan")
importlib.import_module("hermes_captain.runner")

from hermes_captain.plan import PlanError, plan_run, plan_steer, plan_wait  # noqa: E402
from hermes_captain.runner import Captain  # noqa: E402

BANNED = (
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "full-access",
)


def _flags(argv: list[str]) -> str:
    return " ".join(argv)


def test_claude_is_print_mode_without_bypass():
    plan = plan_run("claude", "fix the test")
    assert plan.argv[:2] == ["claude", "-p"]
    assert plan.mode == "background"
    assert not any(flag in _flags(plan.argv) for flag in BANNED)


def test_codex_exec_has_no_bypass():
    plan = plan_run("codex", "fix the test")
    assert plan.argv[:2] == ["codex", "exec"]
    assert not any(flag in _flags(plan.argv) for flag in BANNED)


def test_orca_launch_matches_the_documented_command():
    plan = plan_run("orca", "write the design doc", worker="codex", orca_host="local")
    assert plan.argv == [
        "orca", "agent", "launch", "--agent", "codex",
        "--prompt", "write the design doc", "--json", "--environment", "local",
    ]
    assert plan.mode == "capture"


def test_t3_handover_requires_approval():
    plan = plan_run("t3", "review the diff")
    assert "--permission" in plan.argv
    assert plan.argv[plan.argv.index("--permission") + 1] == "approval-required"
    assert "--provider" not in plan.argv
    claude = plan_run("t3", "review the diff", provider="claudeAgent")
    assert claude.argv[claude.argv.index("--provider") + 1] == "claudeAgent"
    assert "full-access" not in plan.argv
    assert plan.mode == "capture"


def test_remote_host_is_one_quoted_command():
    import shlex
    plan = plan_run("claude", "fix it", host="alex@100.72.158.127", cwd="/Users/alex/app")
    assert plan.argv[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert plan.argv[5] == "alex@100.72.158.127"
    remote = shlex.split(plan.argv[6])
    assert remote[:2] == ["bash", "-c"]
    assert "do" not in remote[2].split() or "for " not in remote[2]
    assert "/Users/alex/app" in remote
    assert "fix it" in remote
    assert "cd /Users" not in plan.argv[4]


def test_bad_host_is_refused():
    try:
        plan_run("claude", "fix it", host="alex@mac;rm -rf /")
    except PlanError as exc:
        assert "host" in str(exc)
    else:
        raise AssertionError("bad host was accepted")


def test_t3_steer_uses_if_busy_steer():
    plan = plan_steer("t3", "thread_123", "try the other file")
    assert plan.argv[plan.argv.index("--if-busy") + 1] == "steer"
    assert plan.argv[plan.argv.index("--thread") + 1] == "thread_123"


def test_terminal_harnesses_do_not_skip_approval():
    kimi = plan_run("kimi", "fix the test")
    assert kimi.argv[:2] == ["kimi", "-p"]
    assert "--yolo" not in kimi.argv and "--auto" not in kimi.argv
    code = plan_run("commandcode", "fix the test")
    assert code.argv[:2] == ["commandcode", "-p"]
    assert "--trust" not in code.argv
    pi = plan_run("pi", "fix the test")
    assert pi.argv[:2] == ["pi", "--print"]
    orca = plan_run("orca", "ping", worktree="path:/Users/alex/Projects/app")
    assert orca.argv[orca.argv.index("--worktree") + 1] == "path:/Users/alex/Projects/app"
    assert "--host" not in orca.argv
    generic = plan_run("worker", "fix the test", prompt_flag="-p")
    assert generic.argv == ["worker", "-p", "fix the test"]
    try:
        plan_run("worker", "fix the test", prompt_flag="--yolo")
    except PlanError:
        return
    raise AssertionError("unknown flag was accepted")
    try:
        plan_steer("claude", "abc", "more")
    except PlanError:
        return
    raise AssertionError("claude steer should be refused")


def test_run_does_not_spawn_when_the_launcher_returns():
    import tempfile
    seen = {}

    def capture(argv, cwd, timeout):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return {"code": 0, "stdout": json.dumps({"threadId": "th_1"}), "error": ""}

    with tempfile.TemporaryDirectory() as tmp:
        captain = Captain(
            Path(tmp),
            spawn=lambda *a: (_ for _ in ()).throw(AssertionError("spawned")),
            capture=capture,
        )
        record = captain.run("t3", "review the diff")
    assert record["handle"] == "th_1"
    assert record["status"] == "launched"
    assert seen["argv"][0] == "t3code"
    assert "full-access" not in seen["argv"]


def test_second_peek_stays_quiet_and_hides_the_log():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "run.log"
        log.write_text("still compiling the widget factory\n", encoding="utf-8")
        record = {
            "id": "abc",
            "agent": "claude",
            "status": "running",
            "pid": 1,
            "log": str(log),
            "output": "",
        }
        (root / "abc.json").write_text(json.dumps(record), encoding="utf-8")
        captain = Captain(root, spawn=lambda *a: 1, capture=lambda *a: {"code": 0, "stdout": ""})
        # pid 1 is alive on this machine (launch). Force the status the card cares about.
        first = captain.peek("abc")
        second = captain.peek("abc")
    assert first["speak"] is True
    assert "widget factory" not in json.dumps(first)
    assert second["speak"] is False
    assert second["note"] == "No change."
    assert "output" not in second


def test_changed_peek_shows_diff_and_stuck_not_the_log():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "run.log"
        log.write_text("Do you want to proceed?\nsecret log line\n", encoding="utf-8")
        record = {
            "id": "abc",
            "agent": "claude",
            "status": "running",
            "pid": 1,
            "log": str(log),
            "cwd": "/tmp/does-not-matter",
        }
        (root / "abc.json").write_text(json.dumps(record), encoding="utf-8")
        captain = Captain(
            root,
            spawn=lambda *a: 1,
            capture=lambda *a: {"code": 0, "stdout": ""},
            diff=lambda cwd: "widget.py | 2 +-",
        )
        card = captain.peek("abc")
    assert card["speak"] is True
    assert card["stuck_on_approval"] is True
    assert card["note"] == "Stuck on approval."
    assert card["diff"] == "widget.py | 2 +-"
    assert "secret log line" not in json.dumps(card)


def test_git_stat_is_a_real_diff_not_a_shell():
    import subprocess
    import tempfile
    from hermes_captain.peek import git_stat
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "captain@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Captain"], cwd=root, check=True)
        (root / "widget.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "add", "widget.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
        (root / "widget.py").write_text("print(1)\nprint(2)\n", encoding="utf-8")
        stat = git_stat(str(root))
    assert "widget.py" in stat
    assert "??" in stat or "M" in stat


def test_handle_comes_from_the_nested_thread():
    from hermes_captain.runner import _guess_handle
    payload = {
        "ok": True,
        "data": {
            "project": {"id": "project-1", "title": "hermes-captain"},
            "thread": {"id": "68cd76c8-61b2-49c9-a608-d0f716d6c63d", "title": "review"},
        },
    }
    assert _guess_handle(json.dumps(payload)) == "68cd76c8-61b2-49c9-a608-d0f716d6c63d"
    from hermes_captain.peek import hold_reply
    assert hold_reply({"speak": False}, "Still running, nothing new.") == "No change."
    assert hold_reply({"speak": True}, "Still running, nothing new.") is None
    assert hold_reply({"speak": False}, "The widget test failed on line 12 because the fixture was empty.") is None


def test_run_id_cannot_leave_the_data_folder():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        captain = Captain(root, spawn=lambda *a: 1, capture=lambda *a: {"code": 0, "stdout": ""})
        try:
            captain.peek("../victim")
        except PlanError as exc:
            assert "run id" in str(exc)
        else:
            raise AssertionError("path traversal was accepted")
        assert not (root.parent / "victim.json").exists()


def test_slash_peek_does_not_call_a_model():
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "hermes_captain_entry",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    entry = importlib.util.module_from_spec(spec)
    entry.__package__ = "hermes_captain"
    sys.modules["hermes_captain_entry"] = entry
    spec.loader.exec_module(entry)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "run.log"
        log.write_text("compiling\n", encoding="utf-8")
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "agent": "claude", "status": "running", "pid": 1, "log": str(log),
        }), encoding="utf-8")
        entry._CAPTAIN = Captain(root, spawn=lambda *a: 1, capture=lambda *a: {"code": 0, "stdout": ""})
        first = entry.handle_command("peek abc")
        second = entry.handle_command("peek abc")
    assert "compiling" not in first
    assert second == "No change."


def test_question_is_not_rewritten_as_no_change():
    from hermes_captain.peek import hold_reply
    reply = "We are waiting on your API key. Which account should I use?"
    assert hold_reply({"speak": False}, reply) is None


def test_t3_pending_approval_shows_on_a_launched_run():
    import tempfile
    inspect = json.dumps({"data": {"thread": {"id": "th_1", "pendingRequests": [{"id": "req"}]}}})

    def capture(argv, cwd, timeout):
        if "inspect" in argv:
            return {"code": 0, "stdout": inspect, "error": ""}
        return {"code": 0, "stdout": "", "error": ""}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "abc.json").write_text(json.dumps({
            "id": "abc",
            "agent": "t3",
            "surface": "t3",
            "status": "launched",
            "handle": "th_1",
        }), encoding="utf-8")
        card = Captain(root, spawn=lambda *a: 1, capture=capture).peek("abc")
    assert card["stuck_on_approval"] is True
    assert card["note"] == "Stuck on approval."


def test_remote_peek_does_not_diff_this_machine():
    import tempfile
    seen = {}

    def capture(argv, cwd, timeout):
        seen["argv"] = argv
        return {"code": 0, "stdout": "?? REVIEW.md\n", "error": ""}

    def local_diff(cwd):
        raise AssertionError("diffed the local machine")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "run.log"
        log.write_text("ok\n", encoding="utf-8")
        (root / "abc.json").write_text(json.dumps({
            "id": "abc",
            "agent": "claude",
            "status": "running",
            "pid": 1,
            "log": str(log),
            "cwd": "/Users/alex/src/hermes-captain",
            "host": "alex@100.72.158.127",
        }), encoding="utf-8")
        card = Captain(root, spawn=lambda *a: 1, capture=capture, diff=local_diff).peek("abc")
    assert card["diff"] == "?? REVIEW.md"
    assert seen["argv"][0] == "ssh"
    assert "alex@100.72.158.127" in seen["argv"]


def test_stderr_does_not_break_the_thread_id():
    from hermes_captain.runner import _capture, _guess_handle
    result = _capture(
        ["python3", "-c", "import sys; print('{\\\"ok\\\": true, \\\"data\\\": {\\\"thread\\\": {\\\"id\\\": \\\"th_9\\\", \\\"title\\\": \\\"review\\\"}}}'); print('warning', file=sys.stderr)"],
        None,
        10,
    )
    assert "warning" not in result["stdout"]
    assert _guess_handle(result["stdout"]) == "th_9"


def test_wait_pings_in_seconds_not_fifteen_minutes():
    import tempfile
    import time
    from hermes_captain.runner import _spawn
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        captain = Captain(root)
        log = root / "sleep.log"
        pid = _spawn(["sleep", "2"], None, log)
        proc = getattr(_spawn, "_last", None)
        assert proc is not None
        captain._procs[pid] = proc
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "pid": pid, "surface": "cli", "status": "running",
        }), encoding="utf-8")
        armed = captain.arm_wait("abc")
        done = captain.wait_done("abc", timeout=8)
    assert armed["model"] is False
    assert done["model"] is False
    assert done["ping"] == "done"
    assert done["seconds"] is not None and done["seconds"] < 6
    assert done["seconds"] < 15 * 60
    del time


def test_remote_cli_stop_does_not_pretend():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "agent": "claude", "pid": 1, "host": "alex@100.72.158.127", "status": "running",
        }), encoding="utf-8")
        result = Captain(root, spawn=lambda *a: 1, capture=lambda *a: {"code": 0, "stdout": ""}).stop("abc")
    assert result["ok"] is False
    assert "not wired" in result["output"]


def test_named_cli_agents_do_not_skip_approval():
    hermes = plan_run("hermes", "fix the test")
    assert hermes.argv == ["hermes", "chat", "-q", "fix the test", "-Q"]
    assert "--yolo" not in hermes.argv
    claude = plan_run("claude-code", "fix the test", continue_session=True)
    assert claude.argv[:3] == ["claude", "--continue", "-p"]
    codex = plan_run("codecs", "fix the test")
    assert codex.argv[:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex.argv
    grok = plan_run("grock", "fix the test")
    assert grok.argv == ["grok", "--no-auto-update", "-p", "fix the test"]
    assert "--always-approve" not in grok.argv
    opened = plan_run("open-code", "fix the test")
    assert opened.argv == ["opencode", "run", "fix the test"]
    assert "-p" not in opened.argv
    py = plan_run("pycode", "fix the test")
    assert py.argv == ["pycode", "-p", "fix the test"]
    assert "dangerous" not in " ".join(py.argv)
    try:
        plan_run("hermes", "fix the test", continue_session=True)
    except PlanError:
        pass
    else:
        raise AssertionError("hermes continue was accepted")
    picked = plan_run("claude", "fix the test", model="sonnet")
    assert picked.argv[:4] == ["claude", "--model", "sonnet", "-p"]
    cheap = plan_run("hermes", "fix the test", model="haiku")
    assert cheap.argv[1:4] == ["chat", "-m", "haiku"]
    assert "--yolo" not in cheap.argv
    try:
        plan_run("codex", "fix the test", model="gpt")
    except PlanError:
        return
    raise AssertionError("codex model flag was accepted")


def test_finished_keep_pings_once():
    import tempfile
    from hermes_captain.runner import _spawn
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        captain = Captain(root)
        captain.notify = lambda run_id, record: notes.append((run_id, record.get("keep")))
        log = root / "done.log"
        pid = _spawn(["python3", "-c", ""], None, log)
        proc = getattr(_spawn, "_last", None)
        captain._procs[pid] = proc
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "pid": pid, "surface": "cli", "status": "running", "keep": True,
        }), encoding="utf-8")
        captain.arm_wait("abc")
        done = captain.wait_done("abc", timeout=8)
    assert done["ping"] == "done"
    assert notes == [("abc", True)]


def test_finish_pings_without_being_asked():
    import tempfile
    from hermes_captain.runner import _spawn
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        captain = Captain(root)
        captain.notify = lambda run_id, record: notes.append(record.get("ping"))
        log = root / "done.log"
        pid = _spawn(["python3", "-c", ""], None, log)
        captain._procs[pid] = getattr(_spawn, "_last", None)
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "pid": pid, "surface": "cli", "status": "running", "keep": False, "title": "beta",
        }), encoding="utf-8")
        captain.arm_wait("abc")
        done = captain.wait_done("abc", timeout=8)
    assert done["ping"] == "done"
    assert notes == ["done"]


def test_real_t3_thread_ids_are_accepted():
    handle = "thread:delegated-task:command%3Amcp%3A58968731-a581-4993-8fff-3cb51a5a132e%3Adelegate-task%3A2490cc31"
    plan = plan_wait(handle)
    assert plan.argv[plan.argv.index("--thread") + 1] == handle


def test_t3_timeout_on_stderr_keeps_waiting():
    import tempfile
    calls = {"n": 0}

    def capture(argv, cwd, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "code": 6,
                "stdout": "",
                "stderr": json.dumps({"ok": False, "error": {"code": "THREAD_WAIT_TIMEOUT"}}),
            }
        return {
            "code": 0,
            "stdout": json.dumps({"data": {"wait": {"outcome": "completed"}}}),
            "stderr": "",
        }

    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        captain = Captain(root, capture=capture)
        captain.notify = lambda run_id, record: notes.append(record.get("ping"))
        (root / "abc.json").write_text(json.dumps({
            "id": "abc", "surface": "t3", "handle": "mcp:79982033-5d61-4be6-9794-8f203010edca",
            "status": "running", "keep": False, "title": "Lane 4",
        }), encoding="utf-8")
        captain.arm_wait("abc")
        done = captain.wait_done("abc", timeout=8)
    assert calls["n"] == 2
    assert done["ping"] == "done"
    assert notes == ["done"]


if __name__ == "__main__":
    import traceback
    failed = 0
    tests = [value for name, value in list(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        try:
            test()
            print("ok", test.__name__)
        except Exception:
            failed += 1
            print("FAIL", test.__name__)
            traceback.print_exc()
    if failed:
        raise SystemExit(f"{failed} failed")
    print(f"{len(tests)} passed")
