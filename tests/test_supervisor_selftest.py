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


# A child that fails like vLLM on a hybrid model unless started with a small enough --max-num-seqs.
SEQS_CHILD = r"""
import sys, time
n = int(sys.argv[sys.argv.index("--max-num-seqs") + 1]) if "--max-num-seqs" in sys.argv else 1024
if n > 637:
    print("(EngineCore pid=1) ERROR 10-29 15:11:44 [core.py:1510] ValueError: max_num_seqs (%d) exceeds available "
          "Mamba cache blocks (637). Please lower max_num_seqs to at most 637 or increase "
          "gpu_memory_utilization." % n, flush=True)
    sys.exit(1)
print("serving", flush=True)
time.sleep(60)
"""


class Remedies(unittest.TestCase):
    def _sup(self, tmp, cmd_tail, remedy=None, max_restarts=3):
        script = tmp / "child.py"
        script.write_text(SEQS_CHILD)
        log = tmp / "svc.log"
        up = lambda: "serving" in (log.read_text() if log.exists() else "")
        sup = Supervisor(backoff=(0.05,), poll_s=0.05)
        sup.add(ProcessSpec("svc", [sys.executable, str(script), *cmd_tail], dict(os.environ), log, up,
                            ready_timeout_s=20, max_restarts=max_restarts, remedy=remedy))
        return sup

    def wait(self, sup, *states):
        deadline = time.time() + 15
        while sup.procs["svc"].state not in states and time.time() < deadline:
            time.sleep(0.05)
        if sup.procs["svc"].state == "restarting":
            pass
        return sup.procs["svc"]

    def test_remedy_fixes_the_command_and_it_comes_up(self):
        from perseus.model.launch import remedy
        tmp, r = Path(tempfile.mkdtemp()), {"vllm": {}}
        sup = self._sup(tmp, ["--max-num-seqs", "1024"], remedy=lambda log, cmd: remedy(log, cmd, r))
        try:
            sup.start("svc")
            deadline = time.time() + 15
            while time.time() < deadline and "serving" not in (tmp / "svc.log").read_text():
                time.sleep(0.05)
            m = sup.wait_ready("svc")
            self.assertEqual(m.state, "up")
            self.assertEqual(m.restarts, 1)                       # one wasted attempt, not three
            self.assertEqual(m.remedies, ["--max-num-seqs 637"])
            self.assertEqual(m.spec.cmd[-2:], ["--max-num-seqs", "637"])
            self.assertEqual(r["vllm"]["max_num_seqs"], 637)      # a later settings restart keeps it
        finally:
            sup.stop_all()

    def test_same_error_twice_with_the_same_command_stops_retrying(self):
        tmp = Path(tempfile.mkdtemp())
        sup = self._sup(tmp, ["--max-num-seqs", "1024"], max_restarts=5)   # no remedy
        try:
            sup.start("svc")
            m = self.wait(sup, "failed")
            self.assertEqual(m.state, "failed")
            self.assertEqual(m.restarts, 1)                       # not 5
            self.assertIn("same error twice", m.reason)
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
