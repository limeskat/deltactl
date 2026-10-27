# tests/test_cli_e2e.py
import re
import subprocess
import sys
import threading

from deltactl.cli import main


def _readline(stream, timeout=5.0):
    box = {}
    thread = threading.Thread(target=lambda: box.update(line=stream.readline()), daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "mock did not print its listening address"
    return box["line"]


def test_mock_subprocess_and_all_commands():
    proc = subprocess.Popen(
        [sys.executable, "-m", "deltactl.cli", "mock", "--listen", "127.0.0.1:0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = _readline(proc.stderr)
        match = re.search(r"listening on ([^:]+):(\d+)", line)
        assert match, line
        host, port = match.group(1), int(match.group(2))
        assert main(["read", "D1000", "--host", host, "--port", str(port)]) == 0
        assert main(["write", "D9000", "9", "--host", host, "--port", str(port)]) == 0
        assert main(["watch", "D9000", "--count", "1", "--host", host, "--port", str(port)]) == 0
        assert main(["doctor", "--host", host, "--port", str(port), "--json"]) == 0
    finally:
        proc.terminate()
        proc.wait(5)
