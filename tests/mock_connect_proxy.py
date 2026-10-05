#!/usr/bin/env python3
"""Mock HTTP CONNECT proxy for testing (no phone required).

Modes:
  default    forwards CONNECT tunnels and absolute-URI GETs to real upstreams
  --canned   answers everything itself: 200 for CONNECT, a fixed page for GET
             (works fully offline — used by the netns end-to-end test)

Usage: mock_connect_proxy.py PORT [--canned]
"""
import socket
import sys
import threading

CANNED_BODY = b"<html><body>TETHERFUSE-MOCK-PROXY-OK</body></html>\n"


def read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return None, None
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    return lines[0], data


def pipe(a, b):
    try:
        while True:
            chunk = a.recv(65536)
            if not chunk:
                break
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            a.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def handle(conn, addr, canned):
    try:
        reqline, data = read_request(conn)
        if not reqline:
            return
        method, target, _proto = reqline.split(" ", 2)
        print(f"mock-proxy: {addr[0]} -> {reqline}", file=sys.stderr, flush=True)

        if method == "CONNECT":
            if canned:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                # keep the tunnel open; echo-close on EOF
                conn.recv(65536)
                return
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=10)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            t = threading.Thread(target=pipe, args=(upstream, conn), daemon=True)
            pipe(conn, upstream)
            t.join(timeout=5)
        elif method in ("GET", "HEAD"):
            if canned:
                body = CANNED_BODY if method == "GET" else b""
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                return
            # absolute-URI proxying: http://host:port/path
            rest = target.split("://", 1)[1]
            hostpart, _, path = rest.partition("/")
            host, _, port = hostpart.rpartition(":")
            if not port.isdigit():
                host, port = hostpart, "80"
            sock = socket.create_connection((host, int(port)), timeout=10)
            sock.sendall(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
        else:
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
    except OSError as e:
        print(f"mock-proxy: error: {e}", file=sys.stderr, flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    port = int(sys.argv[1])
    canned = "--canned" in sys.argv[3:] or (len(sys.argv) > 2 and "--canned" in sys.argv)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(16)
    print(f"mock-proxy: listening on 0.0.0.0:{port} canned={canned}",
          file=sys.stderr, flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr, canned), daemon=True).start()


if __name__ == "__main__":
    main()
