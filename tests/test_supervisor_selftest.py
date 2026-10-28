import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from perseus.safety import socketguard
from perseus.safety.gateway import Gateway
from perseus.safety.netpolicy import Blocked
from perseus.safety.selftest import run
from perseus.supervisor import ProcessSpec, Supervisor
from perseus.tools import Registry, tool

# A child that crashes the first `crashes` times it starts, then stays up (counted in a file).
CHILD = r"""
import sys, time, pathlib
p = pathlib.Path(sys.argv[1]); n = int(p.read_text() or 0) + 1 if p.exists() else 1; p.write_text(str(n))
print(f"start {n}", flush=True)
if n <= int(sys.argv[2]):
    print(sys.argv[3], flush=True); sys.exit(3)
time.sleep(60)
"""


def spec(tmp: Path, crashes: int, max_restarts: int, msg: str = "boom"):
    counter = tmp / "count"
    script = tmp / "child.py"
    script.write_text(CHILD)
    up = lambda: counter.exists() and int(counter.read_text() or 0) > crashes
    return ProcessSpec("svc", [sys.executable, str(script), str(counter), str(crashes), msg], dict(os.environ),
                       tmp / "svc.log", up, ready_timeout_s=20, max_restarts=max_restarts)


class SupervisorTests(unittest.TestCase):
    def test_restarts_with_backoff_then_up(self):
        tmp = Path(tempfile.mkdtemp())
        states = []
        sup = Supervisor(on_state=lambda n, m: states.append(m.state), backoff=(0.1, 0.2, 0.3), poll_s=0.05)
        sup.add(spec(tmp, crashes=2, max_restarts=3))
        try:
            sup.start("svc")
            deadline = time.time() + 15
            while sup.procs["svc"].state != "up" and time.time() < deadline:
                sup.wait_ready("svc") if sup.procs["svc"].state == "starting" else time.sleep(0.05)
            m = sup.procs["svc"]
            self.assertEqual((m.state, m.restarts), ("up", 2))
            self.assertIn("restarting", states)
            self.assertEqual(sup.status()["svc"]["state"], "up")
        finally:
            sup.stop_all()
        self.assertEqual(sup.procs["svc"].state, "stopped")
        self.assertIsNotNone(sup.procs["svc"].proc.poll())  # really gone

    def test_gives_up_and_keeps_log_and_explains_oom(self):
        tmp = Path(tempfile.mkdtemp())
        sup = Supervisor(backoff=(0.05,), poll_s=0.05)
        sup.add(spec(tmp, crashes=99, max_restarts=1, msg="torch.OutOfMemoryError: CUDA out of memory"))
        try:
            sup.start("svc")
            deadline = time.time() + 15
            while sup.procs["svc"].state != "failed" and time.time() < deadline:
                time.sleep(0.05)
            m = sup.procs["svc"]
            self.assertEqual(m.state, "failed")
            self.assertEqual(m.restarts, 1)
            self.assertIn("ran out of GPU memory", m.reason)
            self.assertIn("CUDA out of memory", m.tail)
        finally:
            sup.stop_all()


class Restart(unittest.TestCase):
    def test_deliberate_restart_is_not_a_crash(self):
        tmp = Path(tempfile.mkdtemp())
        sup = Supervisor(backoff=(0.05,), poll_s=0.05)
        sup.add(spec(tmp, crashes=0, max_restarts=0))   # any crash would mean "failed"
        try:
            sup.start("svc")
            self.assertEqual(sup.wait_ready("svc").state, "up")
            first = sup.procs["svc"].proc
            sup.restart("svc")
            deadline = time.time() + 10
            while sup.procs["svc"].state != "up" and time.time() < deadline:
                time.sleep(0.05)
            m = sup.procs["svc"]
            self.assertEqual((m.state, m.restarts), ("up", 0))
            self.assertIsNot(m.proc, first)
            self.assertIsNotNone(first.poll())
        finally:
            sup.stop_all()


class SelfTest(unittest.TestCase):
    def setUp(self):
        socketguard.install(set())

    def tearDown(self):
        socketguard.uninstall()

    def test_passes_on_a_correct_gateway(self):
        reg = Registry()

        @tool(description="x", registry=reg, parameters={"type": "object", "properties": {}})
        def clean(ctx):
            return 1

        rep = run(Gateway(), {"vLLM": 41000, "SearXNG": 8888, "Perseus": 8080}, reg, reach_url=None)
        self.assertTrue(rep.passed, [f"{c.name}: {c.detail}" for c in rep.failures])
        names = [c.name for c in rep.cases]
        for must in ("refuse cloud metadata", "refuse 127.0.0.1 on the vLLM port", "refuse DNS name resolving to loopback",
                     "refuse POST", "refuse direct socket", "tools import no network libraries"):
            self.assertIn(must, names)

    def test_fails_closed_when_a_rule_is_broken(self):
        class Leaky(Gateway):
            def check(self, url, method="GET"):
                if "169.254" in url:
                    return None, "169.254.169.254"  # a regression that lets metadata through
                return super().check(url, method)
        rep = run(Leaky(), {"vLLM": 1}, None, reach_url=None)
        self.assertFalse(rep.passed)
        self.assertEqual([c.name for c in rep.failures], ["refuse cloud metadata"])

    def test_fails_without_socket_guard(self):
        socketguard.uninstall()
        rep = run(Gateway(), {}, None, reach_url=None)
        self.assertFalse(rep.passed)
        self.assertIn("socket guard installed", [c.name for c in rep.failures])

    def test_parse_check_and_offline_reachability(self):
        rep = run(Gateway(resolver=lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))), {}, None,
                  reach_url="https://example.com/", parse_check=lambda: (False, "plain text"))
        self.assertIn("model tool call parsed", [c.name for c in rep.failures])
        reach = next(c for c in rep.cases if c.name == "reach a public page")
        self.assertFalse(reach.ok)
        self.assertFalse(reach.critical)   # offline only warns


if __name__ == "__main__":
    unittest.main()
