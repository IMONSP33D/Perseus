"""Code runner diagnostics: key fingerprints, sandbox lifecycle logging, identity, and the doctor."""

import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from perseus.sandbox.backends import ExecResult, SandboxError, fingerprint
from perseus.sandbox.backends.fake import FakeBackend
from perseus.sandbox.broker import Broker, describe_event
from perseus.sandbox.runner import RunSettings, probe, run_program

KEY = "e2b_0123456789abcdef0123456789abcdef3f9a"


class Fingerprint(unittest.TestCase):
    def test_shows_prefix_and_last_four_only(self):
        self.assertEqual(fingerprint(KEY), "e2b_…3f9a")
        self.assertNotIn("0123456789", fingerprint(KEY))
        self.assertEqual(fingerprint("short"), "(too short to show)")


class Lifecycle(unittest.TestCase):
    def test_every_step_is_reported_with_the_sandbox_id(self):
        events = []
        b = FakeBackend().on(lambda c: c.startswith("python3 main.py"), ExecResult(1, "", "boom"))
        r = run_program(b, "python", "SECRET_CODE()", settings=RunSettings(network="none"), on_event=events.append)
        self.assertEqual([e["event"] for e in events], ["open", "exec", "close"])
        self.assertTrue(all(e["sandbox"] == "fake-1" for e in events))
        self.assertEqual((events[1]["exit"], events[0]["purpose"]), (1, "run python"))
        self.assertEqual((r["sandbox_id"], r["backend"]), ("fake-1", "fake"))
        self.assertNotIn("SECRET_CODE", repr(events))                    # the code itself is never logged
        self.assertNotIn("boom", repr(events))                           # nor its output

    def test_failed_create_is_reported(self):
        events = []
        b = FakeBackend()
        b.fail_open = SandboxError("auth", "E2B rejected the API key")
        with self.assertRaises(SandboxError):
            run_program(b, "python", "x", on_event=events.append)
        self.assertEqual((events[0]["event"], events[0]["code"]), ("open_failed", "auth"))

    def test_probe_reports_its_sandbox(self):
        events = []
        p = probe(FakeBackend(), RunSettings(network="none"), on_event=events.append)
        self.assertEqual(p["probe_sandbox"], "fake-1")
        self.assertEqual(events[0]["purpose"], "start-up probe")

    def test_log_lines_are_readable(self):
        self.assertEqual(describe_event({"event": "open", "sandbox": "i7x", "ms": 812, "purpose": "run python",
                                         "network": "packages"}),
                         "sandbox i7x: created in 812 ms (run python, network packages)")
        self.assertIn("-> exit 1 in 40 ms", describe_event({"event": "exec", "sandbox": "i7x", "cmd": "python3 main.py",
                                                             "exit": 1, "timed_out": False, "ms": 40}))
        self.assertIn("destroy FAILED", describe_event({"event": "close", "sandbox": "i7x", "killed": False,
                                                        "lived_ms": 2100}))
        self.assertIn("create FAILED", describe_event({"event": "open_failed", "purpose": "run python", "ms": 5,
                                                       "code": "auth", "error": "bad key", "network": "none"}))


class BrokerIdentity(unittest.TestCase):
    def test_identity_logged_at_start_and_in_info(self):
        lines = []
        b = FakeBackend()
        b.describe = lambda: {"provider": "e2b", "key": "e2b_…3f9a", "api_url": "https://api.e2b.app",
                              "env_overrides": ["E2B_DOMAIN"]}
        broker = Broker("fake", {"network": "none"}, "t" * 32, backend=b)
        with mock.patch("perseus.sandbox.broker.log", lines.append):
            broker.start()
        self.assertTrue(any(l.startswith("identity: provider=e2b · key=e2b_…3f9a") for l in lines), lines)
        self.assertTrue(any("WARNING" in l and "E2B_DOMAIN" in l for l in lines))
        self.assertTrue(any(l.startswith("sandbox fake-1: created") for l in lines))
        self.assertEqual(broker.info()["identity"]["key"], "e2b_…3f9a")

    def test_connect_shows_who_on_the_start_up_line(self):
        from perseus.sandbox import launch
        from perseus.tools import Registry
        client = mock.Mock()
        client.info.return_value = {"backend": "e2b", "languages": ["python"], "network": "none", "network_blocked": True,
                                    "identity": {"key": "e2b_…3f9a", "key_source": "perseus.toml",
                                                 "api_url": "https://api.e2b.app", "env_overrides": []}}
        ok, msg = launch.connect(client, Registry())
        self.assertTrue(ok)
        self.assertIn("e2b (key e2b_…3f9a from perseus.toml, https://api.e2b.app)", msg)


class DoctorWithStubSDK(unittest.TestCase):
    def setUp(self):
        created = self.created = []

        class Info:
            template_id, state, started_at = "base", types.SimpleNamespace(value="running"), datetime.now(timezone.utc)

        class StubSandbox:
            def __init__(self, sid):
                self.sandbox_id, self.killed = sid, False
                self.files = types.SimpleNamespace(write=lambda p, d: None)
                self.commands = types.SimpleNamespace(run=self._run)

            @classmethod
            def create(cls, **kw):
                sbx = cls(f"isbx{len(created) + 1}")
                created.append(sbx)
                return sbx

            @staticmethod
            def list(api_key=None, limit=None):
                return types.SimpleNamespace(next_items=lambda: [types.SimpleNamespace(sandbox_id=s.sandbox_id)
                                                                 for s in created if not s.killed])

            def get_info(self):
                return Info()

            def _run(self, cmd, cwd=None, timeout=None, on_stdout=None, on_stderr=None):
                out = "" if "mkdir" in cmd else ("pwd=/home/user/job\nhostname=e2b.local\nuser=user\n"
                                                 "kernel=Linux 6.1\nos=Debian\n")
                return types.SimpleNamespace(exit_code=0, stdout=out, stderr="")

            def kill(self):
                self.killed = True
                return True

        stub = types.ModuleType("e2b")
        stub.Sandbox, stub.CommandExitException = StubSandbox, type("CommandExitException", (Exception,), {})
        conn = types.ModuleType("e2b.connection_config")
        conn.ConnectionConfig = lambda api_key=None: types.SimpleNamespace(api_url="https://api.e2b.app", domain="e2b.app")
        self.patch = mock.patch.dict(sys.modules, {"e2b": stub, "e2b.connection_config": conn})
        self.patch.start()
        self.toml = Path(tempfile.mkdtemp()) / "Config"
        self.toml.mkdir()
        (self.toml / "tooling.toml").write_text(f'[code_runner]\napi_key = "{KEY}"\nnetwork = "none"\n')

    def tearDown(self):
        self.patch.stop()

    def run_doctor(self, environ=None):
        from perseus.sandbox import doctor
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.dict("os.environ", environ or {}, clear=True):
            code = doctor.main(["--config", str(self.toml)])
        return code, buf.getvalue()

    def test_full_check_prints_where_code_runs(self):
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("key e2b_…3f9a from tooling.toml [code_runner] api_key", out)
        self.assertIn("E2B created sandbox isbx1", out)
        self.assertIn("listed among this key's team's", out)
        self.assertIn("pwd /home/user/job", out)
        self.assertIn("destroyed", out)
        self.assertNotIn(KEY, out)                                       # never the key itself
        self.assertTrue(self.created[0].killed)

    def test_flags_a_different_key_and_sdk_redirects_in_the_shell(self):
        code, out = self.run_doctor({"E2B_API_KEY": "e2b_" + "9" * 36 + "beef", "E2B_DOMAIN": "elsewhere.dev"})
        self.assertIn("DIFFERENT key (e2b_…beef)", out)
        self.assertIn("E2B_DOMAIN=elsewhere.dev", out)


if __name__ == "__main__":
    unittest.main()
