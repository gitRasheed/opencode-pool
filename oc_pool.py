"""Run opencode generations through shared server processes.

Built against opencode 1.18.21; the server API is not a stable contract, so
re-verify endpoints when the major version changes.

CLI: oc_pool.py up [N] | down | status
Library: generate(model, prompt, variant=None, timeout=600) -> text or None

Scaling rule for orchestrators: servers = ceil(peak concurrent LLM calls / 16).
"""

import base64
import json
import os
import random
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import shutil

STATE = Path(os.environ.get("OC_POOL_STATE", Path.home() / ".cache/opencode-pool.json"))
# one shared native-filesystem workdir: opencode keeps ~52MB of never-evicted
# state per distinct directory
WORKDIR = Path(os.environ.get("OC_POOL_WORKDIR", Path.home() / ".local/share/opencode-pool/workdir"))
OPENCODE = os.environ.get("OC_POOL_BIN") or shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode")
BASE_PORT = int(os.environ.get("OC_POOL_BASE_PORT", 4310))

# server-side mirror of `opencode run --auto`; last-match-wins, so the denies
# must follow the allow-all
PERMISSION = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "question", "pattern": "*", "action": "deny"},
    {"permission": "plan_enter", "pattern": "*", "action": "deny"},
    {"permission": "plan_exit", "pattern": "*", "action": "deny"},
]


def _req(pool, port, method, path, body=None, timeout=30):
    url = f"http://127.0.0.1:{port}{path}{'&' if '?' in path else '?'}directory={WORKDIR}"
    r = urllib.request.Request(url, method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json",
                                        "Authorization": "Basic " + base64.b64encode(
                                            f"opencode:{pool['password']}".encode()).decode()})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def _healthy(pool, port):
    try:
        _req(pool, port, "GET", "/global/health", timeout=5)
        return True
    except Exception:
        return False


def _spawn(port, password):
    env = dict(os.environ, OPENCODE_SERVER_PASSWORD=password)
    p = subprocess.Popen([OPENCODE, "serve", "--port", str(port)], cwd=WORKDIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return p.pid


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return None


def up(n):
    down()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    password = secrets.token_hex(16)
    pool = {"password": password, "servers": []}
    for i in range(n):
        port = BASE_PORT + i
        pool["servers"].append({"port": port, "pid": _spawn(port, password)})
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(pool))
    STATE.chmod(0o600)
    deadline = time.time() + 30
    for s in pool["servers"]:
        while not _healthy(pool, s["port"]):
            if time.time() > deadline:
                sys.exit(f"server on :{s['port']} failed health check")
            time.sleep(0.5)
    print(f"pool up: {n} server(s) on ports {[s['port'] for s in pool['servers']]}, workdir {WORKDIR}")


def down():
    pool = _load()
    if not pool:
        return
    for s in pool["servers"]:
        try:
            os.killpg(os.getpgid(s["pid"]), signal.SIGTERM)
        except Exception:
            pass
    STATE.unlink(missing_ok=True)
    print("pool down")


def _revive(pool, srv):
    try:
        os.killpg(os.getpgid(srv["pid"]), signal.SIGTERM)
    except Exception:
        pass
    srv["pid"] = _spawn(srv["port"], pool["password"])
    STATE.write_text(json.dumps(pool))
    for _ in range(60):
        if _healthy(pool, srv["port"]):
            return True
        time.sleep(0.5)
    return False


def generate(model, prompt, variant=None, timeout=600):
    """Return the generated text, or None on failure (one retry on transport errors)."""
    pool = _load()
    if not pool:
        return None
    provider, model_id = model.split("/", 1)
    for attempt in range(2):
        srv = random.choice(pool["servers"])
        if not _healthy(pool, srv["port"]) and not _revive(pool, srv):
            continue
        sid = None
        try:
            ses = _req(pool, srv["port"], "POST", "/session",
                       {"title": f"gen-{os.getpid()}", "agent": "build", "permission": PERMISSION})
            sid = ses["id"]
            body = {"agent": "build",
                    "model": {"providerID": provider, "modelID": model_id},
                    "tools": {"*": False},
                    "parts": [{"type": "text", "text": prompt}]}
            if variant:
                body["variant"] = variant
            msg = _req(pool, srv["port"], "POST", f"/session/{sid}/message", body, timeout=timeout)
            if msg["info"].get("error"):
                return None
            return "".join(p.get("text", "") for p in msg["parts"] if p.get("type") == "text")
        except Exception:
            try:
                if sid:
                    _req(pool, srv["port"], "POST", f"/session/{sid}/abort", {}, timeout=10)
            except Exception:
                pass
        finally:
            try:
                if sid:
                    _req(pool, srv["port"], "DELETE", f"/session/{sid}", timeout=10)
            except Exception:
                pass
    return None


def status():
    pool = _load()
    if not pool:
        print("no pool")
        return
    for s in pool["servers"]:
        print(f":{s['port']} pid {s['pid']} {'healthy' if _healthy(pool, s['port']) else 'DEAD'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "up":
        up(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    elif cmd == "down":
        down()
    else:
        status()
