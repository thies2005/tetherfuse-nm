"""Verifies the mock CONNECT proxy itself (used by e2e tests and manual runs)."""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def _wait_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _local_origin():
    """Tiny origin server returning a marker body."""
    marker = b"ORIGIN-MARKER-12345"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                         + str(len(marker)).encode() + b"\r\n\r\n" + marker)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], marker


def test_forward_and_canned_modes(tmp_path):
    origin_port, marker = _local_origin()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    proxy_port = s.getsockname()[1]
    s.close()  # free it for the proxy

    here = os.path.dirname(__file__)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(here, "mock_connect_proxy.py"), str(proxy_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_port(proxy_port), "proxy did not start"

        # Plain GET through the proxy (absolute-URI form).
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"}))
        body = opener.open(f"http://127.0.0.1:{origin_port}/x", timeout=10).read()
        assert marker in body
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
