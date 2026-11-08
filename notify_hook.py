"""Conductor agent hook. Usage: python3 notify_hook.py <agent> <done|needs-input> [json]
Reads the hook's JSON from the last argument, else stdin, and writes one small event file
for CONDUCTOR_RUN. It is shipped to the agent's machine as `python3 -c`, so keep it stdlib and
self-contained. It never fails the agent."""
import json, os, re, sys, time

try:
    agent, kind = sys.argv[1], sys.argv[2]
    run = os.environ["CONDUCTOR_RUN"]
    if kind not in ("done", "needs-input") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run):
        raise ValueError("not a conductor event")
    try:
        data = json.loads(sys.argv[3] if len(sys.argv) > 3 else sys.stdin.read())
    except ValueError:
        data = {}
    keys = ("last_assistant_message", "last-assistant-message", "message")
    text = next((str(data[k]) for k in keys if isinstance(data, dict) and data.get(k)), "")
    folder = os.environ.get("CONDUCTOR_EVENTS") or os.path.expanduser("~/.conductor/events")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, run + "." + kind + ".json")
    with open(path + ".tmp", "w") as out:
        json.dump({"agent": agent, "kind": kind, "time": time.time(), "message": text[-1500:]}, out)
    os.replace(path + ".tmp", path)
except Exception:
    pass
