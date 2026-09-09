"""Fresh service boot uses the explicitly selected store and local knowledge API."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
from urllib.request import urlopen


def test_public_cli_starts_without_personal_phone_credentials(tmp_path):
    root = tmp_path.resolve()
    vault = root / "vault"
    for name in ("candidates", "assets", "Notes/散记/碎片想法"):
        (vault / name).mkdir(parents=True)
    database = root / "loop.sqlite3"
    command = [sys.executable, "-c", "import faulthandler, runpy; faulthandler.dump_traceback_later(20); runpy.run_module('fragment_loop.cognitive_server', run_name='__main__')", "--port", "0",
        "--db", str(database), "--graph-db", str(root / "graph.sqlite3"),
        "--vault-root", str(vault), "--product-candidates-dir", str(vault / "candidates"),
        "--product-assets-dir", str(vault / "assets")]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(45):
                process.send_signal(signal.SIGINT)
                _, diagnostics = process.communicate(timeout=10)
                raise AssertionError("service cold-start timed out: " + diagnostics.decode(errors="replace")[-6000:])
            line = process.stdout.readline().decode()
        assert "serving http://127.0.0.1:" in line, process.stderr.read().decode() if process.poll() is not None else line
        port = int(line.split("127.0.0.1:")[1].split("/")[0])
        with urlopen(f"http://127.0.0.1:{port}/fragment/v1/knowledge", timeout=3) as response:
            body = json.load(response)
        assert body["data"] == [] and body["error"] is None
        assert database.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
