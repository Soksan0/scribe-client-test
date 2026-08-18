from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILE = ROOT / ".scribe_data" / "runtime.json"


def load_local_environment() -> None:
    for filename in (".env", ".env.local"):
        path = ROOT / filename
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
                value = value[1:-1]
            if key and not os.environ.get(key):
                os.environ[key] = value


def get_json(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_scribe_api(port: int) -> bool:
    payload = get_json(f"http://127.0.0.1:{port}/api/health")
    return bool(payload and payload.get("status") == "ok" and payload.get("service") == "scribe")


def is_frontend(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}", timeout=1) as response:
            page = response.read(100_000).decode("utf-8", errors="ignore")
            return response.status == 200 and "Scribe" in page
    except (OSError, urllib.error.URLError):
        return False


def port_available(port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family, socket.SOCK_STREAM) as handle:
            handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                handle.bind((address, port))
            except OSError:
                return False
    return True


def available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 100):
        if port_available(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def wait_for(url: str, process: subprocess.Popen | None = None, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"A Scribe service stopped during startup (exit {process.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Scribe did not become ready at {url}")


def reusable_runtime() -> tuple[int, int] | None:
    if not RUNTIME_FILE.exists():
        return None
    try:
        runtime = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        api_port, ui_port = int(runtime["api_port"]), int(runtime["ui_port"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return (api_port, ui_port) if is_scribe_api(api_port) and is_frontend(ui_port) else None


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    load_local_environment()
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not python.exists() or npm is None:
        print("Run the setup steps in README.md before starting Scribe.", file=sys.stderr)
        return 2

    reusable = reusable_runtime()
    if reusable:
        api_port, ui_port = reusable
        address = f"http://localhost:{ui_port}"
        print(f"Scribe is already running at {address}")
        webbrowser.open(address)
        return 0

    existing_api_port = next((port for port in range(8000, 8100) if is_scribe_api(port)), None)
    api_port = existing_api_port if existing_api_port is not None else available_port(8000)
    ui_port = available_port(3000)
    processes: list[subprocess.Popen] = []
    owns_api = not is_scribe_api(api_port)
    try:
        if owns_api:
            backend = subprocess.Popen(
                [str(python), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
                cwd=ROOT,
            )
            processes.append(backend)
            wait_for(f"http://127.0.0.1:{api_port}/api/health", backend)

        environment = os.environ.copy()
        environment.pop("NEXT_PUBLIC_SCRIBE_API_URL", None)
        environment["SCRIBE_API_PROXY_TARGET"] = f"http://127.0.0.1:{api_port}"
        frontend = subprocess.Popen(
            [npm, "exec", "vinext", "--", "dev", "--host", "127.0.0.1", "--port", str(ui_port), "--strictPort"],
            cwd=ROOT,
            env=environment,
        )
        processes.append(frontend)
        address = f"http://localhost:{ui_port}"
        wait_for(address, frontend)
        RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_FILE.write_text(json.dumps({"api_port": api_port, "ui_port": ui_port, "pid": os.getpid()}), encoding="utf-8")
        print(f"Scribe is running locally at {address}. Press Ctrl+C to stop it.")
        webbrowser.open(address)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        return next((process.returncode for process in processes if process.returncode), 0) or 0
    except KeyboardInterrupt:
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        stop(processes)
        try:
            if RUNTIME_FILE.exists() and json.loads(RUNTIME_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                RUNTIME_FILE.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
