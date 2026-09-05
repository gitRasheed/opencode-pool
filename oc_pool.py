"""Run opencode generations through shared server processes.

Built against opencode 1.18.21; the server API is not a stable contract, so
re-verify endpoints when the major version changes.

CLI: oc_pool.py up [N] | down | status
Library: generate(model, prompt, variant=None, timeout=600, meta=None,
tools=False) -> text or None; meta receives usage and latest-call diagnostics.

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
import threading
import time
import urllib.request
import urllib.error
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
    # subagents keep only external_directory rules and denies from the parent
    # ruleset (matched by name, not wildcard), so the blanket allow above is
    # dropped for them; this explicit rule survives the filter
    {"permission": "external_directory", "pattern": "*", "action": "allow"},
    {"permission": "question", "pattern": "*", "action": "deny"},
    {"permission": "plan_enter", "pattern": "*", "action": "deny"},
    {"permission": "plan_exit", "pattern": "*", "action": "deny"},
]

# process-wide backstop: merges last into every agent's policy, covering the
# paths a session ruleset never reaches (doom_loop asks, workflow approvals);
# key order matters, rules match last-to-first
ENV_PERMISSION = json.dumps({"*": "allow", "external_directory": "allow",
                             "question": "deny", "plan_enter": "deny",
                             "plan_exit": "deny", "doom_loop": "deny"})


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
    env = dict(os.environ, OPENCODE_SERVER_PASSWORD=password,
               OPENCODE_PERMISSION=ENV_PERMISSION)
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


def _acc_usage(meta, info):
    """Accumulate tokens/cost into meta. Adds, never overwrites: usage from a
    billed-but-failed attempt, an internal retry, or one step of a multi-step
    response would otherwise vanish from the caller's accounting."""
    if not isinstance(meta, dict) or not isinstance(info, dict):
        return
    acc = meta.setdefault("tokens", {})
    for k, v in (info.get("tokens") or {}).items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
        elif isinstance(v, dict):
            sub = acc.setdefault(k, {})
            for k2, v2 in v.items():
                if isinstance(v2, (int, float)):
                    sub[k2] = sub.get(k2, 0) + v2
    if isinstance(info.get("cost"), (int, float)):
        meta["cost"] = meta.get("cost", 0) + info["cost"]


def generate(model, prompt, variant=None, timeout=600, meta=None, tools=False):
    """Return the generated text, or None on failure (one retry on transport errors).
    Pass a dict as meta to receive usage and the latest generation timing/errors.
    Each message attempt has a timeout+30s wall-clock bound. Health checks,
    session cleanup and the transport retry can extend the full call."""
    started = time.monotonic()
    report = {"attempts": 0, "ok": False, "failures": []}
    if isinstance(meta, dict):
        meta["generation"] = report
    try:
        result = _generate(model, prompt, variant, timeout, meta, tools, report)
        report["ok"] = bool(result)
        return result
    finally:
        report["seconds"] = round(time.monotonic() - started, 3)


def _failure(report, category, started, status=None):
    report["failures"].append({
        "category": category,
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "seconds": round(time.monotonic() - started, 3),
    })


def _generate(model, prompt, variant, timeout, meta, tools, report):
    started = time.monotonic()
    pool = _load()
    if not pool or not pool.get("servers"):
        _failure(report, "no_pool", started)
        return None
    provider, model_id = model.split("/", 1)
    for attempt in range(2):
        started = time.monotonic()
        report["attempts"] += 1
        srv = random.choice(pool["servers"])
        sid = None
        try:
            if not _healthy(pool, srv["port"]) and not _revive(pool, srv):
                _failure(report, "unhealthy_pool", started)
                continue
            ses = _req(pool, srv["port"], "POST", "/session",
                       {"title": f"gen-{os.getpid()}", "agent": "build", "permission": PERMISSION})
            sid = ses["id"]
            body = {"agent": "build",
                    "model": {"providerID": provider, "modelID": model_id},
                    "parts": [{"type": "text", "text": prompt}]}
            if not tools:
                body["tools"] = {"*": False}
            if variant:
                body["variant"] = variant
            box = {}

            def _post():
                try:
                    box["msg"] = _req(pool, srv["port"], "POST",
                                      f"/session/{sid}/message", body, timeout=timeout)
                except Exception as e:
                    box["err"] = e
            th = threading.Thread(target=_post, daemon=True)
            th.start()
            th.join(timeout + 30)
            if "err" in box:
                raise box["err"]
            if "msg" not in box:
                raise TimeoutError(f"generation exceeded {timeout + 30}s wall clock")
            msg = box["msg"]
            steps = [p for p in msg["parts"]
                     if p.get("type") == "step-finish" and (p.get("tokens") or p.get("cost"))]
            for u in (steps or [msg["info"]]):
                _acc_usage(meta, u)
            if msg["info"].get("error"):
                error = msg["info"]["error"]
                data = error.get("data") if isinstance(error, dict) else None
                status = data.get("statusCode") if isinstance(data, dict) else None
                _failure(report, "provider_error", started, status)
                return None
            text = "".join(p.get("text", "") for p in msg["parts"] if p.get("type") == "text")
            if not text:
                _failure(report, "empty_response", started)
            return text
        except Exception as error:
            status = None
            if isinstance(error, urllib.error.HTTPError):
                category, status = "http_error", error.code
            elif isinstance(error, TimeoutError) or (
                    isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)):
                category = "timeout"
            elif isinstance(error, (KeyError, TypeError, ValueError, AttributeError)):
                category = "invalid_response"
            else:
                category = "transport_error"
            _failure(report, category, started, status)
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
