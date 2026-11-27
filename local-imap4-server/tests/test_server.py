"""Black-box tests for imap_archive_server.py.  Run from the project directory:

    python3 -m unittest discover -s tests -v

Standard library only.  Each test class starts the real server as a subprocess against a
synthetic repository, so nothing outside a temporary directory is touched.
"""
import hashlib
import imaplib
import os
import random
import re
import shutil
import socket
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Client, ServerProc, SERVER_PY          # noqa: E402
from repo_factory import (build_imap_importer_repo, build_repo, multipart_message,   # noqa: E402
                          simple_message)

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
    def start(cls, accounts, server_opts=None, **kw):
        """kw: auth_opts, logging_opts, tls_opts (see ServerProc)."""
        s = ServerProc(accounts, server_opts, tmp=tempfile.mkdtemp(dir=cls.tmp), **kw).start()
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


class GenericImapRepositoryTests(Base):
    """Repositories written by the generic IMAP importer (Yahoo, Dovecot, Outlook.com over OAuth...).

    The layout differs from the Gmail one: no gm_id, files named <sha256>.eml, a per-folder
    delimiter, and importer-only tables the server ignores.
    """

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        msgs = [
            dict(raw=simple_message(1, "Hello world"), folders=["INBOX", "Archive"], read=True),
            dict(raw=simple_message(2, "Quarterly report", "dave@example.com", "zebra"),
                 folders=["INBOX"], starred=True, flags="\\Answered"),
            dict(raw=multipart_message(3), folders=["Sent Items"]),
        ]
        cls.slash = os.path.join(tmp, "slash")
        build_imap_importer_repo(cls.slash, msgs)
        cls.dot = os.path.join(tmp, "dot")      # Dovecot-style namespace: '.' separates levels
        build_imap_importer_repo(cls.dot, [
            dict(raw=simple_message(1), folders=["INBOX", "INBOX.Sent"]),
            dict(raw=simple_message(2), folders=["INBOX.Projects.Travel"])], delimiter=".")
        cls.srv = cls.start({"me": (cls.slash, PASSWORD, {}), "dot": (cls.dot, PASSWORD, {})})

    def test_list_select_fetch_search(self):
        c = self.client()
        names = c.cmd('LIST "" "*"').blob.decode()
        for n in ("INBOX", "Archive", "Sent Items"):
            self.assertIn(n, names)
        self.assertIn(b"MESSAGES 2", c.cmd("STATUS INBOX (MESSAGES UNSEEN)").blob)
        self.assertTrue(c.cmd("EXAMINE INBOX").ok())
        r = c.cmd("UID FETCH 1:* (FLAGS INTERNALDATE RFC822.SIZE)")
        self.assertIn("FLAGS (\\Seen)", r.blob.decode())
        self.assertIn('INTERNALDATE "17-Jul-2024 09:44:25 +0000"', r.blob.decode())
        self.assertEqual(c.cmd("UID FETCH 1 (BODY.PEEK[])").literal("BODY[]"),
                         simple_message(1, "Hello world"))
        self.assertIn(b"* SEARCH 2", c.cmd("UID SEARCH BODY zebra").blob)
        self.assertIn(b"* SEARCH 2", c.cmd("UID SEARCH FLAGGED").blob)
        c.cmd('SELECT "Sent Items"')
        self.assertEqual(c.cmd("UID FETCH 3 (BODY.PEEK[1])").literal("BODY[1]"), b"hello 3")

    def test_verify_uses_sha256_for_generic_repositories(self):
        r = self.srv.run("--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 problem(s)", r.stdout)

    def test_folder_with_dot_delimiter_is_listed_as_stored(self):
        # Characterisation: the server does not read folders.delimiter; names are listed as stored
        # and always reported with '/' as the hierarchy delimiter. Spec item C2 changes this.
        c = self.client(login="dot")
        names = c.cmd('LIST "" "*"').blob.decode()
        self.assertIn("INBOX.Sent", names)
        self.assertIn("INBOX.Projects.Travel", names)
        self.assertTrue(c.cmd("SELECT INBOX.Sent").ok())


class InboxCaseTests(Base):
    """INBOX is case-insensitive (RFC 3501 6.3.1); every other folder name stays an exact match.

    Yahoo and Outlook.com archives store the folder as 'Inbox', so a standard client's
    'SELECT INBOX' used to answer 'NO no such mailbox'.
    """

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        five = [dict(raw=simple_message(i), folders=["Inbox"], read=True) for i in range(1, 6)]
        other = [dict(raw=simple_message(10), folders=["Sent"]),
                 dict(raw=simple_message(11), folders=["Inboxes"])]
        cls.mixed_case = os.path.join(tmp, "mixed")
        build_imap_importer_repo(cls.mixed_case, five + other)
        cls.both = os.path.join(tmp, "both")             # two spellings stored side by side
        build_imap_importer_repo(cls.both, [dict(raw=simple_message(1), folders=["INBOX"]),
                                            dict(raw=simple_message(2), folders=["INBOX"]),
                                            dict(raw=simple_message(3), folders=["Inbox"])])
        cls.upper = os.path.join(tmp, "upper")           # Gmail style, already 'INBOX'
        build_repo(cls.upper, basic_messages())
        cls.srv = cls.start({"mixed": (cls.mixed_case, PASSWORD, {}),
                             "both": (cls.both, PASSWORD, {}),
                             "upper": (cls.upper, PASSWORD, {}),
                             "hidden": (cls.mixed_case, PASSWORD, {"hide_folders": "Inbox"})})

    def test_every_spelling_of_inbox_opens_the_stored_folder(self):
        c = self.client(login="mixed")
        for name in ("INBOX", "inbox", "Inbox", "iNbOx"):
            for cmd in ("SELECT", "EXAMINE"):
                r = c.cmd(f"{cmd} {name}")
                self.assertTrue(r.ok(), f"{cmd} {name}: {r.line}")
                self.assertIn(b"5 EXISTS", r.blob)
        r = c.cmd("STATUS INBOX (MESSAGES UNSEEN)")
        self.assertIn(b'* STATUS "INBOX" (MESSAGES 5 UNSEEN 0)', r.blob)   # echoes the request
        self.assertEqual(c.cmd("UID FETCH 1 (BODY.PEEK[])").status, "OK")

    def test_list_shows_the_stored_name_and_finds_it_by_any_spelling(self):
        c = self.client(login="mixed")
        full = c.cmd('LIST "" "*"').blob.decode()
        self.assertIn('"Inbox"', full)              # reported as stored, not renamed
        self.assertNotIn('"INBOX"', full)
        for pat in ("INBOX", "inbox", "Inbox"):
            r = c.cmd(f'LIST "" "{pat}"').blob.decode()
            self.assertEqual(r.strip(), '* LIST () "/" "Inbox"', pat)
        self.assertEqual(c.cmd('LIST "" "Inb*"').blob.decode().count("LIST"), 2)   # Inbox, Inboxes
        self.assertEqual(c.cmd('LIST "" "INB*"').blob.decode().strip(), "")        # wildcards unchanged

    def test_other_names_stay_exact(self):
        c = self.client(login="mixed")
        for name in ("sent", "SENT", "Inbo", "Inboxes2", '"Inbox "', "inboxes"):
            self.assertEqual(c.cmd(f"SELECT {name}").status, "NO", name)
        self.assertTrue(c.cmd("SELECT Sent").ok())
        self.assertTrue(c.cmd("SELECT Inboxes").ok())
        self.assertEqual(c.cmd("SELECT \u0131nbox").status, "NO")      # dotless i is not an I

    def test_exact_spelling_wins_when_two_are_stored(self):
        c = self.client(login="both")
        self.assertIn(b"2 EXISTS", c.cmd("SELECT INBOX").blob)
        self.assertIn(b"1 EXISTS", c.cmd("SELECT Inbox").blob)
        self.assertIn(b"2 EXISTS", c.cmd("SELECT inbox").blob)          # first in sort order

    def test_inbox_unchanged_when_already_upper_case(self):
        c = self.client(login="upper")
        self.assertIn(b"3 EXISTS", c.cmd("SELECT INBOX").blob)
        self.assertIn(b"3 EXISTS", c.cmd("SELECT inbox").blob)

    def test_hide_folders_still_hides_the_resolved_folder(self):
        c = self.client(login="hidden")
        for name in ("INBOX", "Inbox", "inbox"):
            self.assertEqual(c.cmd(f"SELECT {name}").status, "NO", name)
        self.assertEqual(c.cmd('LIST "" "INBOX"').blob, b"")


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


def parse_stats(line):
    """'stats: uptime=3s commands=NOOP:3,UID_FETCH:2 ...' -> {'uptime': '3s', ...}; the
    comma-separated counters are turned into dicts of ints."""
    out = {}
    for tok in line.split()[1:]:
        k, _, v = tok.partition("=")
        out[k] = v
    for k in ("commands", "rejected", "errors"):
        out[k] = {} if out.get(k, "-") == "-" else {a: int(b) for a, b in
                                                   (x.rsplit(":", 1) for x in out[k].split(","))}
    return out


def stats_lines(srv):
    return [l for l in srv.log().splitlines() if " stats: uptime=" in l]


def wait_for(cond, timeout=8.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class StatsAndSlowCommandTests(Base):
    """Counters ('stats:' log line, SIGUSR1) and the slow-command warning."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        build_repo(cls.repo, basic_messages())

    def srv_with(self, **logging_opts):
        return self.start({"me": (self.repo, PASSWORD, {})}, logging_opts=logging_opts)

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "needs SIGUSR1")
    def test_sigusr1_writes_exact_counters_without_any_content(self):
        srv = self.srv_with(stats_interval=0)
        c = self.client(srv)                                    # LOGIN ok
        for _ in range(3):
            c.cmd("NOOP")
        c.cmd('SELECT "Secret Folder Name"')                    # fails: no such mailbox
        c.cmd("EXAMINE INBOX")
        c.cmd("UID FETCH 1:2 (FLAGS)")
        c.cmd("UID FETCH 1 (BODY.PEEK[])")
        c.cmd("XYZZY secret-argument")                          # unknown command
        c.cmd("SEARCH " + "(" * 150 + "ALL" + ")" * 150)        # parser limit
        before = len(stats_lines(srv))
        srv.proc.send_signal(signal.SIGUSR1)
        self.assertTrue(wait_for(lambda: len(stats_lines(srv)) > before))
        line = stats_lines(srv)[-1]
        st = parse_stats(line[line.index("stats:"):])
        self.assertEqual(st["logins_ok"], "1")
        self.assertEqual(st["logins_failed"], "0")
        self.assertEqual(st["open"], "1")
        self.assertEqual(st["commands"], {"LOGIN": 1, "NOOP": 3, "SELECT": 1, "EXAMINE": 1,
                                          "UID_FETCH": 2, "(unknown)": 1, "SEARCH": 1})
        self.assertEqual(st["errors"], {"parser_limit": 1})
        self.assertGreater(int(st["bytes_sent"]), 300)
        for secret in ("Secret Folder Name", "secret-argument", PASSWORD, "127.0.0.1"):
            self.assertNotIn(secret, line.split("stats:")[1], secret)

    def test_periodic_stats_line_and_final_line_at_shutdown(self):
        srv = self.srv_with(stats_interval=1)
        c = self.client(srv)
        c.cmd("NOOP")
        self.assertTrue(wait_for(lambda: len(stats_lines(srv)) >= 2, 10))
        c.close()
        srv.stop()
        lines = stats_lines(srv)
        self.assertGreaterEqual(len(lines), 3)               # two periodic ones and the final one
        self.assertLess(srv.log().index(lines[-1]), srv.log().index("shutting down"))

    def test_no_stats_lines_by_default(self):
        srv = self.srv_with()
        self.client(srv).cmd("NOOP")
        time.sleep(1.2)
        srv.stop()
        self.assertEqual(stats_lines(srv), [])

    def test_failed_login_and_rejections_are_counted(self):
        srv = self.start({"me": (self.repo, PASSWORD, {})}, {"max_connections": 1},
                         logging_opts={"stats_interval": 0})
        time.sleep(0.5)             # let the server finish with the readiness probe's connection
        c = self.client(srv, login=None)
        self.assertEqual(c.cmd("LOGIN me wrong").status, "NO")
        extra = Client(srv.port)
        self.addCleanup(extra.close)
        self.assertIn(b"BYE", extra.greeting)
        srv.proc.send_signal(signal.SIGUSR1)
        self.assertTrue(wait_for(lambda: stats_lines(srv)))
        line = stats_lines(srv)[-1]
        st = parse_stats(line[line.index("stats:"):])
        self.assertEqual((st["logins_failed"], st["rejected"]), ("1", {"max_connections": 1}))
        self.assertGreaterEqual(int(st["connections"]), 2)    # includes the startup readiness probe

    def test_slow_command_warning_has_tag_and_command_only(self):
        srv = self.srv_with(slow_command_seconds="0.0001")
        c = self.client(srv)
        c.cmd('SELECT "Secret Folder Name"')
        c.cmd("EXAMINE INBOX")
        c.cmd("UID FETCH 1 (BODY.PEEK[])")
        uid_fetch = r"WARNING slow command: t\d+ UID FETCH took [\d.]+ s \(account me, peer 127\.0\.0\.1\)"
        # the warning is written just after the reply is sent, so give the server a moment
        self.assertTrue(wait_for(lambda: re.search(uid_fetch, srv.log())), srv.log())
        log = srv.log()
        self.assertRegex(log, r"slow command: t\d+ LOGIN took")
        for secret in ("Secret Folder Name", PASSWORD):
            self.assertNotIn(secret, log)

    def test_no_slow_warning_for_fast_commands_by_default(self):
        srv = self.srv_with()
        c = self.client(srv)
        c.cmd("EXAMINE INBOX")
        c.cmd("UID FETCH 1:3 (ENVELOPE)")
        self.assertNotIn("slow command", srv.log())

    def test_slow_command_can_be_switched_off(self):
        srv = self.srv_with(slow_command_seconds=0)
        self.client(srv).cmd("EXAMINE INBOX")
        self.assertNotIn("slow command", srv.log())


class ParserLimitTests(Base):
    """Deep nesting and huge commands get a protocol error, never a traceback or a stall."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        build_repo(cls.repo, [dict(raw=simple_message(i), folders=["INBOX"], read=bool(i % 2))
                              for i in range(1, 3001)])
        cls.srv = cls.start({"me": (cls.repo, PASSWORD, {})})

    def sel(self):
        c = self.client()
        c.cmd("EXAMINE INBOX")
        return c

    def test_nesting_up_to_the_limit_works_and_beyond_is_refused(self):
        c = self.sel()
        ok = c.cmd("SEARCH " + "(" * 100 + "SEEN" + ")" * 100)
        self.assertTrue(ok.ok(), ok.line)
        self.assertTrue(ok.blob.startswith(b"* SEARCH 1 3 5 7"))
        for depth in (101, 500, 30000):
            r = c.cmd("SEARCH " + "(" * depth + "SEEN" + ")" * depth)
            self.assertEqual(r.status, "BAD", depth)
            self.assertIn("nested too deeply", r.text)
        self.assertTrue(c.cmd("NOOP").ok())

    def test_not_and_or_chains_are_limited_too(self):
        c = self.sel()
        self.assertTrue(c.cmd("SEARCH " + "NOT " * 100 + "SEEN").ok())
        r = c.cmd("SEARCH " + "NOT " * 101 + "SEEN")
        self.assertEqual((r.status, "nested too deeply" in r.text), ("BAD", True))
        r = c.cmd("SEARCH " + "NOT " * 15000 + "SEEN")
        self.assertEqual(r.status, "BAD")
        r = c.cmd("SEARCH " + "OR " * 5000 + "SEEN " * 5001)
        self.assertEqual(r.status, "BAD")
        self.assertTrue(c.cmd("NOOP").ok())

    def test_too_many_items(self):
        c = self.sel()
        r = c.cmd("SEARCH " + "ALL " * 10001)
        self.assertEqual((r.status, r.text.startswith("command too complex")), ("BAD", True))
        r = c.cmd("SEARCH " + "ALL " * 9000)           # large but within the limit: just works
        self.assertTrue(r.ok(), r.line)

    def test_fetch_items_nesting(self):
        c = self.sel()
        r = c.cmd("FETCH 1 " + "(" * 30000 + "FLAGS" + ")" * 30000)
        self.assertEqual(r.status, "BAD")

    def test_no_traceback_in_the_log(self):
        c = self.sel()
        c.cmd("SEARCH " + "(" * 5000 + "ALL" + ")" * 5000)
        c.cmd("SEARCH " + "NOT " * 5000 + "ALL")
        time.sleep(0.2)
        self.assertNotIn("Traceback", self.srv.log())
        self.assertNotIn("RecursionError", self.srv.log())

    def test_a_long_message_set_is_answered_quickly_and_correctly(self):
        c = self.sel()
        uids = list(range(2, 6001, 2))                  # 3000 non-adjacent elements, about 17 KB
        t0 = time.time()
        r = c.cmd("UID FETCH " + ",".join(map(str, uids)) + " (FLAGS)")
        self.assertTrue(r.ok(), r.line)
        self.assertEqual(r.blob.count(b" FETCH ("), 1500)       # only even UIDs up to 3000 exist
        self.assertLess(time.time() - t0, 3)
        r = c.cmd("UID FETCH 5:3,1,2:2,10:* (UID)")             # reversed, repeated, open-ended
        got = sorted(int(m) for m in re.findall(rb"UID (\d+)", r.blob))
        self.assertEqual(got, [1, 2, 3, 4, 5] + list(range(10, 3001)))


class LoginThrottleTests(Base):
    """Per-IP blocking after repeated failed logins. Each failed login takes about a second."""

    @classmethod
    def setUpClass(cls):
        tmp = cls.make_tmp()
        cls.repo = os.path.join(tmp, "me")
        build_repo(cls.repo, basic_messages())

    def srv_with(self, **auth):
        return self.start({"me": (self.repo, PASSWORD, {})}, auth_opts=auth,
                          logging_opts={"stats_interval": 0})

    def fail(self, c, n=1):
        for _ in range(n):
            self.assertEqual(c.cmd("LOGIN me wrong").status, "NO")

    def test_ip_is_blocked_after_the_limit_across_connections(self):
        srv = self.srv_with(login_fail_limit=3, login_fail_window=60, login_block_seconds=60)
        a = self.client(srv, login=None)
        self.fail(a, 2)
        b = self.client(srv, login=None)                  # a second connection counts too
        r = b.cmd("LOGIN me wrong")
        self.assertIn(b"* BYE", r.blob)                    # third failure: the IP is now blocked
        c = Client(srv.port)                               # new connection from the same IP
        self.addCleanup(c.close)
        t0 = time.time()
        self.assertIn(b"BYE Too many failed logins", c.greeting)
        self.assertEqual(c.read_eof(5), b"")
        self.assertLess(time.time() - t0, 1)
        self.assertIn("blocking 127.0.0.1 for 60 s after 3 failed logins within 60 s", srv.log())

    def test_a_connection_already_open_cannot_log_in_once_blocked(self):
        srv = self.srv_with(login_fail_limit=2, login_fail_window=60, login_block_seconds=60)
        open_early = self.client(srv, login=None)
        a = self.client(srv, login=None)
        self.fail(a, 1)
        b = self.client(srv, login=None)
        self.assertIn(b"* BYE", b.cmd("LOGIN me wrong").blob)   # second failure blocks the IP
        t0 = time.time()
        r = open_early.cmd(f'LOGIN me "{PASSWORD}"')            # even the right password: refused
        self.assertEqual(r.status, "NO")
        self.assertIn(b"BYE", r.blob)
        self.assertLess(time.time() - t0, 1)                    # and without any hashing delay

    def test_block_expires(self):
        srv = self.srv_with(login_fail_limit=2, login_fail_window=60, login_block_seconds=2)
        a = self.client(srv, login=None)
        self.fail(a, 2)
        self.assertIn(b"BYE", Client(srv.port).greeting)
        time.sleep(2.2)
        c = self.client(srv)                                    # logs in normally again
        self.assertTrue(c.cmd("NOOP").ok())
        self.assertIn("login block on 127.0.0.1 has ended", srv.log())

    def test_success_resets_the_count(self):
        srv = self.srv_with(login_fail_limit=3, login_fail_window=60, login_block_seconds=60)
        a = self.client(srv, login=None)
        self.fail(a, 2)
        a.login("me", PASSWORD)                                 # success clears the two failures
        b = self.client(srv, login=None)
        self.fail(b, 2)                                         # would be the 4th without the reset
        self.assertTrue(self.client(srv).cmd("NOOP").ok())      # still not blocked

    def test_exempt_network_is_never_blocked(self):
        srv = self.srv_with(login_fail_limit=2, login_fail_window=60, login_block_seconds=60,
                            no_throttle_networks="127.0.0.0/8")
        a = self.client(srv, login=None)
        self.fail(a, 2)
        b = self.client(srv, login=None)
        self.fail(b, 1)
        self.assertTrue(self.client(srv).cmd("NOOP").ok())

    def test_limit_zero_switches_throttling_off(self):
        srv = self.srv_with(login_fail_limit=0)
        a = self.client(srv, login=None)
        self.fail(a, 2)
        b = self.client(srv, login=None)
        self.fail(b, 2)
        self.assertTrue(self.client(srv).cmd("NOOP").ok())

    def test_failures_outside_the_window_do_not_count(self):
        srv = self.srv_with(login_fail_limit=2, login_fail_window=1, login_block_seconds=60)
        a = self.client(srv, login=None)
        self.fail(a, 1)
        time.sleep(1.5)
        b = self.client(srv, login=None)
        self.fail(b, 1)                                         # the first one has aged out
        self.assertTrue(self.client(srv).cmd("NOOP").ok())

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "needs SIGUSR1")
    def test_throttled_connections_are_counted(self):
        srv = self.srv_with(login_fail_limit=2, login_fail_window=60, login_block_seconds=60)
        a = self.client(srv, login=None)
        self.fail(a, 1)
        b = self.client(srv, login=None)
        b.cmd("LOGIN me wrong")
        Client(srv.port).close()
        srv.proc.send_signal(signal.SIGUSR1)
        self.assertTrue(wait_for(lambda: stats_lines(srv)))
        line = stats_lines(srv)[-1]
        st = parse_stats(line[line.index("stats:"):])
        self.assertEqual(st["rejected"], {"throttled": 1})
        self.assertEqual(st["logins_failed"], "2")
        self.assertNotIn(PASSWORD, srv.log())


class CheckConfigTests(Base):
    """--check-config, and the checks that run at startup."""

    @classmethod
    def setUpClass(cls):
        cls.make_tmp()

    def setup_repo(self, name="me", **kw):
        path = os.path.join(self.tmp, f"{name}-{time.time_ns()}")
        build_repo(path, basic_messages(), **kw)
        return path

    def conf(self, accounts=None, server_opts=None, **kw):
        accounts = accounts or {"me": (self.setup_repo(), PASSWORD, {})}
        return ServerProc(accounts, server_opts, tmp=tempfile.mkdtemp(dir=self.tmp), **kw)

    def check(self, srv, *flags):
        r = srv.run("--check-config", *flags)
        return r.returncode, r.stdout

    def test_good_configuration(self):
        rc, out = self.check(self.conf())
        self.assertEqual(rc, 0, out)
        self.assertIn("OK    account me: gmail-local-repository, 4 folder(s), 5 message(s)", out)
        self.assertIn("password file has a valid hash for all 1 account(s)", out)
        self.assertIn("0 FAIL", out)
        self.assertNotIn("FAIL  ", out)

    def test_two_repository_types_side_by_side(self):
        imap = os.path.join(self.tmp, "imap-repo")
        build_imap_importer_repo(imap, [dict(raw=simple_message(1), folders=["Inbox"])])
        srv = self.conf({"me": (self.setup_repo(), PASSWORD, {}), "yahoo": (imap, PASSWORD, {})})
        rc, out = self.check(srv)
        self.assertEqual(rc, 0, out)
        self.assertIn("account yahoo: imap-local-repository, 1 folder(s), 1 message(s)", out)

    def test_nothing_is_started_or_written(self):
        srv = self.conf()
        repo = srv.ini and os.path.dirname(srv.ini)
        before = sorted(os.listdir(repo))
        self.check(srv, "--check-bind")
        self.assertEqual(sorted(os.listdir(repo)), before)

    def test_missing_repository_fails(self):
        srv = self.conf({"me": (os.path.join(self.tmp, "no-such-repo"), PASSWORD, {})})
        rc, out = self.check(srv)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  account me:", out)

    def test_unsupported_repository_fails(self):
        repo = self.setup_repo()
        with open(os.path.join(repo, "repository.json"), "w") as f:
            f.write('{"application": "something-else", "schema_version": 1}')
        rc, out = self.check(self.conf({"me": (repo, PASSWORD, {})}))
        self.assertEqual(rc, 1)
        self.assertIn("unrecognised repository application", out)

    def test_database_missing_something_the_server_reads_fails(self):
        repo = self.setup_repo()
        db = sqlite3.connect(os.path.join(repo, "repository.db"))
        db.execute("ALTER TABLE folder_messages RENAME TO folder_messages_old")
        db.execute("ALTER TABLE messages RENAME COLUMN is_starred TO starred")
        db.commit()
        db.close()
        rc, out = self.check(self.conf({"me": (repo, PASSWORD, {})}))
        self.assertEqual(rc, 1)
        self.assertIn("folder_messages", out)
        self.assertIn("messages.is_starred", out)

    def test_password_problems(self):
        srv = self.conf({"me": (self.setup_repo(), PASSWORD, {}),
                         "other": (self.setup_repo(), PASSWORD, {})})
        with open(srv.pw, encoding="utf-8") as f:
            lines = [l for l in f if not l.startswith("other:")]
        with open(srv.pw, "w", encoding="utf-8") as f:
            f.writelines(lines)
        rc, out = self.check(srv)
        self.assertEqual(rc, 1)
        self.assertIn("no password set for: other", out)
        os.remove(srv.pw)
        rc, out = self.check(srv)
        self.assertEqual(rc, 1)
        self.assertIn("password file not found", out)

    def test_warnings_pass_unless_strict(self):
        srv = self.conf({"me": (self.setup_repo(), PASSWORD, {"hide_folders": "No*Such"})})
        os.chmod(srv.pw, 0o644)
        rc, out = self.check(srv)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN  account me: hide_folders pattern 'No*Such' matches no folder", out)
        self.assertIn("is readable by other users", out)
        rc, out = self.check(srv, "--strict")
        self.assertEqual(rc, 1)

    def test_plaintext_on_the_network_is_a_warning_loopback_is_not(self):
        rc, out = self.check(self.conf(server_opts={"host": "0.0.0.0"}))
        self.assertIn("WARN  plaintext listener on 0.0.0.0:", out)
        rc, out = self.check(self.conf())
        self.assertIn("OK    plaintext listener on 127.0.0.1:", out)

    def test_unreadable_configuration_exits_with_2(self):
        srv = self.conf()
        with open(srv.ini, "a", encoding="utf-8") as f:
            f.write("\n[no_such_section]\nbogus_option = 1\n")
        r = srv.run("--check-config")
        self.assertIn("unknown section [no_such_section]", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, SERVER_PY, "--config", "/no/such.ini", "--check-config"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("config file not found", r.stdout)

    def test_flags_without_check_config_are_rejected(self):
        r = self.conf().run("--strict")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only apply with --check-config", r.stderr)

    def test_check_bind_reports_a_port_in_use(self):
        srv = self.conf()
        self.assertEqual(self.check(srv, "--check-bind")[0], 0)      # free: fine
        srv.start()
        self.addCleanup(srv.stop)
        rc, out = self.check(srv, "--check-bind")                    # now the server holds it
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  cannot listen on", out)

    def _make_cert(self, name, days=365):
        cert, key = os.path.join(self.tmp, f"{name}.pem"), os.path.join(self.tmp, f"{name}.key")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", cert, "-days", str(days), "-subj", "/CN=localhost"],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("openssl command not available")
        return cert, key

    def test_tls_certificate_checks(self):
        cert, key = self._make_cert("good")
        other_cert, other_key = self._make_cert("other")
        srv = self.conf(tls_opts={"cert": cert, "key": key})
        rc, out = self.check(srv)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"OK    TLS certificate and key load; certificate valid until 20\d\d-")
        srv = self.conf(tls_opts={"cert": cert, "key": other_key})          # key of another cert
        rc, out = self.check(srv)
        self.assertEqual(rc, 1)
        self.assertIn("TLS certificate and key cannot be loaded", out)
        srv = self.conf(tls_opts={"cert": os.path.join(self.tmp, "gone.pem"), "key": key})
        rc, out = self.check(srv)
        self.assertEqual(rc, 1)
        self.assertIn("TLS certificate file not found", out)

    def test_certificate_expiry_warning_and_expired_message(self):
        cert, key = self._make_cert("soon", days=10)
        srv = self.conf(tls_opts={"cert": cert, "key": key})
        rc, out = self.check(srv)
        self.assertEqual(rc, 0)                                             # a warning, not a failure
        self.assertRegex(out, r"WARN  TLS certificate expires in \d+ days")
        import importlib.util
        spec = importlib.util.spec_from_file_location("imapd_for_check", SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = mod.load_config(srv.ini)
        from datetime import datetime, timedelta, timezone
        later = datetime.now(timezone.utc) + timedelta(days=400)
        self.assertTrue(any("EXPIRED" in t for lvl, t in mod.check_tls(cfg, now=later) if lvl == "WARN"))

    def test_startup_refuses_a_repository_the_server_cannot_read(self):
        repo = self.setup_repo()
        db = sqlite3.connect(os.path.join(repo, "repository.db"))
        db.execute("ALTER TABLE folder_messages RENAME TO folder_messages_old")
        db.commit()
        db.close()
        srv = self.conf({"me": (repo, PASSWORD, {})})
        r = subprocess.run([sys.executable, SERVER_PY, "--config", srv.ini], capture_output=True,
                           text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("startup check failed", r.stderr)
        self.assertIn("folder_messages", r.stderr)

    def test_startup_logs_warnings_and_still_starts(self):
        srv = self.conf({"me": (self.setup_repo(), PASSWORD, {"hide_folders": "No*Such"})})
        srv.start()
        self.addCleanup(srv.stop)
        self.assertIn("startup check: account me: hide_folders pattern 'No*Such' matches no folder",
                      srv.log())
        self.assertTrue(Client(srv.port).greeting.startswith(b"* OK"))


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

    def test_python_version_check(self):
        too_old = self.mod.python_too_old
        self.assertIsNone(too_old((3, 11, 0)))
        self.assertIsNone(too_old((3, 12, 7, "final", 0)))
        self.assertIsNone(too_old((4, 0)))
        for old in ((3, 10, 14), (3, 9), (2, 7, 18)):
            msg = too_old(old)
            self.assertIn("Python 3.11", msg)
            self.assertIn(f"this is Python {old[0]}.{old[1]}", msg)
        self.assertEqual(self.mod.MIN_PYTHON, (3, 11))

    def test_server_refuses_to_start_on_an_old_python(self):
        # Fake the interpreter version before the module's first statement runs.
        code = ("import sys, runpy; sys.version_info = (3, 10, 14, 'final', 0); "
                f"runpy.run_path({SERVER_PY!r}, run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs Python 3.11 or later (this is Python 3.10)", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_parse_set_normalises_and_in_set_agrees_with_a_naive_scan(self):
        m = self.mod
        self.assertEqual(m.parse_set("5:3,1,2:2,10:*,9", 20), [(1, 5), (9, 20)])   # adjacent ranges merge
        self.assertEqual(m.parse_set("*", 7), [(7, 7)])
        rnd = random.Random(7)
        for _ in range(300):
            spec = ",".join(rnd.choice([str(rnd.randint(1, 60)), f"{rnd.randint(1, 60)}:{rnd.randint(1, 60)}", "*"])
                            for _ in range(rnd.randint(1, 12)))
            ranges = m.parse_set(spec, 50)
            naive = []
            for part in spec.split(","):
                a, _, b = part.partition(":")
                lo = 50 if a == "*" else int(a)
                hi = (50 if b == "*" else int(b)) if b else lo
                naive.append((min(lo, hi), max(lo, hi)))
            for v in range(0, 70):
                self.assertEqual(m.in_set(v, ranges), any(lo <= v <= hi for lo, hi in naive), (spec, v))

    def test_a_huge_message_set_is_cheap(self):
        m = self.mod
        spec = ",".join(str(i) for i in range(2, 60000, 2))          # 30,000 separate elements
        ranges = m.parse_set(spec, 100000)
        t0 = time.time()
        hits = sum(1 for v in range(1, 100001) if m.in_set(v, ranges))   # 100,000 messages
        self.assertEqual(hits, 29999)
        self.assertLess(time.time() - t0, 2)

    def test_tokenize_limits(self):
        m = self.mod
        t, depth = m.tokenize("(" * 100 + "x" + ")" * 100), 0
        while isinstance(t[0], list):
            t, depth = t[0], depth + 1
        self.assertEqual((depth, t), (100, ["x"]))
        with self.assertRaises(m.Err):
            m.tokenize("(" * 101 + "x" + ")" * 101)
        self.assertEqual(len(m.tokenize(" ".join(["a"] * 10000))), 10000)
        with self.assertRaises(m.Err):
            m.tokenize(" ".join(["a"] * 10001))
        self.assertEqual(len(m.tokenize("(a) " * 4000)), 4000)         # parentheses are not items
        self.assertEqual(m.tokenize('1 (A "b c" BODY[HEADER.FIELDS (X Y)])'),
                         ["1", ["A", "b c", "BODY[HEADER.FIELDS (X Y)]"]])

    def test_stat_name_only_uses_known_commands(self):
        n = self.mod.stat_name
        self.assertEqual(n("noop"), "NOOP")
        self.assertEqual(n("uid", ["fetch", "1"]), "UID FETCH")
        self.assertEqual(n("uid", ["bogus"]), "UID")
        self.assertEqual(n("xyzzy"), "(unknown)")
        self.assertEqual(n("__init__"), "(unknown)")

    def test_stats_line_format(self):
        st = self.mod.Stats(clock=lambda: 100.0)
        st.started = 40.0
        st.connections, st.logins_ok, st.bytes_sent = 5, 3, 1234
        st.commands.update({"UID FETCH": 2, "NOOP": 7})
        st.errors["internal"] += 1
        self.assertEqual(st.line(2),
                         "stats: uptime=60s connections=5 open=2 rejected=- logins_ok=3 logins_failed=0 "
                         "logins_throttled=0 commands=NOOP:7,UID_FETCH:2 slow_commands=0 bytes_sent=1234 "
                         "errors=internal:1")

    def test_throttle_logic_with_a_fake_clock(self):
        import ipaddress
        now = [1000.0]
        T = lambda **kw: self.mod.LoginThrottle(**{"limit": 3, "window": 60, "block_seconds": 100, **kw},
                                                clock=lambda: now[0])
        t = T()
        self.assertEqual(t.blocked_for("10.0.0.1"), 0)
        self.assertFalse(t.record_failure("10.0.0.1"))
        now[0] += 10
        self.assertFalse(t.record_failure("10.0.0.1"))
        self.assertTrue(t.record_failure("10.0.0.1"))                      # third within 60 s
        self.assertAlmostEqual(t.blocked_for("10.0.0.1"), 100, delta=1)
        self.assertEqual(t.blocked_for("10.0.0.2"), 0)                      # other IPs unaffected
        now[0] += 101
        self.assertEqual(t.blocked_for("10.0.0.1"), 0)                      # block ended
        now[0] += 1000
        t.record_failure("10.0.0.3"); now[0] += 61; t.record_failure("10.0.0.3")
        self.assertFalse(t.record_failure("10.0.0.3"))                      # first one aged out of the window
        t.record_success("10.0.0.3")
        self.assertFalse(t.record_failure("10.0.0.3"))                      # success cleared the count
        ex = T(exempt=[ipaddress.ip_network("192.168.0.0/16")])
        for _ in range(10):
            self.assertFalse(ex.record_failure("192.168.1.5"))
        self.assertEqual(ex.blocked_for("192.168.1.5"), 0)
        off = T(limit=0)
        for _ in range(10):
            self.assertFalse(off.record_failure("10.9.9.9"))
        self.assertEqual(off.blocked_for("10.9.9.9"), 0)
        m = T()                                                             # IPv4-mapped IPv6 is the same address
        m.record_failure("::ffff:10.1.1.1"); m.record_failure("10.1.1.1")
        self.assertTrue(m.record_failure("::ffff:10.1.1.1"))
        self.assertGreater(m.blocked_for("10.1.1.1"), 0)
        self.assertEqual(m.blocked_for("not an ip"), 0)                     # garbage is never blocked

    def test_throttle_memory_is_bounded(self):
        t = self.mod.LoginThrottle(limit=5, window=60, block_seconds=60)
        t.MAX_TRACKED = 100
        for i in range(1000):
            t.record_failure(f"10.{i // 250}.{i % 250}.1")
        self.assertLessEqual(len(t.failures), 100)
        blocker = self.mod.LoginThrottle(limit=1, window=60, block_seconds=60)
        blocker.MAX_TRACKED = 50
        for i in range(500):
            blocker.record_failure(f"10.1.{i // 250}.{i % 250}")
        self.assertLessEqual(len(blocker.blocked), 50)

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
