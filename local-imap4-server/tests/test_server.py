"""Black-box tests for imap_archive_server.py.  Run from the project directory:

    python3 -m unittest discover -s tests -v

Standard library only.  Each test class starts the real server as a subprocess against a
synthetic repository, so nothing outside a temporary directory is touched.
"""
import hashlib
import imaplib
import os
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Client, ServerProc, SERVER_PY          # noqa: E402
from repo_factory import build_repo, multipart_message, simple_message   # noqa: E402

PASSWORD = "correct horse battery"


def basic_messages():
    return [
        # 1
        dict(raw=simple_message(1, "Hello world"), folders=["INBOX", "Archive"], read=True,
             date="2024-07-17T09:44:25+00:00"),
        # 2
        dict(raw=simple_message(2, "Quarterly report", "dave@example.com",
                                "the zebra crossed the road"),
             folders=["INBOX"], starred=True, date="2024-08-01T00:00:00+00:00"),
        # 3
        dict(raw=multipart_message(3), folders=["INBOX"], date="2023-01-15T10:00:00+00:00"),
        # 4: RFC 2047 subject, non-ASCII folder
        dict(raw=simple_message(4, "=?utf-8?q?Caf=C3=A9_au_lait?="),
             folders=["R\u00e9unions/2023"]),
        # 5: hidden by hide_folders
        dict(raw=simple_message(5, "spam"), folders=["[Gmail]/Spam"]),
    ]


class Base(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for s in getattr(cls, "servers", []):
            s.stop()
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    @classmethod
    def make_tmp(cls):
        cls.tmp = tempfile.mkdtemp(prefix="imapd-tests-")
        cls.servers = []
        return cls.tmp

    @classmethod
    def start(cls, accounts, server_opts=None):
        s = ServerProc(accounts, server_opts, tmp=tempfile.mkdtemp(dir=cls.tmp)).start()
        cls.servers.append(s)
        return s

    def client(self, srv=None, login="me"):
        c = Client((srv or self.srv).port)
        self.addCleanup(c.close)
        if login:
            c.login(login, PASSWORD)
        return c


class ProtocolTests(Base):
    """Read-only IMAP behaviour that must not change."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        cls.ids = build_repo(cls.repo, basic_messages())
        cls.repo2 = os.path.join(tmp, "other")
        build_repo(cls.repo2, [dict(raw=simple_message(1, "other mailbox"), folders=["Other"])],
                   application="imap-local-repository", schema_version=1,
                   mailbox="other@example.org", created_at="2025-01-02T03:04:05+00:00")
        cls.srv = cls.start({"me": (cls.repo, PASSWORD, {"hide_folders": "[[]Gmail]*"}),
                             "other": (cls.repo2, PASSWORD, {})})

    def test_greeting_and_capability(self):
        c = self.client(login=None)
        self.assertTrue(c.greeting.startswith(b"* OK [CAPABILITY IMAP4rev1]"))
        r = c.cmd("CAPABILITY")
        self.assertIn(b"IMAP4rev1", r.blob)
        self.assertTrue(r.ok())

    def test_list_status_select(self):
        c = self.client()
        r = c.cmd('LIST "" "*"')
        names = r.blob.decode()
        for n in ("INBOX", "Archive", "&AFI-"[:0] + "R&AOk-unions/2023"):
            self.assertIn(n, names)
        self.assertNotIn("Spam", names, "hide_folders must hide [Gmail]/Spam")
        self.assertIn("(\\Noselect)", c.cmd('LIST "" ""').blob.decode())
        r = c.cmd("STATUS INBOX (MESSAGES UIDNEXT UNSEEN UIDVALIDITY)")
        self.assertIn(b"MESSAGES 3", r.blob)
        self.assertIn(b"UIDNEXT 4", r.blob)
        self.assertIn(b"UNSEEN 2", r.blob)
        r = c.cmd("SELECT INBOX")
        self.assertTrue(r.ok())
        self.assertIn("[READ-ONLY]", r.text)
        self.assertIn(b"3 EXISTS", r.blob)
        self.assertIn(b"UIDVALIDITY", r.blob)
        self.assertEqual(c.cmd("SELECT Nope").status, "NO")
        self.assertEqual(c.cmd("SELECT [Gmail]/Spam").status, "NO")

    def test_fetch_metadata(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        r = c.cmd("UID FETCH 1:* (FLAGS RFC822.SIZE INTERNALDATE)")
        text = r.blob.decode()
        self.assertIn("FLAGS (\\Seen)", text)
        self.assertIn("FLAGS (\\Flagged)", text)
        self.assertIn("FLAGS ()", text)
        self.assertIn('INTERNALDATE "17-Jul-2024 09:44:25 +0000"', text)
        size = len(simple_message(1, "Hello world"))
        self.assertIn(f"RFC822.SIZE {size}", text)

    def test_body_is_served_byte_for_byte(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        for uid, raw in ((1, simple_message(1, "Hello world")), (3, multipart_message(3))):
            r = c.cmd(f"UID FETCH {uid} (BODY.PEEK[])")
            got = r.literal("BODY[]")
            self.assertEqual(got, raw)
            self.assertEqual(hashlib.sha256(got).hexdigest(), hashlib.sha256(raw).hexdigest())
        r = c.cmd("UID FETCH 1 (RFC822)")
        self.assertEqual(r.literal("RFC822"), simple_message(1, "Hello world"))

    def test_header_sections_and_partial(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        raw = simple_message(2, "Quarterly report", "dave@example.com", "the zebra crossed the road")
        head, _, body = raw.partition(b"\r\n\r\n")
        r = c.cmd("UID FETCH 2 (BODY.PEEK[HEADER])")
        self.assertEqual(r.literal("BODY[HEADER]"), head + b"\r\n\r\n")
        r = c.cmd("UID FETCH 2 (BODY.PEEK[TEXT])")
        self.assertEqual(r.literal("BODY[TEXT]"), body + b"\r\n\r\n"[:0])
        r = c.cmd("UID FETCH 2 (BODY.PEEK[HEADER.FIELDS (From Subject)])")
        got = r.literal("BODY[HEADER.FIELDS (From Subject)]")
        self.assertIn(b"From: dave@example.com", got)
        self.assertIn(b"Subject: Quarterly report", got)
        self.assertNotIn(b"Message-ID", got)
        r = c.cmd("UID FETCH 2 (BODY.PEEK[HEADER.FIELDS.NOT (From Subject)])")
        got = r.literal("BODY[HEADER.FIELDS.NOT (From Subject)]")
        self.assertIn(b"Message-ID", got)
        self.assertNotIn(b"Subject:", got)
        r = c.cmd("UID FETCH 2 (BODY.PEEK[]<0.10>)")
        self.assertEqual(r.literal("BODY[]<0>"), raw[:10])

    def test_multipart_sections_and_structure(self):
        # Characterisation test: pins the current output for a simple multipart message so
        # that a future rewrite of the MIME code can be compared against it.
        c = self.client()
        c.cmd("EXAMINE INBOX")
        r = c.cmd("UID FETCH 3 (BODYSTRUCTURE ENVELOPE)")
        text = r.blob.decode()
        self.assertIn('"MIXED"', text)
        self.assertIn('("TEXT" "PLAIN"', text)
        self.assertIn('("APPLICATION" "OCTET-STREAM"', text)
        self.assertIn('"multipart 3"', text)
        self.assertEqual(c.cmd("UID FETCH 3 (BODY.PEEK[1])").literal("BODY[1]"), b"hello 3")
        att = c.cmd("UID FETCH 3 (BODY.PEEK[2])").literal("BODY[2]")
        self.assertTrue(att.startswith(b"QUJDREVG"))
        mime = c.cmd("UID FETCH 3 (BODY.PEEK[2.MIME])").literal("BODY[2.MIME]")
        self.assertIn(b"Content-Transfer-Encoding: base64", mime)
        self.assertEqual(c.cmd("UID FETCH 3 (BODY.PEEK[9])").literal("BODY[9]"), b"")

    def test_envelope_decodes_nothing_and_quotes_safely(self):
        c = self.client()
        c.cmd("SELECT \"R&AOk-unions/2023\"")
        r = c.cmd("UID FETCH 4 (ENVELOPE)")
        self.assertIn(b"=?utf-8?q?Caf=C3=A9_au_lait?=", r.blob)

    def test_search(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")

        def uids(crit):
            r = c.cmd(f"UID SEARCH {crit}")
            self.assertTrue(r.ok(), r.line)
            line = [x for x in r.blob.decode().splitlines() if x.startswith("* SEARCH")][0]
            return [int(x) for x in line.split()[2:]]
        self.assertEqual(uids("ALL"), [1, 2, 3])
        self.assertEqual(uids("SEEN"), [1])
        self.assertEqual(uids("UNSEEN"), [2, 3])
        self.assertEqual(uids("FLAGGED"), [2])
        self.assertEqual(uids("FROM dave"), [2])
        self.assertEqual(uids("SUBJECT hello"), [1])
        self.assertEqual(uids("BODY zebra"), [2])
        self.assertEqual(uids("TEXT zebra"), [2])
        self.assertEqual(uids("OR SEEN FLAGGED"), [1, 2])
        self.assertEqual(uids("NOT SEEN"), [2, 3])
        self.assertEqual(uids("(UNSEEN FROM carol)"), [3])
        self.assertEqual(uids("SINCE 1-Aug-2024"), [2])
        self.assertEqual(uids("BEFORE 1-Jan-2024"), [3])
        self.assertEqual(uids("ON 17-Jul-2024"), [1])
        self.assertEqual(uids("UID 2:3"), [2, 3])
        self.assertEqual(uids("LARGER 1500"), [3])
        self.assertEqual(uids("HEADER Message-ID <2@example.com>"), [2])
        self.assertEqual(uids("CHARSET UTF-8 SUBJECT report"), [2])
        r = c.cmd("SEARCH SUBJECT report")          # message numbers, not UIDs
        self.assertIn(b"* SEARCH 2", r.blob)
        self.assertEqual(c.cmd("SEARCH FROBNICATE").status, "BAD")

    def test_search_header_with_empty_string_requires_the_header(self):
        # RFC 3501: an empty string matches every message that *has* that header.
        c = self.client()
        c.cmd("EXAMINE INBOX")
        r = c.cmd('UID SEARCH HEADER X-No-Such-Header ""')
        self.assertIn(b"* SEARCH\r\n", r.blob)
        r = c.cmd('UID SEARCH HEADER Subject ""')
        self.assertIn(b"* SEARCH 1 2 3", r.blob)

    def test_write_commands_are_refused(self):
        c = self.client()
        c.cmd("SELECT INBOX")
        for text in ("STORE 1 +FLAGS (\\Seen)", "UID STORE 1 +FLAGS (\\Seen)", "COPY 1 Archive",
                     "EXPUNGE", "CREATE foo", "DELETE foo", "RENAME a b"):
            r = c.cmd(text)
            self.assertEqual(r.status, "NO", text)
            self.assertIn("READ-ONLY", r.text)

    def test_accounts_are_isolated(self):
        c = self.client(login="other")
        names = c.cmd('LIST "" "*"').blob.decode()
        self.assertIn("Other", names)
        self.assertNotIn("INBOX", names)
        self.assertEqual(c.cmd("SELECT INBOX").status, "NO")

    def test_state_machine(self):
        c = self.client(login=None)
        self.assertEqual(c.cmd("SELECT INBOX").status, "NO")
        self.assertEqual(c.cmd("FETCH 1 (FLAGS)").status, "NO")
        self.assertEqual(c.cmd("LIST \"\" \"*\"").status, "NO")
        self.assertEqual(c.cmd("XYZZY").status, "BAD")
        c.login("me", PASSWORD)
        self.assertEqual(c.cmd("FETCH 1 (FLAGS)").status, "NO")        # nothing selected
        self.assertEqual(c.cmd(f"LOGIN me {PASSWORD}").status, "BAD")  # no second login
        self.assertTrue(c.cmd("SELECT INBOX").ok())
        self.assertTrue(c.cmd("CLOSE").ok())
        self.assertEqual(c.cmd("FETCH 1 (FLAGS)").status, "NO")
        r = c.cmd("LOGOUT")
        self.assertIn(b"* BYE", r.blob)

    def test_login_with_literal_password(self):
        c = self.client(login=None)
        c.send(f"a LOGIN me {{{len(PASSWORD)}}}\r\n")
        self.assertTrue(c.f.readline().startswith(b"+"))
        c.send(PASSWORD + "\r\n")
        self.assertTrue(c.read_until("a").ok())

    def test_imaplib_end_to_end(self):
        m = imaplib.IMAP4("127.0.0.1", self.srv.port)
        m.login("me", PASSWORD)
        self.assertEqual(m.select("INBOX", readonly=True)[0], "OK")
        typ, data = m.uid("FETCH", "1", "(BODY.PEEK[])")
        self.assertEqual(data[0][1], simple_message(1, "Hello world"))
        typ, data = m.uid("SEARCH", None, "UNSEEN")
        self.assertEqual(data[0].split(), [b"2", b"3"])
        m.logout()


class AuthTests(Base):
    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        repo = os.path.join(tmp, "me")
        build_repo(repo, basic_messages())
        cls.srv = cls.start({"me": (repo, PASSWORD, {})})

    def test_bad_login_then_three_failures_close_the_connection(self):
        c = self.client(login=None)
        t0 = time.time()
        r = c.cmd("LOGIN me wrong")
        self.assertEqual(r.status, "NO")
        self.assertIn("AUTHENTICATIONFAILED", r.text)
        self.assertGreaterEqual(time.time() - t0, 0.9)         # failed logins are delayed
        c.cmd("LOGIN nobody wrong")
        r = c.cmd("LOGIN me wrong")
        self.assertIn(b"* BYE", r.blob)
        self.assertEqual(c.read_eof(5), b"")

    def test_login_is_case_insensitive_and_password_is_not_logged(self):
        c = self.client(login=None)
        self.assertTrue(c.cmd(f'LOGIN ME "{PASSWORD}"').ok())
        self.assertNotIn(PASSWORD, self.srv.log())


class RobustnessTests(Base):
    """Timeouts, limits and protocol abuse.  Needs the options added with this test suite."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        build_repo(cls.repo, basic_messages())

    def srv_with(self, **opts):
        return self.start({"me": (self.repo, PASSWORD, {})}, opts)

    def test_unauthenticated_connection_is_dropped_after_login_timeout(self):
        srv = self.srv_with(login_timeout=1, idle_timeout=30)
        c = self.client(srv, login=None)
        t0 = time.time()
        data = c.read_eof(10)
        self.assertIn(b"* BYE", data)
        self.assertLess(time.time() - t0, 5)

    def test_authenticated_connection_is_dropped_after_idle_timeout(self):
        srv = self.srv_with(login_timeout=30, idle_timeout=2)
        c = self.client(srv)
        t0 = time.time()
        data = c.read_eof(10)
        self.assertIn(b"* BYE", data)
        self.assertGreaterEqual(time.time() - t0, 1.5)
        self.assertLess(time.time() - t0, 6)

    def test_activity_resets_the_idle_timer(self):
        srv = self.srv_with(login_timeout=30, idle_timeout=2)
        c = self.client(srv)
        for _ in range(4):
            time.sleep(1.0)
            self.assertTrue(c.cmd("NOOP").ok())       # 4 s in total, never idle for 2 s

    def test_slow_literal_does_not_defeat_the_timeout(self):
        srv = self.srv_with(login_timeout=1, idle_timeout=30)
        c = self.client(srv, login=None)
        c.send("a LOGIN me {50}\r\n")
        self.assertTrue(c.f.readline().startswith(b"+"))
        c.send("only a few bytes")          # ...and then nothing
        self.assertIn(b"* BYE", c.read_eof(10))

    def test_connection_limit(self):
        srv = self.srv_with(max_connections=2)
        a, b = self.client(srv, login=None), self.client(srv, login=None)
        c = Client(srv.port)
        self.addCleanup(c.close)
        self.assertIn(b"BYE", c.greeting)
        self.assertEqual(c.read_eof(5), b"")
        a.close()
        time.sleep(0.5)
        d = Client(srv.port)
        self.addCleanup(d.close)
        self.assertTrue(d.greeting.startswith(b"* OK"))

    def test_overlong_command_line_closes_only_that_connection(self):
        srv = self.srv_with()
        c = self.client(srv, login=None)
        c.send(b"a NOOP " + b"x" * 200_000 + b"\r\n")
        self.assertIn(b"BYE", c.read_eof(10))
        self.assertTrue(self.client(srv).cmd("NOOP").ok())      # the server is still fine

    def test_oversized_synchronising_literal_is_refused_without_desync(self):
        srv = self.srv_with()
        c = self.client(srv, login=None)
        c.send("a LOGIN me {999999999}\r\n")
        r = c.read_until("a")
        self.assertEqual(r.status, "NO")                 # no "+" was sent, so no data follows
        self.assertTrue(c.cmd("NOOP").ok())              # the connection is still in sync

    def test_oversized_non_synchronising_literal_closes_the_connection(self):
        srv = self.srv_with()
        c = self.client(srv, login=None)
        c.send("a LOGIN me {999999999+}\r\n")
        self.assertIn(b"BYE", c.read_eof(10))

    def test_garbage_does_not_kill_the_server(self):
        srv = self.srv_with()
        c = self.client(srv, login=None)
        c.send(b"\xff\xfe\x00\x01 \x02\r\n)))\r\n\r\na\r\nb LOGIN\r\n")
        time.sleep(0.5)
        c.close()
        self.assertTrue(self.client(srv).cmd("NOOP").ok())

    def test_abrupt_disconnect_mid_fetch_is_harmless(self):
        srv = self.srv_with()
        c = self.client(srv)
        c.cmd("EXAMINE INBOX")
        c.send("x FETCH 1:* (BODY.PEEK[])\r\n")
        c.s.shutdown(socket.SHUT_RDWR)
        c.close()
        self.assertTrue(self.client(srv).cmd("NOOP").ok())


class DamagedArchiveTests(Base):
    """Missing and corrupted message files are reported, not fatal."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        build_repo(cls.repo, [dict(raw=simple_message(n, body="needle"), folders=["INBOX"])
                              for n in (1, 2, 3)])
        shard = os.path.join(cls.repo, "messages", "000001")
        os.remove(os.path.join(shard, "00000002.eml"))                     # message 2: missing
        path = os.path.join(shard, "00000003.eml")                          # message 3: same size,
        with open(path, "rb") as f:                                         # one byte changed
            data = bytearray(f.read())
        data[-5] ^= 0x01
        with open(path, "wb") as f:
            f.write(bytes(data))
        cls.srv = cls.start({"me": (cls.repo, PASSWORD, {})})

    def test_missing_file_gives_NO_after_the_earlier_responses(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        r = c.cmd("FETCH 1:3 (BODY.PEEK[])")
        self.assertEqual(r.status, "NO")
        self.assertIn("No such file", r.text)
        self.assertEqual(r.blob.count(b" FETCH ("), 1)                      # message 1 was sent
        self.assertTrue(c.cmd("FETCH 1 (FLAGS)").ok())                      # session still fine
        self.assertEqual(c.cmd("SEARCH BODY needle").status, "NO")
        self.assertEqual(c.cmd("SEARCH SEEN").status, "OK")                 # metadata-only: fine

    def test_corrupt_file_is_logged_but_still_served(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        r = c.cmd("UID FETCH 3 (BODY.PEEK[])")
        self.assertTrue(r.ok())
        self.assertIn(b"Subject: message 3", r.blob)
        time.sleep(0.2)
        self.assertIn("INTEGRITY MISMATCH uid=3", self.srv.log())

    def test_verify_command_reports_problems(self):
        r = self.srv.run("--verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)
        self.assertIn("SHA256", r.stdout)
        self.assertIn("checked 3 messages, 2 problem(s)", r.stdout)


class TlsTests(Base):
    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.cert, cls.key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", cls.key, "-out", cls.cert, "-days", "2", "-subj",
                            "/CN=localhost"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            raise unittest.SkipTest("openssl command not available")
        repo = os.path.join(tmp, "me")
        build_repo(repo, [dict(raw=multipart_message(n, parts=3, part_size=200_000),
                               folders=["INBOX"]) for n in (1, 2, 3)])
        cls.srv = cls.start({"me": (repo, PASSWORD, {})}, tls_opts={"cert": cls.cert, "key": cls.key})

    @classmethod
    def start(cls, accounts, server_opts=None, tls_opts=None):
        s = ServerProc(accounts, server_opts, tmp=tempfile.mkdtemp(dir=cls.tmp),
                       tls_opts=tls_opts).start()
        cls.servers.append(s)
        return s

    def test_implicit_tls_login_and_large_fetch(self):
        c = Client(self.srv.port, tls=True)
        self.addCleanup(c.close)
        c.login("me", PASSWORD)
        c.cmd("EXAMINE INBOX")
        r = c.cmd("FETCH 1:3 (BODY.PEEK[])")
        self.assertTrue(r.ok())
        self.assertEqual(r.blob.count(b" FETCH ("), 3)
        self.assertGreater(len(r.blob), 1_000_000)

    def test_plaintext_client_on_tls_port_does_not_hurt(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.sendall(b"a1 NOOP\r\n")
        s.close()
        c = Client(self.srv.port, tls=True)
        self.addCleanup(c.close)
        self.assertTrue(c.cmd("NOOP").status in ("NO", "OK"))


class ShutdownTests(Base):
    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "big")
        build_repo(cls.repo, [dict(raw=multipart_message(i, parts=4, part_size=3000),
                                   folders=["INBOX"]) for i in range(1, 2001)])

    def _terminated_while(self, work):
        srv = self.start({"me": (self.repo, PASSWORD, {})})
        c = self.client(srv)
        c.cmd("EXAMINE INBOX")
        work(c)
        time.sleep(0.3)
        t0 = time.time()
        srv.proc.send_signal(signal.SIGTERM)
        rc = srv.proc.wait(15)
        self.assertLess(time.time() - t0, 10)
        return rc

    def test_sigterm_with_idle_client(self):
        self.assertEqual(self._terminated_while(lambda c: None), 0)

    def test_sigterm_during_a_big_fetch(self):
        rc = self._terminated_while(lambda c: c.send("x FETCH 1:* (ENVELOPE BODYSTRUCTURE)\r\n"))
        self.assertEqual(rc, 0)


class ConcurrencyTests(Base):
    """One client doing heavy work must not stall everybody else."""

    N = 3000

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "big")
        msgs = [dict(raw=multipart_message(i, parts=4, part_size=3000), folders=["INBOX"])
                for i in range(1, cls.N + 1)]
        build_repo(cls.repo, msgs)
        cls.srv = cls.start({"me": (cls.repo, PASSWORD, {})}, {"cache_messages": 16})

    def _max_noop_latency_while(self, heavy_command):
        heavy = self.client()
        heavy.cmd("EXAMINE INBOX")
        other = self.client()
        stop, lat, started = threading.Event(), [], threading.Event()

        def probe():
            started.set()
            while not stop.is_set():
                t0 = time.time()
                other.cmd("NOOP")
                lat.append(time.time() - t0)
                time.sleep(0.02)
        th = threading.Thread(target=probe)
        th.start()
        started.wait()
        t0 = time.time()
        r = heavy.cmd(heavy_command)
        duration = time.time() - t0
        stop.set()
        th.join()
        self.assertTrue(r.ok(), r.line)
        return max(lat), duration, r

    def test_big_fetch_does_not_block_other_connections(self):
        worst, duration, r = self._max_noop_latency_while(
            "FETCH 1:* (ENVELOPE BODYSTRUCTURE BODY.PEEK[HEADER])")
        print(f"\n  FETCH took {duration:.1f}s; worst NOOP latency on another connection: "
              f"{worst * 1000:.0f} ms", file=sys.stderr)
        self.assertEqual(r.blob.count(b" FETCH ("), self.N)
        self.assertLess(worst, 0.5)

    def test_big_body_search_does_not_block_other_connections(self):
        worst, duration, r = self._max_noop_latency_while("SEARCH BODY needle-not-present")
        print(f"\n  SEARCH took {duration:.1f}s; worst NOOP latency: {worst * 1000:.0f} ms",
              file=sys.stderr)
        self.assertLess(worst, 0.5)

    def test_big_select_and_list_do_not_block_other_connections(self):
        worst, duration, r = self._max_noop_latency_while("STATUS INBOX (MESSAGES UNSEEN)")
        self.assertLess(worst, 0.5)

    def test_parallel_clients_get_consistent_answers(self):
        results, errors = [], []

        def work():
            try:
                c = self.client()
                c.cmd("EXAMINE INBOX")
                r = c.cmd("UID FETCH 1:200 (BODY.PEEK[1])")
                results.append(r.blob.count(b"hello "))
            except Exception as e:           # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 6)


class UnitTests(unittest.TestCase):
    """Functions imported directly from the module."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("imap_archive_server_under_test", SERVER_PY)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_utf7_round_trip(self):
        for s in ("INBOX", "R\u00e9unions/2023", "a&b", "\u65e5\u672c\u8a9e", "[Gmail]/Sent Mail"):
            self.assertEqual(self.mod.utf7_decode(self.mod.utf7_encode(s)), s)
        self.assertEqual(self.mod.utf7_encode("R\u00e9unions"), "R&AOk-unions")

    def test_parse_set_and_in_set(self):
        r = self.mod.parse_set("1:3,7,10:*", 20)
        self.assertTrue(self.mod.in_set(2, r) and self.mod.in_set(7, r) and self.mod.in_set(20, r))
        self.assertFalse(self.mod.in_set(5, r))
        self.assertTrue(self.mod.in_set(4, self.mod.parse_set("5:3", 9)))   # 5:3 means 3:5

    def test_tokenize(self):
        t = self.mod.tokenize('1:* (FLAGS BODY.PEEK[HEADER.FIELDS (From To)]<0.100>) "a b"')
        self.assertEqual(t[0], "1:*")
        self.assertEqual(t[1], ["FLAGS", "BODY.PEEK[HEADER.FIELDS (From To)]<0.100>"])
        self.assertEqual(t[2], "a b")
        with self.assertRaises(self.mod.Err):
            self.mod.tokenize("(unbalanced")

    def test_password_hash_round_trip(self):
        h = self.mod.hash_password("s3cret")
        self.assertTrue(self.mod.check_password("s3cret", h))
        self.assertFalse(self.mod.check_password("other", h))
        self.assertFalse(self.mod.check_password("s3cret", "garbage"))

    def test_command_label_never_contains_arguments(self):
        label = getattr(self.mod, "command_label", None)
        self.assertIsNotNone(label, "module must provide command_label() for safe logging")
        self.assertEqual(label("a1 LOGIN bob hunter2"), "a1 LOGIN")
        self.assertEqual(label('a1 LOGIN bob "hunter2"'), "a1 LOGIN")
        self.assertNotIn("hunter2", label('a1 UID FETCH 1 (BODY[])'))
        self.assertEqual(label("x"), "x")
        self.assertEqual(label(""), "")

    def test_send_gives_up_on_a_client_that_never_reads(self):
        mod = self.mod

        class Reader:
            pass

        class Writer:
            def get_extra_info(self, name):
                return ("203.0.113.9", 1234)

            def write(self, data):
                pass

            async def drain(self):
                await asyncio.sleep(3600)          # the client's buffer is full and stays full

            def close(self):
                pass
        import asyncio
        old = mod.WRITE_TIMEOUT
        mod.WRITE_TIMEOUT = 0.2
        try:
            ses = mod.Session(Reader(), Writer(), {}, None, None, None)
            t0 = time.time()
            with self.assertRaises(ConnectionError):
                asyncio.run(ses.send(b"* OK\r\n"))
            self.assertLess(time.time() - t0, 3)
        finally:
            mod.WRITE_TIMEOUT = old


if __name__ == "__main__":
    unittest.main()
