"""Import from a fake IMAP server, then serve the repository with imap_archive_server.py and read it back."""
import os, sys, subprocess, tempfile, hashlib, imaplib, time, socket, re
from pathlib import Path
here = Path(__file__).parent
sys.path[:0] = [str(here), str(here.parent)]
import logging; logging.basicConfig(level=logging.ERROR)
import imap_local_repository as imp
from fake_imap_server import FakeImapServer
SERVER = Path(os.environ["ARCHIVE_SERVER_DIR"])

tmp = Path(tempfile.mkdtemp()); cert, key = tmp/"c.pem", tmp/"k.pem"
subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-keyout",key,"-out",cert,"-days","2",
  "-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost,IP:127.0.0.1"],check=True,capture_output=True)
def msg(n): return b"From: a@x\r\nSubject: m%d\r\nMessage-ID: <%d@x>\r\n\r\nbody %d\r\n" % (n,n,n)
D="17-Jul-2024 02:44:25 -0700"
fake = FakeImapServer(cert,key)
fake.folders = {"INBOX":{"uidvalidity":1,"flags":[],"msgs":{1:(r"\Seen",D,msg(1)),2:(r"\Flagged",D,msg(2))}},
                "Archive":{"uidvalidity":2,"flags":[],"msgs":{5:("",D,msg(1)),6:("",D,msg(3))}},
                "Entw&APw-rfe":{"uidvalidity":3,"flags":[],"msgs":{1:("",D,msg(4))}},
                "[Gmail]/Spam":{"uidvalidity":4,"flags":[],"msgs":{1:("",D,msg(5))}}}
fake.start()
os.environ.update(IMAP_HOST="localhost",IMAP_PORT=str(fake.port),IMAP_USERNAME="u",IMAP_PASSWORD="p",
                  IMAP_CA_FILE=str(cert),DB_LOCATION=str(tmp/"data"),IMAP_MIN_FREE_BYTES="0")
assert imp.main()==0
repo = tmp/"data"/"u"

s=socket.socket(); s.bind(("127.0.0.1",0)); port=s.getsockname()[1]; s.close()
ini = tmp/"srv.ini"
ini.write_text(f"[server]\nhost = 127.0.0.1\nport = {port}\n[auth]\npassword_file = {tmp/'pw'}\n"
               f"[account:me]\npath = {repo}\nhide_folders = [[]Gmail]*\n")
run=lambda *a,**k: subprocess.run([sys.executable,str(SERVER/"imap_archive_server.py"),"--config",str(ini),*a],capture_output=True,text=True,**k)
r=run("--set-password","me",input="secret\nsecret\n"); assert r.returncode==0,r.stderr
r=run("--verify"); print(r.stdout.strip()); assert r.returncode==0 and "0 problem" in r.stdout
proc=subprocess.Popen([sys.executable,str(SERVER/"imap_archive_server.py"),"--config",str(ini)],stderr=subprocess.PIPE,text=True)
time.sleep(1.5)
def check(c,l):
    print("OK  " if c else "FAIL",l)
    if not c: proc.kill(); sys.exit(1)
try:
    m=imaplib.IMAP4("127.0.0.1",port); m.login("me","secret")
    names=[re.search(r'"/" (.*)$',x.decode()).group(1).strip('"') for x in m.list()[1]]
    print("folders:",names)
    check("INBOX" in names and "Archive" in names and "&APw-" in " ".join(names), "folders served (UTF-7 encoded)")
    check(not any("Spam" in n for n in names), "hide_folders pattern '[[]Gmail]*' hides the Spam folder")
    m.select("INBOX",readonly=True); t,d=m.uid("FETCH","1:*","(FLAGS)")
    check(b"\\Seen" in d[0] and b"\\Flagged" in d[1], "\\Seen / \\Flagged flags come from is_read / is_starred")
    t,d=m.uid("FETCH","1","(BODY.PEEK[])"); raw=d[0][1]
    check(raw==msg(1) and hashlib.sha256(raw).hexdigest()==hashlib.sha256(msg(1)).hexdigest(), "message bytes served exactly as stored")
    m.select("Archive",readonly=True); t,d=m.uid("SEARCH",None,"ALL")
    check(len(d[0].split())==2, "duplicate message appears in both folders (Archive holds 2)")
    t,d=m.uid("FETCH","1:*","(INTERNALDATE)"); check(b"17-Jul-2024 09:44:25 +0000" in d[0], "INTERNALDATE correct")
    m.logout()
finally:
    proc.terminate(); out=proc.communicate()[1]
print("server log excerpt:", [l for l in out.splitlines() if "account me" in l])
print("ALL PASSED")
