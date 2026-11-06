"""Captain plans stay on verified commands and never add a bypass flag."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import importlib
import types

pkg = types.ModuleType("hermes_captain")
pkg.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
pkg.__package__ = "hermes_captain"
sys.modules["hermes_captain"] = pkg
importlib.import_module("hermes_captain.plan")
importlib.import_module("hermes_captain.runner")

from hermes_captain.plan import PlanError, plan_run, plan_steer  # noqa: E402
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
        "--prompt", "write the design doc", "--json", "--host", "local",
    ]
    assert plan.mode == "capture"


def test_t3_handover_requires_approval():
    plan = plan_run("t3", "review the diff")
    assert "--permission" in plan.argv
    assert plan.argv[plan.argv.index("--permission") + 1] == "approval-required"
    assert "full-access" not in plan.argv
    assert plan.mode == "capture"


def test_remote_host_is_an_argument_not_a_shell_string():
    plan = plan_run("claude", "fix it", host="alex@100.72.158.127", cwd="/Users/alex/app")
    assert plan.argv[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert "alex@100.72.158.127" in plan.argv
    assert plan.argv[plan.argv.index("--") + 1] == "bash"
    assert "/Users/alex/app" in plan.argv
    assert "fix it" in plan.argv
    joined = _flags(plan.argv)
    assert "cd /Users" not in joined


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


def test_claude_steer_is_refused():
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
