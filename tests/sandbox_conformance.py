"""The sandbox backend conformance suite: what "interchangeable" means, as tests.

Every backend must pass all of it. A backend subclasses `Conformance`, sets
`make_backend()`, and gets the whole suite (see tests/test_sandbox_conformance.py).
The tests go through runner.run_program and runner.probe, the same path the broker uses.
"""

from __future__ import annotations

import os
import time

from perseus.sandbox.runner import RunSettings, probe, run_program

HELLO = {
    "python": 'print("hello")',
    "javascript": 'console.log("hello")',
    "bash": "echo hello",
    "c": '#include <stdio.h>\nint main(void) { puts("hello"); return 0; }\n',
    "cpp": '#include <iostream>\nint main() { std::cout << "hello" << std::endl; }\n',
    "rust": 'fn main() { println!("hello"); }\n',
    "go": 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hello") }\n',
    "java": 'public class Main { public static void main(String[] a) { System.out.println("hello"); } }\n',
}
BROKEN = {
    "c": "int main(void) { return undefined_name; }\n",
    "cpp": "int main() { return undefined_name; }\n",
    "rust": "fn main() { let x: i32 = \"not a number\"; }\n",
    "go": "package main\n\nfunc main() { undefinedName() }\n",
}
HOST_SECRET = "PERSEUS_CONFORMANCE_HOST_SECRET"


class Conformance:
    """Mixin for unittest.TestCase. Subclasses define make_backend()."""

    settings = RunSettings(network="none", run_timeout_s=10, build_timeout_s=120, max_output_kb=64)

    def make_backend(self):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = cls().make_backend()
        cls.found = probe(cls.backend, cls.settings)

    def run_(self, language, code, stdin="", **overrides):
        s = RunSettings(**{**self.settings.__dict__, **overrides})
        return run_program(self.backend, language, code, stdin, s)

    def needs(self, language):
        if language not in self.found["languages"]:
            self.skipTest(f"{language} toolchain not installed in this sandbox")

    # ------------------------------------------------------------- the contract
    def test_probe_finds_python_and_reports_network_closed(self):
        self.assertIn("python", self.found["languages"], self.found)
        self.assertIs(self.found["network_blocked"], True)

    def test_hello_world_in_every_installed_language(self):
        for language in self.found["languages"]:
            with self.subTest(language=language):
                r = self.run_(language, HELLO[language])
                self.assertEqual(r["status"], "ok", r)
                self.assertEqual(r["exit_code"], 0)
                self.assertEqual(r["stdout"].strip(), "hello")

    def test_compile_errors_are_reported_as_compile_errors(self):
        for language, code in BROKEN.items():
            if language not in self.found["languages"]:
                continue
            with self.subTest(language=language):
                r = self.run_(language, code)
                self.assertEqual(r["status"], "compile_error", r)
                self.assertNotEqual(r["build"]["exit_code"], 0)
                self.assertTrue(r["build"]["output"])
                self.assertNotIn("stdout", r)          # the program never ran

    def test_nonzero_exit_is_a_runtime_error_with_its_code(self):
        r = self.run_("python", "import sys\nprint('bye', file=sys.stderr)\nsys.exit(3)")
        self.assertEqual((r["status"], r["exit_code"]), ("runtime_error", 3))
        self.assertIn("bye", r["stderr"])

    def test_stdin_reaches_the_program(self):
        r = self.run_("python", "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))", stdin="2 3 5\n")
        self.assertEqual(r["stdout"].strip(), "10")

    def test_no_stdin_means_empty_input_not_a_hang(self):
        r = self.run_("python", "import sys\nprint(repr(sys.stdin.read()))", run_timeout_s=5)
        self.assertEqual((r["status"], r["stdout"].strip()), ("ok", "''"))

    def test_infinite_loop_is_killed_at_the_run_timeout(self):
        t0 = time.monotonic()
        r = self.run_("python", "print('started', flush=True)\nwhile True:\n    pass", run_timeout_s=2)
        self.assertEqual(r["status"], "timeout", r)
        self.assertIsNone(r["exit_code"])
        self.assertLess(time.monotonic() - t0, 60)

    def test_network_is_blocked(self):
        # a real HTTPS request: a firewall that filters by TLS host name can let a bare TCP connect through
        code = ("import urllib.request\nfor u in ('https://example.com/', 'https://1.1.1.1/'):\n"
                "    try:\n        urllib.request.urlopen(u, timeout=6); print('OPEN')\n"
                "    except Exception:\n        print('BLOCKED')")
        r = self.run_("python", code, run_timeout_s=20)
        self.assertEqual(r["stdout"].split(), ["BLOCKED", "BLOCKED"], r)

    def test_host_environment_is_not_visible(self):
        os.environ[HOST_SECRET] = "should-never-be-seen"
        try:
            r = self.run_("python", "import os\nprint('\\n'.join(sorted(os.environ)))")
        finally:
            os.environ.pop(HOST_SECRET, None)
        names = set(r["stdout"].split())
        for leaked in (HOST_SECRET, "PERSEUS_SANDBOX_TOKEN", "E2B_API_KEY", "HF_TOKEN"):
            self.assertNotIn(leaked, names)

    def test_every_call_gets_a_fresh_sandbox(self):
        self.run_("bash", "echo left-behind > marker.txt")
        r = self.run_("bash", "cat marker.txt 2>/dev/null || echo clean")
        self.assertEqual(r["stdout"].strip(), "clean")

    def test_packages_level_reaches_registries_and_nothing_else(self):
        if "packages" not in self.backend.capabilities().network_levels:
            self.skipTest("this backend can't enforce network = packages")
        code = ("import urllib.request\nfor u in ('https://pypi.org/simple/', 'https://example.com/'):\n"
                "    try:\n        urllib.request.urlopen(u, timeout=8); print(u, 'OPEN')\n"
                "    except Exception as e:\n        print(u, 'BLOCKED')")
        r = self.run_("python", code, network="packages", run_timeout_s=30)
        self.assertIn("https://pypi.org/simple/ OPEN", r["stdout"], r)
        self.assertIn("https://example.com/ BLOCKED", r["stdout"], r)

    def test_offline_call_has_no_network_even_when_the_setting_allows_it(self):
        level = "full" if "full" in self.backend.capabilities().network_levels else "none"
        s = RunSettings(**{**self.settings.__dict__, "network": level})
        code = ("import urllib.request\ntry:\n    urllib.request.urlopen('https://example.com/', timeout=6)\n"
                "    print('OPEN')\nexcept Exception:\n    print('BLOCKED')")
        r = run_program(self.backend, "python", code, settings=s, offline=True)
        self.assertEqual((r["network"], r["stdout"].strip()), ("none", "BLOCKED"))

    def test_huge_output_is_capped_keeping_head_and_tail(self):
        r = self.run_("python", "print('FIRST')\nprint('x' * 2_000_000)\nprint('LAST')", max_output_kb=16)
        self.assertTrue(r.get("output_cut"))
        self.assertLess(len(r["stdout"].encode()), 16 * 1024)
        self.assertTrue(r["stdout"].startswith("FIRST"))
        self.assertTrue(r["stdout"].rstrip().endswith("LAST"))
