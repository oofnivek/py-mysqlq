"""Tests for mysqlq. Run with: python3 -m unittest -v"""
import importlib.machinery
import importlib.util
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mysqlq")

# mysqlq has no .py extension, so load it explicitly
_loader = importlib.machinery.SourceFileLoader("mysqlq", SCRIPT)
mysqlq = importlib.util.module_from_spec(importlib.util.spec_from_loader("mysqlq", _loader))
_loader.exec_module(mysqlq)

CREDENTIALS = """\
[test]
host = db.example.com
port = 3307
user = reader
password = s3cr"et\\pw
database = app
"""

# Stands in for the mysql client: records its arguments, the defaults file it was handed, and stdin
FAKE_MYSQL = f"""#!{sys.executable}
import json, os, sys
cnf_path = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--defaults-extra-file="))
with open(cnf_path) as f:
    cnf = f.read()
with open(os.environ["FAKE_MYSQL_OUT"], "w") as out:
    json.dump({{"argv": sys.argv[1:], "cnf": cnf, "stdin": sys.stdin.read()}}, out)
"""


class CheckReadOnlyTest(unittest.TestCase):
    ALLOWED = [
        "SELECT 1",
        "select * from t where note = 'drop table t; delete'",
        "SELECT `update`, created_at FROM t",
        "SELECT 1 -- delete everything\n",
        "SELECT 1 # update\n",
        "SELECT 1; SELECT 2;",
        "(SELECT 1)",
        "SHOW TABLES",
        "DESC t",
        "DESCRIBE t",
        "EXPLAIN SELECT * FROM t",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ]
    REJECTED = [
        "",
        "  ;  ",
        "DELETE FROM t",
        "SET autocommit = 0",
        "SELECT 1; DROP TABLE t",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "EXPLAIN ANALYZE UPDATE t SET a = 1",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t LOCK IN SHARE MODE",
        "SELECT * FROM t INTO OUTFILE '/tmp/x'",
        "SELECT 1 INTO DUMPFILE '/tmp/x'",
        "SELECT /*! 1; DROP TABLE t */",
        "SELECT /*+ MAX_EXECUTION_TIME(1) */ 1",
        "SELECT 1 \\! id",
    ]

    def test_allowed(self):
        for sql in self.ALLOWED:
            with self.subTest(sql=sql):
                mysqlq.check_read_only(sql)

    def test_rejected(self):
        for sql in self.REJECTED:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                mysqlq.check_read_only(sql)


class LoadProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "credentials")
        with open(self.path, "w") as f:
            f.write(CREDENTIALS + "[incomplete]\nhost = x\n")
        os.chmod(self.path, 0o600)
        patcher = mock.patch.dict(os.environ, {"MYSQLQ_CREDENTIALS": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_profile(self):
        profile = mysqlq.load_profile("test")
        self.assertEqual(profile["host"], "db.example.com")
        self.assertEqual(profile["password"], 's3cr"et\\pw')

    def test_rejects_group_or_world_access(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            os.chmod(self.path, mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(SystemExit, "chmod 600"):
                mysqlq.load_profile("test")

    def test_missing_file(self):
        os.remove(self.path)
        with self.assertRaisesRegex(SystemExit, "not found"):
            mysqlq.load_profile("test")

    def test_unknown_profile(self):
        with self.assertRaisesRegex(SystemExit, "profile 'nope' not found"):
            mysqlq.load_profile("nope")

    def test_missing_keys(self):
        with self.assertRaisesRegex(SystemExit, "missing user, password"):
            mysqlq.load_profile("incomplete")


def listen():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server


def serve_forever(server, handler):
    def loop():
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handler, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


def echo(conn):
    with conn:
        while data := conn.recv(65536):
            conn.sendall(data)


def fake_socks5(conn):
    """Minimal no-auth SOCKS5 server that only handles CONNECT to a domain name."""
    assert mysqlq.recv_exact(conn, 3) == b"\x05\x01\x00"
    conn.sendall(b"\x05\x00")
    assert mysqlq.recv_exact(conn, 4) == b"\x05\x01\x00\x03"
    host = mysqlq.recv_exact(conn, mysqlq.recv_exact(conn, 1)[0]).decode()
    port = struct.unpack(">H", mysqlq.recv_exact(conn, 2))[0]
    upstream = socket.create_connection((host, port))
    conn.sendall(b"\x05\x00\x00\x01" + bytes(4) + bytes(2))
    threading.Thread(target=mysqlq.pipe, args=(conn, upstream), daemon=True).start()
    mysqlq.pipe(upstream, conn)


class ForwarderTest(unittest.TestCase):
    def setUp(self):
        self.echo = listen()
        self.proxy = listen()
        self.addCleanup(self.echo.close)
        self.addCleanup(self.proxy.close)
        serve_forever(self.echo, echo)
        serve_forever(self.proxy, fake_socks5)

    def test_tunnels_first_connection_then_stops_listening(self):
        port = mysqlq.start_forwarder(self.proxy.getsockname(), "127.0.0.1", self.echo.getsockname()[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(b"ping")
            self.assertEqual(mysqlq.recv_exact(conn, 4), b"ping")
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=5)


class MainTest(unittest.TestCase):
    """Runs the script end to end against a fake mysql client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.mkdir(bin_dir)
        fake = os.path.join(bin_dir, "mysql")
        with open(fake, "w") as f:
            f.write(FAKE_MYSQL)
        os.chmod(fake, 0o755)
        creds = os.path.join(self.tmp.name, "credentials")
        with open(creds, "w") as f:
            f.write(CREDENTIALS)
        os.chmod(creds, 0o600)
        self.out = os.path.join(self.tmp.name, "out.json")
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], MYSQLQ_CREDENTIALS=creds,
                        FAKE_MYSQL_OUT=self.out)
        self.env.pop("MYSQLQ_PROFILE", None)

    def run_mysqlq(self, *args, **kwargs):
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        return subprocess.run([sys.executable, SCRIPT, *args], env=self.env, capture_output=True, text=True,
                              **kwargs)

    def recorded(self):
        with open(self.out) as f:
            return json.load(f)

    def init_command(self, argv):
        return next(a.split("=", 1)[1] for a in argv if a.startswith("--init-command="))

    def test_runs_query_with_credentials_via_pipe(self):
        result = self.run_mysqlq("--profile", "test", "SELECT 1")
        self.assertEqual(result.returncode, 0, result.stderr)
        rec = self.recorded()
        self.assertEqual(rec["stdin"], "SELECT 1")
        self.assertRegex(rec["argv"][0], r"^--defaults-extra-file=/dev/fd/\d+$")
        self.assertNotIn("s3cr", " ".join(rec["argv"]))
        self.assertIn('password="s3cr\\"et\\\\pw"', rec["cnf"])
        self.assertIn("host=db.example.com\nport=3307", rec["cnf"])
        self.assertIn("--skip-reconnect", rec["argv"])
        self.assertEqual(rec["argv"][-1], "app")

    def test_default_limits(self):
        self.run_mysqlq("--profile", "test", "SELECT 1")
        init = self.init_command(self.recorded()["argv"])
        self.assertIn("transaction_read_only = ON", init)
        self.assertIn("max_execution_time = 30000", init)
        self.assertIn("sql_select_limit = 1000", init)

    def test_custom_and_unlimited_limits(self):
        self.run_mysqlq("--profile", "test", "--timeout", "5", "--max-rows", "0", "SELECT 1")
        init = self.init_command(self.recorded()["argv"])
        self.assertIn("max_execution_time = 5000", init)
        self.assertIn(f"sql_select_limit = {mysqlq.UNLIMITED_ROWS}", init)

    def test_negative_limit_is_an_error(self):
        result = self.run_mysqlq("--profile", "test", "--max-rows", "-1", "SELECT 1")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(self.out))

    def test_reads_sql_from_piped_stdin(self):
        result = self.run_mysqlq("--profile", "test", stdin=None, input="SHOW TABLES")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded()["stdin"], "SHOW TABLES")

    def test_errors_instead_of_waiting_on_a_terminal(self):
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        try:
            result = self.run_mysqlq("--profile", "test", stdin=terminal, timeout=10)
        finally:
            os.close(terminal)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no SQL given", result.stderr)

    def test_rejected_sql_never_reaches_mysql(self):
        result = self.run_mysqlq("--profile", "test", "DROP TABLE t")
        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected", result.stderr)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
