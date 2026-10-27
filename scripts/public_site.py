"""Start/stop the dedicated public server and Cloudflare connector on Windows."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TraceReview" / "deployment"
STATE = DEPLOY / "review-state.json"
STOP = DEPLOY / "review-stop.request"
CONFIG = DEPLOY / "review-tunnel.yml"
CONTROL_PORT = 8769
APP_PORT = 8767
ORIGIN = "https://review.njfu.org"
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def active(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.4):
            return True
    except OSError:
        return False


def save_state(data):
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE)


def supervise():
    # The bound port prevents duplicate supervisors; only owned child handles are stopped.
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", CONTROL_PORT))
    except OSError:
        return
    guard.listen(16)
    guard.setblocking(False)
    children = {}
    streams = []
    try:
        if not CONFIG.exists():
            raise RuntimeError("Cloudflare tunnel configuration is missing.")
        if active(APP_PORT):
            raise RuntimeError("Public port 8767 is already in use; no existing process was stopped.")
        STOP.unlink(missing_ok=True)
        commands = {
            "app": [sys.executable, "-u", "-X", "utf8", str(ROOT / "app.py"), "--no-browser",
                    "--port", str(APP_PORT), "--public-origin", ORIGIN,
                    "--storage", str(DEPLOY.parent / "public-workspaces")],
            "tunnel": [str(DEPLOY / "cloudflared.exe"), "tunnel", "--config", str(CONFIG),
                       "--no-autoupdate", "run", "njfu-review"],
        }
        logs = {name: (DEPLOY / f"review-{name}.log").open("ab", buffering=0) for name in commands}
        streams.extend(logs.values())
        next_start = {name: 0 for name in commands}
        while not STOP.exists():
            try:
                connection, _ = guard.accept()
                connection.close()
            except BlockingIOError:
                pass
            for name, command in commands.items():
                child = children.get(name)
                if child is not None and child.poll() is not None:
                    children.pop(name)
                    next_start[name] = time.monotonic() + 5
                    child = None
                if child is None and time.monotonic() >= next_start[name]:
                    children[name] = subprocess.Popen(command, cwd=ROOT, stdout=logs[name], stderr=logs[name],
                                                       stdin=subprocess.DEVNULL, creationflags=HIDDEN)
            save_state({"status":"running", "url":ORIGIN, "supervisor_pid":os.getpid(),
                        "processes":{name:child.pid for name, child in children.items()},
                        "updated_at":time.time()})
            time.sleep(1)
    except Exception as error:
        save_state({"status":"error", "message":str(error), "updated_at":time.time()})
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
        for stream in streams:
            stream.close()
        guard.close()
        if STOP.exists():
            save_state({"status":"stopped", "url":ORIGIN, "updated_at":time.time()})
            STOP.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Manage review.njfu.org on this computer")
    parser.add_argument("action", choices=("start", "stop", "status", "run"))
    args = parser.parse_args()
    DEPLOY.mkdir(parents=True, exist_ok=True)
    if args.action == "run":
        return supervise()
    if args.action == "start":
        if active(CONTROL_PORT):
            print("Public site is already running: " + ORIGIN)
            return
        if not CONFIG.exists():
            raise SystemExit("Complete the Cloudflare tunnel setup first.")
        subprocess.Popen([sys.executable, "-u", "-X", "utf8", str(Path(__file__).resolve()), "run"],
                         cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=HIDDEN)
        for _ in range(30):
            time.sleep(.2)
            if active(APP_PORT):
                print("Public server started: " + ORIGIN)
                return
        raise SystemExit("Startup is not ready. Check " + str(STATE))
    if args.action == "stop":
        if not active(CONTROL_PORT):
            print("Public site is already stopped.")
            return
        STOP.touch()
        print("Stopping the public site. The local app on port 8765 is unaffected.")
        return
    print(STATE.read_text("utf-8") if STATE.exists() else "Not started.")
    print("Public server listening: " + str(active(APP_PORT)))


if __name__ == "__main__":
    main()
