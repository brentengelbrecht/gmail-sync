"""Test helpers: start the server as a subprocess, and a tiny raw IMAP client.

The server is exercised as a black box (its real command line, config file and sockets),
so these tests also work against any other version of imap_archive_server.py:

    ARCHIVE_SERVER=/path/to/other/imap_archive_server.py python3 -m unittest discover tests
"""
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.environ.get("ARCHIVE_SERVER") or os.path.join(os.path.dirname(HERE),
                                                             "imap_archive_server.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerProc:
    """accounts: {login: (repo_path, password, {extra account options})}."""

    def __init__(self, accounts, server_opts=None, tmp=None, log_level="INFO", tls_opts=None,
                 auth_opts=None, logging_opts=None):
        self.tmp = tmp or tempfile.mkdtemp(prefix="imapd-test-")
        self.port = free_port()
        self.ini = os.path.join(self.tmp, "srv.ini")
        self.pw = os.path.join(self.tmp, "srv.passwd")
        self.logfile = os.path.join(self.tmp, "srv.log")
        server = {"host": "127.0.0.1", "port": self.port}
        server.update(server_opts or {})                 # may override host
        lines = ["[server]"] + [f"{k} = {v}" for k, v in server.items()]
        if tls_opts:
            lines += ["[tls]"] + [f"{k} = {v}" for k, v in tls_opts.items()]
        auth = {"password_file": self.pw}
        auth.update(auth_opts or {})
        logging_ = {"level": log_level, "file": self.logfile}
        logging_.update(logging_opts or {})
        lines += ["[auth]"] + [f"{k} = {v}" for k, v in auth.items()]
        lines += ["[logging]"] + [f"{k} = {v}" for k, v in logging_.items()]
        for login, (path, _pw, extra) in accounts.items():
            lines += [f"[account:{login}]", f"path = {path}"]
            lines += [f"{k} = {v}" for k, v in (extra or {}).items()]
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        for login, (_path, password, _extra) in accounts.items():
            r = self.run("--set-password", login, input=f"{password}\n{password}\n")
            assert r.returncode == 0, r.stderr
        self.proc = None

    def run(self, *args, **kw):
        return subprocess.run([sys.executable, SERVER_PY, "--config", self.ini, *args],
                              capture_output=True, text=True, **kw)

    def start(self):
        self.proc = subprocess.Popen([sys.executable, SERVER_PY, "--config", self.ini],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: " + self.proc.stderr.read().decode())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for p in (self.proc.stdout, self.proc.stderr) if self.proc else ():
            p.close()

    def log(self):
        try:
            with open(self.logfile, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class Resp:
    def __init__(self, blob, line):
        self.blob = blob           # all untagged data, literals included
        self.line = line           # the tagged status line, without CRLF
        parts = line.split(" ", 2)
        self.status = parts[1] if len(parts) > 1 else ""
        self.text = parts[2] if len(parts) > 2 else ""

    def ok(self):
        return self.status == "OK"

    def literal(self, label):
        """Bytes of the first `label {n}` literal in the untagged data."""
        m = re.search(re.escape(label.encode()) + rb" \{(\d+)\}\r\n", self.blob)
        if not m:
            return None
        return self.blob[m.end():m.end() + int(m.group(1))]


class Client:
    def __init__(self, port, timeout=15, tls=False):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # self-signed test cert
            self.s = ctx.wrap_socket(self.s)
        self.f = self.s.makefile("rb")
        self.n = 0
        self.greeting = self.f.readline()

    def close(self):
        try:
            self.f.close()
            self.s.close()
        except OSError:
            pass

    def send(self, data):
        self.s.sendall(data if isinstance(data, bytes) else data.encode())

    def read_until(self, tag):
        blob = b""
        while True:
            line = self.f.readline()
            if not line:
                raise EOFError(blob.decode("utf-8", "replace"))
            m = re.search(rb"\{(\d+)\}\r\n$", line)
            while m:
                line += self.f.read(int(m.group(1)))
                rest = self.f.readline()
                line += rest
                m = re.search(rb"\{(\d+)\}\r\n$", rest)
            if line.startswith(tag.encode() + b" "):
                return Resp(blob, line.decode("utf-8", "replace").rstrip("\r\n"))
            blob += line

    def cmd(self, text):
        self.n += 1
        tag = f"t{self.n}"
        self.send(f"{tag} {text}\r\n")
        return self.read_until(tag)

    def login(self, user, password):
        r = self.cmd(f'LOGIN {user} "{password}"')
        assert r.ok(), r.line
        return r

    def read_eof(self, timeout=None):
        """Everything the server sends until it closes the connection."""
        if timeout:
            self.s.settimeout(timeout)
        data = b""
        while True:
            chunk = self.f.read1(65536) if hasattr(self.f, "read1") else self.f.read(1)
            if not chunk:
                return data
            data += chunk
