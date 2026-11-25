#!/usr/bin/env python3
"""Differential check: run the same commands against two versions of imap_archive_server.py
and report every difference in the replies.  Use it before and after any refactoring:

    python3 tests/compare_servers.py /path/to/old/imap_archive_server.py [/path/to/new.py]

The second argument defaults to ../imap_archive_server.py.  Exit status 0 = identical replies.
"""
import os
import shutil
import sys
import tempfile
import email.message

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                          # noqa: E402
from harness import Client, ServerProc                  # noqa: E402
from repo_factory import build_repo, multipart_message, simple_message   # noqa: E402

PW = "pw-for-compare"


def nested_message():
    inner = (b"From: inner@example.com\r\nSubject: inner message\r\nDate: Tue, 1 Jan 2019 10:00:00 +0000\r\n"
             b"Content-Type: text/plain\r\n\r\ninner body\r\n")
    return (b"From: outer@example.com\r\nTo: a@example.com, B <b@example.com>\r\nCc: c@example.com\r\n"
            b"Subject: =?utf-8?b?w5xiZXJzaWNodA==?=\r\nMessage-ID: <nest@example.com>\r\n"
            b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"OUT\"\r\n\r\n"
            b"--OUT\r\nContent-Type: multipart/alternative; boundary=\"IN\"\r\n\r\n"
            b"--IN\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"caf=C3=A9\r\n--IN\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>caf\xc3\xa9</p>\r\n--IN--\r\n"
            b"--OUT\r\nContent-Type: message/rfc822\r\n\r\n" + inner +
            b"--OUT\r\nContent-Type: image/png; name=\"x.png\"\r\nContent-Disposition: inline; filename*=utf-8''x%C3%A9.png\r\n"
            b"Content-Transfer-Encoding: base64\r\nContent-ID: <img1>\r\n\r\niVBORw0KGgo=\r\n--OUT--\r\n")


def messages():
    return [
        dict(raw=simple_message(1), folders=["INBOX", "Archive"], read=True),
        dict(raw=simple_message(2, "Quarterly report", "dave@example.com", "zebra"),
             folders=["INBOX"], starred=True, date="2024-08-01T00:00:00+00:00"),
        dict(raw=multipart_message(3), folders=["INBOX"], date="2023-01-15T10:00:00+00:00"),
        dict(raw=nested_message(), folders=["INBOX", "R\u00e9unions/2023"], date="1700000000000"),
        dict(raw=b"From: lf@example.com\nSubject: LF only\n\nno carriage returns\n",
             folders=["INBOX"], date="not a date"),
        dict(raw=b"Subject: no from, 8bit \xe9\r\n\r\nraw \xe9 byte\r\n", folders=["INBOX"]),
        dict(raw=b"", folders=["Empty-message"]),
    ]


COMMANDS = [
    'LIST "" "*"', 'LIST "" "%"', 'LIST "R*" "*"', 'LIST "" ""', 'LSUB "" "*"', "LIST", 'LIST "" "I*"',
    "STATUS INBOX (MESSAGES RECENT UIDNEXT UIDVALIDITY UNSEEN)", "STATUS Nope (MESSAGES)",
    "STATUS INBOX (BOGUS)", "SUBSCRIBE INBOX", "NOOP", "CHECK",
    "SELECT Nope", "FETCH 1 (FLAGS)", "SELECT INBOX",
    "FETCH 1:* (FLAGS)", "FETCH 1:* (UID FLAGS INTERNALDATE RFC822.SIZE)", "FETCH * (UID)",
    "FETCH 2:4 ALL", "FETCH 1:3 FAST", "FETCH 1:6 FULL", "FETCH 1:6 ENVELOPE", "FETCH 1:6 BODYSTRUCTURE",
    "FETCH 1:6 BODY", "UID FETCH 1:* (ENVELOPE BODYSTRUCTURE)", "FETCH 4 BODYSTRUCTURE",
    "FETCH 1:6 (RFC822.SIZE RFC822.HEADER)", "FETCH 1:6 RFC822", "FETCH 1:6 RFC822.TEXT",
    "FETCH 1:6 (BODY.PEEK[])", "FETCH 1:6 (BODY[])", "FETCH 1:6 (BODY.PEEK[HEADER])",
    "FETCH 1:6 (BODY.PEEK[TEXT])", "FETCH 1:6 (BODY.PEEK[HEADER.FIELDS (FROM TO subject)])",
    "FETCH 1:6 (BODY.PEEK[HEADER.FIELDS.NOT (Message-ID)])", "FETCH 3:4 (BODY.PEEK[1])",
    "FETCH 3:4 (BODY.PEEK[2])", "FETCH 3:4 (BODY.PEEK[1.MIME])", "FETCH 3:4 (BODY.PEEK[2.MIME])",
    "FETCH 4 (BODY.PEEK[1.1])", "FETCH 4 (BODY.PEEK[1.2])", "FETCH 4 (BODY.PEEK[2])",
    "FETCH 4 (BODY.PEEK[2.1])", "FETCH 4 (BODY.PEEK[3])", "FETCH 4 (BODY.PEEK[3.MIME])",
    "FETCH 4 (BODY.PEEK[9.9])", "FETCH 1:6 (BODY.PEEK[]<0.20>)", "FETCH 1:6 (BODY.PEEK[]<5>)",
    "FETCH 1:6 (BODY.PEEK[]<100000.20>)", "FETCH 1 (BODY.PEEK[HEADER]<3.10>)",
    "FETCH 1 (NOSUCHITEM)", "FETCH 99 (FLAGS)", "FETCH 1:2 (UID", "FETCH", "FETCH 1", "FETCH x (FLAGS)",
    "UID FETCH 2:3 (FLAGS)", "UID FETCH 1000:* (FLAGS)", "UID FETCH * (FLAGS)", "UID FETCH 1,3,5 (UID)",
    "UID BOGUS 1", "UID",
    "SEARCH ALL", "SEARCH SEEN", "SEARCH UNSEEN", "SEARCH FLAGGED UNFLAGGED", "SEARCH ANSWERED",
    "SEARCH DELETED", "SEARCH DRAFT", "SEARCH NEW", "SEARCH OLD", "SEARCH RECENT",
    "SEARCH FROM dave", "SEARCH FROM outer", "SEARCH TO b@example.com", "SEARCH CC c@", "SEARCH BCC x",
    "SEARCH SUBJECT report", "SEARCH SUBJECT \u00dcbersicht", "SEARCH HEADER Message-ID nest",
    "SEARCH BODY zebra", "SEARCH BODY caf", "SEARCH TEXT zebra", "SEARCH TEXT inner",
    "SEARCH SINCE 1-Jan-2024", "SEARCH BEFORE 1-Jan-2024", "SEARCH ON 17-Jul-2024",
    "SEARCH LARGER 600", "SEARCH SMALLER 300", "SEARCH UID 2:4", "SEARCH 1:3", "SEARCH *",
    "SEARCH NOT SEEN", "SEARCH OR SEEN FLAGGED", "SEARCH (SEEN) (FROM alice)", "SEARCH OR (FROM dave) (FROM outer)",
    "SEARCH CHARSET UTF-8 SUBJECT report", "SEARCH BADKEY", "SEARCH FROM", "SEARCH SINCE notadate",
    "UID SEARCH ALL", "UID SEARCH UNSEEN FROM alice", "UID SEARCH UID 3:*",
    "STORE 1 +FLAGS (\\Seen)", "UID STORE 1 +FLAGS (\\Seen)", "COPY 1 Archive", "UID COPY 1 Archive",
    "EXPUNGE", "UID EXPUNGE 1", "APPEND INBOX", "CREATE x", "DELETE x", "RENAME x y",
    'SELECT "R&AOk-unions/2023"', "FETCH 1:* (ENVELOPE BODYSTRUCTURE)",
    "EXAMINE Archive", "SEARCH ALL", "SELECT Empty-message", "FETCH 1:* (BODY[] ENVELOPE BODYSTRUCTURE)",
    "UNSELECT", "FETCH 1 (FLAGS)", "CLOSE", "EXAMINE INBOX", "CLOSE", "BOGUS", "LOGIN a b", "LOGOUT",
]


def run(server_py, repo):
    harness.SERVER_PY = server_py
    srv = ServerProc({"me": (repo, PW, {"hide_folders": "Hidden*"})},
                     tmp=tempfile.mkdtemp(prefix="cmp-")).start()
    out = []
    try:
        c = Client(srv.port)
        out.append(("greeting", c.greeting))
        c.login("me", PW)
        for text in COMMANDS:
            try:
                r = c.cmd(text)
                out.append((text, r.line, r.blob))
            except EOFError as e:
                out.append((text, "EOF", str(e)))
                break
        c.close()
    finally:
        srv.stop()
        shutil.rmtree(srv.tmp, ignore_errors=True)
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    old = os.path.abspath(sys.argv[1])
    new = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "imap_archive_server.py")
    tmp = tempfile.mkdtemp(prefix="cmp-repo-")
    try:
        repo = os.path.join(tmp, "repo")
        build_repo(repo, messages())
        a, b = run(old, repo), run(new, repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    diffs = 0
    for x, y in zip(a, b):
        if x != y:
            diffs += 1
            print(f"DIFFERENT: {x[0]}\n  old: {x[1:]!r:.300}\n  new: {y[1:]!r:.300}")
    if len(a) != len(b):
        diffs += 1
        print(f"different number of replies: {len(a)} vs {len(b)}")
    print(f"{len(a)} commands compared, {diffs} difference(s)")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
