"""The RCE addon: upstream client, relay policy and shaping, relay + client over HTTP, boot decisions, tool, config.

A fake WindowsRCE API (same endpoints, same X-API-Key header, same response shape) stands in
for the real VM, so these run anywhere.
"""

import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from perseus.addons.windows_rce import launch
from perseus.addons.windows_rce.client import MESSAGES, WindowsClient
from perseus.addons.windows_rce.relay import Relay, clean_path, serve
from perseus.addons.windows_rce.upstream import Upstream, check_url, read_secret
from perseus.config import ConfigError, validate as validate_config
from perseus.engine.tool_loader import import_problem
from perseus.sandbox.backends import SandboxError
from perseus.tools import TOOLS_DIR, Registry, ToolContext, ToolFailure, load_builtin

TOKEN = "t" * 32
KEY = "k" * 48


def setUpModule():
    patcher = mock.patch("perseus.addons.windows_rce.relay.log")   # keep test output clean
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


# ---------------------------------------------------------------------- a fake WindowsRCE API
class FakeAPI:
    """Answers like the real API. `script` maps a request to a response dict; `busy` 503s that many times."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.busy = 0
        self.reverts = 0
        self.result: dict = {"job_id": "j1", "exit_code": 0, "timed_out": False, "duration_s": 1.2,
                             "stdout": "hi\n", "stderr": "", "truncated": False, "collected_b64": {}, "missing": []}
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _ok_key(self):
                if self.headers.get("X-API-Key") != KEY:
                    self._send(401, {"detail": "bad key"})
                    return False
                return True

            def do_GET(self):
                if not self._ok_key():
                    return
                if self.path == "/status":
                    return self._send(200, {"vm_state": "running", "ssh_reachable": True, "busy": False, "runs": 3})
                self._send(404, {"detail": "nope"})

            def do_POST(self):
                if not self._ok_key():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                api.requests.append((self.path, body))
                if api.busy:
                    api.busy -= 1
                    return self._send(503, {"detail": "busy"})
                if self.path == "/revert":
                    api.reverts += 1
                    return self._send(200, {"reverted": True})
                if self.path == "/execute":
                    return self._send(200, dict(api.result, language=body.get("language")))
                self._send(404, {"detail": "nope"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------- upstream
class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.addCleanup(self.api.close)

    def test_status_execute_and_revert(self):
        up = Upstream(self.api.url, KEY)
        self.assertEqual(up.status()["vm_state"], "running")
        self.assertEqual(up.execute({"language": "python", "code": "x", "timeout": 5})["stdout"], "hi\n")
        up.revert()
        self.assertEqual(self.api.reverts, 1)

    def test_wrong_key_is_auth(self):
        with self.assertRaises(SandboxError) as c:
            Upstream(self.api.url, "wrong").status()
        self.assertEqual(c.exception.code, "auth")

    def test_unreachable(self):
        with self.assertRaises(SandboxError) as c:
            Upstream("http://127.0.0.1:1", KEY).status()
        self.assertEqual(c.exception.code, "unavailable")
        self.assertIn("start-api.ps1", str(c.exception))

    def test_busy_is_waited_out(self):
        self.api.busy = 2
        with mock.patch("perseus.addons.windows_rce.upstream.time.sleep"):
            r = Upstream(self.api.url, KEY).execute({"language": "python", "code": "x", "timeout": 5})
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(len(self.api.requests), 3)

    def test_busy_past_the_wait_is_quota(self):
        self.api.busy = 99
        with mock.patch("perseus.addons.windows_rce.upstream.time.sleep"), self.assertRaises(SandboxError) as c:
            Upstream(self.api.url, KEY).execute({"language": "python", "code": "x", "timeout": 5}, wait_busy_s=0)
        self.assertEqual(c.exception.code, "quota")

    def test_no_key_and_bad_urls(self):
        with self.assertRaises(SandboxError):
            Upstream(self.api.url, "")
        self.assertEqual(check_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765")
        for bad in ("127.0.0.1:8765", "ftp://x:1", "http://a:1/execute", "http://u:p@a:1", "http://a:1?x=1"):
            with self.subTest(url=bad), self.assertRaises(SandboxError):
                check_url(bad)

    def test_key_read_from_tooling_toml_only(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tooling.toml").write_text(f'[windows_rce]\napi_key = "{KEY}"\n')
            (Path(d) / "provider.toml").write_text('[secrets]\nhf_token = "hf_secret"\n')
            self.assertEqual(read_secret(d), KEY)
            (Path(d) / "tooling.toml").write_text('[windows_rce]\napi_key = "..."\n')
            self.assertEqual(read_secret(d), "")             # the template placeholder means "not set"
        self.assertEqual(read_secret(""), "")


# ---------------------------------------------------------------------- relay policy and shaping
def relay(**cfg) -> Relay:
    up = mock.Mock()
    r = Relay(cfg, TOKEN, upstream=up)
    r.state.update(starting=False, reachable=True)
    return r


class RelayPolicy(unittest.TestCase):
    def test_minimal_request(self):
        req = relay().parse({"language": "python", "code": "print(1)"})
        self.assertEqual(req, {"language": "python", "code": "print(1)", "files": {}, "stdin": "", "timeout": 60,
                               "network": False, "revert": True, "collect": []})

    def test_network_needs_allow_network(self):
        with self.assertRaises(SandboxError) as c:
            relay().parse({"language": "python", "code": "x", "network": True})
        self.assertIn("allow_network = false", str(c.exception))
        self.assertTrue(relay(allow_network=True).parse({"language": "python", "code": "x", "network": True})["network"])

    def test_keep_state_needs_allow_keep_state(self):
        self.assertFalse(relay().parse({"language": "python", "code": "x", "keep_vm_state": True})["revert"])
        with self.assertRaises(SandboxError) as c:
            relay(allow_keep_state=False).parse({"language": "python", "code": "x", "keep_vm_state": True})
        self.assertIn("allow_keep_state = false", str(c.exception))

    def test_timeout_capped(self):
        r = relay(run_timeout_s=30)
        self.assertEqual(r.parse({"language": "python", "code": "x"})["timeout"], 30)
        for bad in (0, 31, 2.5, True, "10"):
            with self.subTest(timeout=bad), self.assertRaises(SandboxError):
                r.parse({"language": "python", "code": "x", "timeout_s": bad})

    def test_paths_stay_inside_the_job_folder(self):
        self.assertEqual(clean_path("data\\in.csv"), "data/in.csv")
        for bad in ("../x", "C:/Windows/x", "/etc/x", "a/../b", "..", "a//b", ".hidden", "x" * 200, "a:b"):
            with self.subTest(path=bad), self.assertRaises(SandboxError):
                clean_path(bad)
        with self.assertRaises(SandboxError):
            relay().parse({"language": "python", "code": "x", "collect": ["../../secret"]})

    def test_extra_file_cannot_replace_main(self):
        with self.assertRaises(SandboxError):
            relay().parse({"language": "python", "code": "x", "files": {"main.py": "y"}})
        req = relay().parse({"language": "python", "code": "", "files": {"main.py": "y"}, "command": "python main.py"})
        self.assertEqual((req["files"], req["command"]), ({"main.py": "y"}, "python main.py"))

    def test_unknown_language_and_empty_program(self):
        with self.assertRaises(SandboxError):
            relay().parse({"language": "rust", "code": "fn main(){}"})
        with self.assertRaises(SandboxError):
            relay().parse({"language": "python", "code": "  "})


class RelayShape(unittest.TestCase):
    req = {"language": "python", "timeout": 60, "network": False, "revert": True}

    def test_outcomes(self):
        r = relay()
        self.assertEqual(r.shape(self.req, {"exit_code": 0, "stdout": "ok"})["status"], "ok")
        self.assertEqual(r.shape(self.req, {"exit_code": 1, "stderr": "Traceback"})["status"], "runtime_error")
        t = r.shape(self.req, {"exit_code": None, "timed_out": True})
        self.assertEqual((t["status"], t["note"]), ("timeout", "the program ran past 60s and was stopped"))
        cs = r.shape(dict(self.req, language="csharp"),
                     {"exit_code": 1, "stdout": "Program.cs(1,5): error CS1002: ; expected"})
        self.assertEqual(cs["status"], "compile_error")

    def test_ssh_failure_is_a_vm_error_not_a_program_error(self):
        r = relay()
        with self.assertRaises(SandboxError) as c:
            r.shape(self.req, {"exit_code": 255, "stdout": "", "stderr": ""})
        self.assertEqual(c.exception.code, "unavailable")
        self.assertTrue(r.dirty)

    def test_output_capped(self):
        out = relay(max_output_kb=4).shape(self.req, {"exit_code": 0, "stdout": "x" * 50_000})
        self.assertTrue(out["output_cut"])
        self.assertLess(len(out["stdout"]), 5000)

    def test_collected_text_binary_and_budget(self):
        r = relay(max_collect_kb=1)
        out = r.shape(self.req, {"exit_code": 0, "collected_b64": {
            "out.txt": b64("héllo".encode()), "img.png": b64(b"\x89PNG\x00\x01"),
            "big.csv": b64(b"a," * 2000), "late.txt": b64(b"more")}, "missing": ["gone.txt"]})
        files = {f["path"]: f for f in out["files"]}
        self.assertEqual(files["out.txt"]["text"], "héllo")
        self.assertTrue(files["img.png"]["binary"])
        self.assertTrue(files["big.csv"]["cut"])
        self.assertTrue(files["late.txt"]["omitted"])          # the budget ran out
        self.assertEqual(out["missing"], ["gone.txt"])

    def test_collected_bytes_pass_through_for_downloads(self):
        exe = b"MZ\x90\x00" + bytes(range(256)) * 8
        out = relay().shape(self.req, {"exit_code": 0, "collected_b64": {
            "publish\\app.exe": b64(exe), "notes.txt": b64(b"hi")}})
        files = {f["path"]: f for f in out["files"]}
        self.assertEqual(base64.b64decode(files["publish/app.exe"]["b64"]), exe)   # exact, slashes normalised
        self.assertTrue(files["publish/app.exe"]["binary"])
        self.assertEqual(base64.b64decode(files["notes.txt"]["b64"]), b"hi")

    def test_download_budget(self):
        out = relay(max_download_mb=0).shape(self.req, {"exit_code": 0, "collected_b64": {"app.exe": b64(b"MZ\x00")}})
        self.assertTrue(out["files"][0]["too_large"])
        self.assertNotIn("b64", out["files"][0])


# ---------------------------------------------------------------------- relay + client over HTTP
class RelayAndClient(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        self.relay = Relay({"url": self.api.url, "allow_keep_state": True, "idle_revert_s": 30}, TOKEN,
                           upstream=Upstream(self.api.url, KEY))
        self.httpd = serve(self.relay, 0, housekeeping=False)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.client = WindowsClient(self.httpd.server_address[1], TOKEN)
        deadline = time.monotonic() + 10
        while not self.client.healthy() and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_info_and_run(self):
        info = self.client.info()
        self.assertTrue(info["reachable"])
        self.assertEqual(info["vm"]["vm_state"], "running")
        self.assertEqual(info["languages"], ["python", "node", "powershell", "cmd", "csharp"])
        self.assertNotIn(KEY, json.dumps(info))                         # only a fingerprint
        r = self.client.run({"language": "python", "code": "print('hi')"})
        self.assertEqual((r["status"], r["stdout"], r["vm_state"]), ("ok", "hi\n", "reverted"))
        sent = self.api.requests[-1][1]
        self.assertEqual((sent["revert"], sent["network"]), (True, False))

    def test_a_built_exe_reaches_output_file_byte_for_byte(self):
        from perseus.engine.outputs import Outputs
        from perseus.tools.output_file import output_file
        from perseus.tools.run_windows_code import run_windows_code
        exe = b"MZ\x90\x00" + bytes(range(256)) * 4096          # 1 MB, every byte value
        self.api.result = {**self.api.result, "collected_b64": {"publish/app.exe": b64(exe)}}
        store = Outputs()
        ctx = ToolContext(windows=self.client, outputs=store)
        r = run_windows_code(ctx, "csharp", "x", command="dotnet publish -c Release -o publish",
                             collect=["publish/app.exe"])
        for o in r["outputs"]:                                  # what the engine does with the reserved key
            store.add(o["ref"], o["data"])
        got = output_file(ctx, "app.exe", source="windows:publish/app.exe")
        self.assertEqual(got["file"]["data"], exe)

    def test_policy_refusals_reach_the_model_as_plain_messages(self):
        with self.assertRaises(ToolFailure) as c:
            self.client.run({"language": "python", "code": "x", "network": True})
        self.assertIn("allow_network = false", str(c.exception))
        self.assertEqual(self.api.requests, [])                           # nothing reached the VM

    def test_kept_state_is_tracked_and_reverted_when_idle(self):
        self.client.run({"language": "python", "code": "x", "keep_vm_state": True})
        self.assertTrue(self.relay.dirty)
        self.assertFalse(self.relay.sweep())                              # not idle yet
        self.relay.last_used -= 31
        self.assertTrue(self.relay.sweep())
        self.assertFalse(self.relay.dirty)
        self.assertEqual(self.api.reverts, 1)

    def test_explicit_revert_and_shutdown_revert(self):
        self.client.revert()
        self.assertEqual(self.api.reverts, 1)
        self.client.run({"language": "python", "code": "x", "keep_vm_state": True})
        self.relay.shutdown()
        self.assertEqual(self.api.reverts, 2)
        self.relay.shutdown()                                             # clean now: nothing more to do
        self.assertEqual(self.api.reverts, 2)

    def test_bad_token_refused(self):
        bad = WindowsClient(self.httpd.server_address[1], "wrong" * 8)
        self.assertIn("error", bad.info())
        with self.assertRaises(ToolFailure):
            bad.run({"language": "python", "code": "x"})
        self.assertEqual(self.api.requests, [])

    def test_api_down_is_a_plain_message(self):
        self.api.close()
        with self.assertRaises(ToolFailure) as c:
            self.client.run({"language": "python", "code": "x"})
        self.assertEqual(str(c.exception), MESSAGES["unavailable"])

    def test_wrong_key_at_start_up_is_reported(self):
        relay_ = Relay({"url": self.api.url}, TOKEN, upstream=Upstream(self.api.url, "wrong"))
        relay_.start()
        self.assertEqual(relay_.state["error"]["code"], "auth")
        self.assertEqual(relay_.run({"language": "python", "code": "x"})[0], 503)

    def test_unreachable_at_start_up_is_not_fatal(self):
        relay_ = Relay({"url": "http://127.0.0.1:1"}, TOKEN, upstream=Upstream("http://127.0.0.1:1", KEY))
        relay_.start()
        self.assertIsNone(relay_.state["error"])
        self.assertFalse(relay_.state["reachable"])


# ---------------------------------------------------------------------- boot decisions
class Launch(unittest.TestCase):
    cfg = {"enabled": True, "url": "http://127.0.0.1:8765", "api_key_set": True}

    def test_off_reasons(self):
        self.assertIn("no [windows_rce] section", launch.plan({}).reason)
        self.assertIn("enabled = false", launch.plan({**self.cfg, "enabled": False}, "/c").reason)
        self.assertIn("api_key", launch.plan({**self.cfg, "api_key_set": False}, "/c").reason)
        self.assertIn("url", launch.plan({**self.cfg, "url": "8765"}, "/c").reason)

    def test_key_passed_by_config_path_never_read(self):
        p = launch.plan(self.cfg, "/home/u/Perseus/Config")
        self.assertTrue(p.start, p.reason)
        settings = json.loads(p.env[launch.SETTINGS_ENV])
        self.assertEqual(settings["config_path"], "/home/u/Perseus/Config")
        self.assertNotIn("api_key_set", settings)
        self.assertNotIn("api_key", settings)

    def test_relay_environment_is_an_allowlist(self):
        env = {"PATH": "/usr/bin", "HF_TOKEN": "hf_x", "E2B_API_KEY": "e2b_x", "HTTPS_PROXY": "http://p:1"}
        out = launch.relay_env(env, launch.plan(self.cfg, "/c"), TOKEN)
        self.assertEqual((out["PATH"], out[launch.TOKEN_ENV]), ("/usr/bin", TOKEN))
        for k in ("HF_TOKEN", "E2B_API_KEY", "HTTPS_PROXY"):
            self.assertNotIn(k, out)

    def test_connect_narrows_network_and_state_to_the_policy(self):
        reg, client = Registry(), mock.Mock()
        client.info.return_value = {"languages": ["python", "csharp"], "allow_network": False,
                                    "allow_keep_state": True, "reachable": True, "vm": {"vm_state": "running"},
                                    "identity": {"api_url": "http://127.0.0.1:8765", "key": "25…fb9c"},
                                    "limits": {"run_timeout_s": 90}}
        ok, msg = launch.connect(client, reg)
        self.assertTrue(ok)
        self.assertIn("network off", msg)
        cap = reg._caps["windows_rce"]
        self.assertEqual(cap.enums["network"], [False])
        self.assertEqual(cap.enums["keep_vm_state"], [False, True])
        self.assertIn("no network", cap.note)
        self.assertIn("90 s", cap.note)
        self.assertFalse(cap.reaches_web)

    def test_connect_warns_when_the_api_is_not_up_yet(self):
        reg, client = Registry(), mock.Mock()
        client.info.return_value = {"languages": ["python"], "reachable": False, "identity": {}}
        ok, msg = launch.connect(client, reg)
        self.assertTrue(ok)
        self.assertIn("not reachable yet", msg)

    def test_connect_reports_relay_errors(self):
        reg, client = Registry(), mock.Mock()
        reg.provide("windows_rce")
        client.info.return_value = {"error": {"code": "auth", "message": "rejected"}}
        self.assertEqual(launch.connect(client, reg), (False, "off (auth: rejected)"))
        self.assertNotIn("windows_rce", reg._caps)


# ---------------------------------------------------------------------- the tools
class WindowsTools(unittest.TestCase):
    def setUp(self):
        from perseus.tools import reset_windows_vm, run_windows_code
        self.mod = run_windows_code
        self.reset = reset_windows_vm.reset_windows_vm

    def ctx(self, result, limits=None):
        w = mock.Mock()
        w.run.return_value = result
        w.limits = limits or {"run_timeout_s": 120}
        return ToolContext(windows=w)

    def test_files_pass_the_import_check(self):
        self.assertIsNone(import_problem(TOOLS_DIR / "reset_windows_vm.py"))
        self.assertIsNone(import_problem(TOOLS_DIR / "run_windows_code.py"))

    def test_loaded_hidden_until_capability_then_narrowed(self):
        reg = load_builtin()
        self.assertIn("run_windows_code", reg.all_names())
        t = reg.loaded("run_windows_code")
        self.assertEqual((t.requires, t.untrusted, t.lookup), ("windows_rce", True, False))
        self.assertIsNone(reg.get("run_windows_code"))
        try:
            reg.provide("windows_rce", enums={"language": ["python"], "network": [False], "keep_vm_state": [False]})
            self.assertTrue(reg.validate("run_windows_code", {"language": "python", "code": "x", "network": True})[2])
            self.assertFalse(reg.validate("run_windows_code", {"language": "python", "code": "x"})[2])
            self.assertTrue(reg.validate("run_windows_code", {"language": "node", "code": "x"})[2])
        finally:
            reg.withdraw("windows_rce")

    def test_ok_render_and_feed(self):
        out = self.mod.run_windows_code(self.ctx({"status": "ok", "exit_code": 0, "stdout": "4\n", "stderr": "",
                                                  "seconds": 12.1, "vm_state": "reverted", "job_id": "j9",
                                                  "files": [{"path": "o.txt", "bytes": 2, "text": "hi"},
                                                            {"path": "a.png", "bytes": 9, "binary": True}]}),
                                        "python", "print(2+2)", collect=["o.txt", "a.png"])
        self.assertTrue(out["text"].startswith("ran successfully (ok), exit code 0"))
        self.assertIn("collected o.txt (2 bytes):\nhi", out["text"])
        self.assertIn("a.png: binary", out["text"])
        self.assertIn("clean snapshot", out["text"])
        self.assertEqual(out["feed"]["pill"], "Program OK")
        self.assertIn("job j9", out["feed"]["detail"])
        self.assertEqual(out["feed"]["summary"], "Windows · Python · 1 line")

    def test_collected_bytes_become_outputs_with_refs(self):
        exe = b"MZ\x90\x00\xff\xfe" * 100
        out = self.mod.run_windows_code(self.ctx({"status": "ok", "exit_code": 0, "stdout": "", "stderr": "",
                                                  "vm_state": "reverted", "files": [
                                                      {"path": "publish/app.exe", "bytes": len(exe), "binary": True,
                                                       "b64": base64.b64encode(exe).decode()},
                                                      {"path": "big.iso", "bytes": 9, "binary": True, "too_large": True}]}),
                                        "csharp", "x", collect=["publish/app.exe", "big.iso"])
        self.assertEqual(out["outputs"], [{"ref": "windows:publish/app.exe", "data": exe}])
        self.assertIn('output_file source="windows:publish/app.exe"', out["text"])
        self.assertIn("big.iso: binary, 9 bytes (not shown); too large", out["text"])
        self.assertNotIn(base64.b64encode(exe).decode()[:40], out["text"] + json.dumps(out["feed"]))

    def test_failure_headlines(self):
        r = self.mod.run_windows_code(self.ctx({"status": "runtime_error", "exit_code": 1, "stdout": "",
                                                "stderr": "Traceback\nZeroDivisionError: division by zero\n",
                                                "vm_state": "kept"}), "python", "1/0", keep_vm_state=True)
        self.assertEqual(r["feed"]["headline"], "ZeroDivisionError: division by zero")
        self.assertIn("kept its state", r["text"])
        cs = self.mod.run_windows_code(self.ctx({"status": "compile_error", "exit_code": 1,
                                                 "stdout": "Build FAILED.\nProgram.cs(1,1): error CS0103: x\n"}),
                                       "csharp", "x;")
        self.assertEqual(cs["feed"]["headline"], "Program.cs(1,1): error CS0103: x")

    def test_timeout_over_the_limit_and_off(self):
        with self.assertRaises(ToolFailure):
            self.mod.run_windows_code(self.ctx({}, {"run_timeout_s": 30}), "python", "x", timeout_s=60)
        with self.assertRaises(ToolFailure):
            self.mod.run_windows_code(ToolContext(), "python", "x")
        with self.assertRaises(ToolFailure):
            self.reset(ToolContext())

    def test_reset(self):
        ctx = self.ctx({})
        self.assertIn("clean snapshot", self.reset(ctx)["text"])
        ctx.windows.revert.assert_called_once()


# ---------------------------------------------------------------------- config
class Config(unittest.TestCase):
    base = {"model": {"id": "org/model"}}

    def test_defaults(self):
        w = validate_config(dict(self.base))["windows_rce"]
        self.assertEqual((w["enabled"], w["url"], w["allow_network"], w["run_timeout_s"]),
                         (True, "http://127.0.0.1:8765", False, 120))

    def test_placeholder_key_is_empty_and_the_key_never_reaches_resolved(self):
        self.assertEqual(validate_config({**self.base, "windows_rce": {"api_key": "..."}})["windows_rce"]["api_key"], "")
        cfg = validate_config({**self.base, "windows_rce": {"api_key": KEY}})
        self.assertNotIn(KEY, json.dumps(cfg.public()))

    def test_bad_values_named(self):
        for key, value, needle in (("url", "127.0.0.1:8765", "API's address"), ("run_timeout_s", 601, "between 1"),
                                   ("api_key", "a b", "no spaces")):
            with self.subTest(key=key), self.assertRaises(ConfigError) as c:
                validate_config({**self.base, "windows_rce": {key: value}})
            self.assertIn(needle, " ".join(c.exception.problems))

    def test_template_matches_the_schema(self):
        import tomllib
        tpl = tomllib.loads((Path(__file__).resolve().parents[1] / "Config/templates/tooling.toml").read_text())
        from perseus.config import SCHEMA, home_file
        self.assertEqual(set(tpl["windows_rce"]), set(SCHEMA["windows_rce"]))
        self.assertEqual(home_file("windows_rce", "api_key"), "tooling.toml")
        validate_config({**self.base, "windows_rce": tpl["windows_rce"]})


if __name__ == "__main__":
    unittest.main()
