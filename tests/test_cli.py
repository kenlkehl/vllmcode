import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from vllmcode.cli import (Client, Error, FLAGS, PORTS, RequestError, Server, candidates,
                          discover, launch_config, main, probe, validate_flags)


class FakeServer:
    def __init__(self):
        self.requests = []
        self.models = [{"id": "org/model", "max_model_len": 32768}]
        self.flags = {"reasoning_parser": "qwen3", "tool_call_parser": "hermes", "enable_auto_tool_choice": True}
        self.key = None
        self.reasoning = "17 times 19 is 323."
        self.call = True
        self.bad_protocol = False
        self.server_info = True
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond(None)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.respond(body)

            def respond(self, body):
                fixture.requests.append((self.path, body, self.headers.get("Authorization")))
                status = 200
                if fixture.key and self.headers.get("Authorization") != "Bearer " + fixture.key:
                    status, result = 401, {"error": "Unauthorized"}
                elif self.path == "/v1/models":
                    result = {"data": fixture.models}
                elif self.path.startswith("/server_info") and fixture.server_info:
                    result = {"args": fixture.flags}
                elif self.path == "/v1/chat/completions":
                    call = {"function": {"name": "vllmcode_probe", "arguments": '{"answer":323}'}}
                    result = {"choices": [{"message": {"reasoning": fixture.reasoning,
                              "tool_calls": [call] if fixture.call else []}}]}
                elif self.path == "/v1/responses":
                    result = {"output": [] if fixture.bad_protocol else [
                        {"type": "function_call", "name": "vllmcode_probe", "arguments": '{"answer":323}'}]}
                elif self.path == "/v1/messages":
                    result = {"content": [] if fixture.bad_protocol else [
                        {"type": "tool_use", "name": "vllmcode_probe", "input": {"answer": 323}}]}
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", fixture.base + "/v1/models")
                    self.end_headers()
                    return
                else:
                    status, result = 404, {"error": "Not found"}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class URLTests(unittest.TestCase):
    def test_equivalent_inputs(self):
        for value in ("host:8020", "http://host:8020", "host:8020/v1", "http://host:8020/v1/"):
            self.assertEqual(candidates(value), ["http://host:8020/v1"])

    def test_port_order_and_proxy_default(self):
        for value in ("host", "host/v1", "http://host", "http://host/v1/"):
            self.assertEqual(candidates(value), [f"http://host:{p}/v1" for p in PORTS] + ["http://host/v1"])

    def test_https_prefix_ipv6(self):
        self.assertEqual(candidates("https://host:443/llm/v1/"), ["https://host:443/llm/v1"])
        self.assertEqual(candidates("[::1]:8000/v1"), ["http://[::1]:8000/v1"])
        self.assertEqual(candidates("https://host")[-1], "https://host/v1")

    def test_reject_bad_urls(self):
        for value in ("", "http://", "ftp://host", "http://host:bad", "http://u:p@host",
                      "host?key=secret", "host#fragment", "host:0", "host:65536", "host:", "bad host", "::1"):
            with self.subTest(value=value), self.assertRaises(Error):
                candidates(value)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.err = contextlib.redirect_stderr(io.StringIO())
        self.out.__enter__()
        self.err.__enter__()

    def tearDown(self):
        self.err.__exit__(None, None, None)
        self.out.__exit__(None, None, None)

    def test_discovery_auth_and_lora(self):
        with FakeServer() as f:
            f.key = "test-secret"
            with self.assertRaisesRegex(Error, "authorization"):
                discover(Client(), f.base)
            f.models.append({"id": "adapter", "parent": "org/model"})
            s = discover(Client(f.key), f.base + "/v1")
            self.assertEqual((s.model, s.context), ("org/model", 32768))
            self.assertEqual(discover(Client(f.key), f.base, "adapter").model, "adapter")
            with self.assertRaisesRegex(Error, "not served"):
                discover(Client(f.key), f.base, "missing")

    def test_multiple_models_require_selection(self):
        with FakeServer() as f:
            f.models.append({"id": "second"})
            with self.assertRaisesRegex(Error, "Multiple models"):
                discover(Client(), f.base)

    def test_discovery_falls_through_in_order(self):
        calls = []

        def request(url):
            calls.append(url)
            if ":8020/" not in url:
                raise RequestError("Connection refused")
            return {"data": [{"id": "model"}]}

        client = Client()
        client.request = request
        self.assertEqual(discover(client, "host").base, "http://host:8020/v1")
        self.assertEqual(calls, [f"http://host:{p}/v1/models" for p in PORTS[:4]])

    def test_flags_and_protocols(self):
        with FakeServer() as f:
            s = discover(Client(), f.base)
            validate_flags(Client(), s, strict=True)
            for harness in ("codex", "claude", "opencode"):
                probe(Client(), s, harness)
            f.flags["enable_auto_tool_choice"] = False
            with self.assertRaisesRegex(Error, "disabled"):
                validate_flags(Client(), s)
            f.server_info = False
            validate_flags(Client(), s)
            with self.assertRaisesRegex(Error, "Strict"):
                validate_flags(Client(), s, strict=True)

    def test_missing_reasoning_tools_and_protocol_fail(self):
        with FakeServer() as f:
            s = discover(Client(), f.base)
            f.reasoning = ""
            with self.assertRaisesRegex(Error, "reasoning"):
                probe(Client(), s, "codex")
            f.reasoning = "thinking"
            f.call = False
            with self.assertRaisesRegex(Error, "Automatic tool-call"):
                probe(Client(), s, "codex")
            f.call = True
            f.bad_protocol = True
            for harness in ("codex", "claude"):
                with self.assertRaisesRegex(Error, "API tool-call"):
                    probe(Client(), s, harness)

    def test_dry_run_redacts_and_does_not_exec(self):
        with FakeServer() as f, patch.dict(os.environ, {"VLLM_API_KEY": "test-secret"}), patch("os.execvpe") as execv:
            f.key = "test-secret"
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(["run", "claude", f.base, "--dry-run"]), 0)
            self.assertNotIn("test-secret", stream.getvalue())
            self.assertIn("org/model", stream.getvalue())
            execv.assert_not_called()

    def test_redirects_are_not_followed(self):
        with FakeServer() as f:
            with self.assertRaises(RequestError) as exc:
                Client("secret").request(f.base + "/redirect")
            self.assertEqual(exc.exception.status, 302)
            self.assertEqual(len(f.requests), 1)

    def test_effort_cli_reaches_probes_and_launch(self):
        with FakeServer() as f, patch.dict(os.environ, {"VLLM_API_KEY": "", "OPENCODE_CONFIG_CONTENT": "{}"}):
            for harness in ("codex", "claude", "opencode"):
                with self.subTest(harness=harness), patch("shutil.which", return_value="agent"), patch("os.execvpe") as execute:
                    f.requests.clear()
                    self.assertEqual(main(["run", harness, f.base, "--effort", "xhigh"]), 0)
                    bodies = {path: body for path, body, _ in f.requests if body is not None}
                    self.assertEqual(bodies["/v1/chat/completions"]["reasoning_effort"], "xhigh")
                    if harness == "codex":
                        self.assertEqual(bodies["/v1/responses"]["reasoning"], {"effort": "xhigh"})
                        self.assertIn('model_reasoning_effort="xhigh"', execute.call_args.args[1])
                    elif harness == "claude":
                        self.assertEqual(bodies["/v1/messages"]["output_config"], {"effort": "xhigh"})
                        self.assertEqual(execute.call_args.args[1][-2:], ["--effort", "xhigh"])
                    else:
                        cfg = json.loads(execute.call_args.args[2]["OPENCODE_CONFIG_CONTENT"])
                        self.assertEqual(cfg["provider"]["vllmcode"]["models"]["org/model"]["options"]["reasoningEffort"], "xhigh")

    def test_rejected_server_effort_blocks_launch(self):
        with patch("shutil.which", return_value="agent"), patch("vllmcode.cli.discover", return_value=Server("http://host/v1", "model")), \
             patch("vllmcode.cli.validate_flags"), patch("vllmcode.cli.Client.request", side_effect=RequestError("unsupported effort", 400)), \
             patch("os.execvpe") as execute:
            self.assertEqual(main(["run", "codex", "host", "--effort", "high"]), 1)
            execute.assert_not_called()

    def test_auto_review_cli_and_failed_probe(self):
        with FakeServer() as f, patch.dict(os.environ, {"VLLM_API_KEY": ""}), \
             patch("shutil.which", return_value="codex"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.153.4", "")), \
             patch("os.execvpe") as execute:
            self.assertEqual(main(["run", "codex", f.base, "--auto-review", "--", "exec", "hello"]), 0)
            self.assertIn('approvals_reviewer="auto_review"', execute.call_args.args[1])
            self.assertEqual(execute.call_args.args[1][-2:], ["exec", "hello"])
            execute.reset_mock()
            f.call = False
            self.assertEqual(main(["run", "codex", f.base, "--auto-review"]), 1)
            execute.assert_not_called()

    def test_real_process_handoff(self):
        prompt = "literal $(echo nope) with spaces"
        cases = (("codex", ["exec", "--json", prompt]),
                 ("claude", ["-p", prompt, "--output-format", "json"]),
                 ("opencode", ["run", prompt, "--format", "json"]))
        with FakeServer() as f, tempfile.TemporaryDirectory() as tmp:
            for name, extra in cases:
                with self.subTest(harness=name):
                    harness = Path(tmp, name)
                    harness.write_text(f'#!{sys.executable}\nimport sys,json\n'
                                       'print(json.dumps({"args":sys.argv[1:],"stdin":sys.stdin.read()}))\n'
                                       'sys.exit(7)\n')
                    harness.chmod(0o755)
                    env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], VLLM_API_KEY="",
                               OPENCODE_CONFIG_CONTENT="{}")
                    result = subprocess.run([sys.executable, "-m", "vllmcode", "run", name, f.base, "--", *extra],
                                            env=env, input="piped input\n", capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    # Agent stdout is directly parseable, without launcher diagnostics.
                    saved = json.loads(result.stdout)
                    self.assertEqual(saved["args"][-len(extra):], extra)
                    self.assertEqual(saved["stdin"], "piped input\n")
                    self.assertLess(result.stderr.index("Model:"), result.stderr.index("Starting " + name))


class ConfigurationTests(unittest.TestCase):
    def test_codex_auto_review_routing(self):
        server = Server("https://host/v1", "org/model", 262144)
        cmd, env = launch_config("codex", server, "secret", ["exec", "--json", "Explain this repository"], {},
                                 effort="xhigh", auto_review=True)
        self.assertIn('approval_policy="on-request"', cmd)
        self.assertIn('approvals_reviewer="auto_review"', cmd)
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertIn('model="org/model"', cmd)
        self.assertIn('model_providers.vllmcode.base_url="https://host/v1"', cmd)
        path = json.loads(next(x.split("=", 1)[1] for x in cmd if x.startswith("model_catalog_json=")))
        catalog = json.loads(Path(path).read_text())
        self.assertTrue(catalog["models"])
        self.assertTrue(all(not m["supported_in_api"] and m["visibility"] == "none" for m in catalog["models"]))
        self.assertNotIn("secret", " ".join(cmd))
        self.assertEqual(env["VLLMCODE_API_KEY"], "secret")
        normal, _ = launch_config("codex", server, "", [], {})
        self.assertFalse(any(x.startswith(("approval_policy=", "approvals_reviewer=", "model_catalog_json=")) for x in normal))

    def test_auto_review_rejects_conflicting_routing_and_permissions(self):
        for extra in (["--yolo"], ["exec", "-s", "danger-full-access"], ["-melsewhere"],
                      ["--config=approval_policy='never'"], ["-c", 'model_provider="openai"'],
                      ["-cmodel_catalog_json='/tmp/models.json'"], ["--remote=ws://host"],
                      ["--config", 'permissions.foo.bar=true']):
            with self.subTest(extra=extra), self.assertRaises(Error):
                launch_config("codex", Server("http://host/v1", "model"), "", extra, {}, auto_review=True)
        for harness in ("claude", "opencode"):
            with self.assertRaises(Error):
                launch_config(harness, Server("http://host/v1", "model"), "", [], {}, auto_review=True)
        # A prompt mentioning config keys is not itself an override.
        launch_config("codex", Server("http://host/v1", "model"), "", ["exec", "Explain approval_policy=never"], {}, auto_review=True)

    def test_auto_review_version_check_blocks_before_network(self):
        for version in ("codex-cli 0.152.0", "unexpected version"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, version, "")), \
                 patch("vllmcode.cli.discover") as discover_mock, patch("os.execvpe") as execute:
                self.assertEqual(main(["run", "codex", "host", "--auto-review"]), 1)
                discover_mock.assert_not_called()
                execute.assert_not_called()

    def test_first_class_effort(self):
        server = Server("http://host/v1", "model", 262144)
        cmd, _ = launch_config("codex", server, "", [], {}, effort="xhigh")
        self.assertIn('model_reasoning_effort="xhigh"', cmd)
        self.assertIn('model_supports_reasoning_summaries=true', cmd)
        cmd, _ = launch_config("claude", server, "", [], {}, effort="low")
        self.assertEqual(cmd[-2:], ["--effort", "low"])
        _, env = launch_config("opencode", server, "", [], {}, effort="xhigh")
        model = json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["vllmcode"]["models"]["model"]
        self.assertEqual(model["options"], {"reasoningEffort": "xhigh"})

    def test_effort_rejections(self):
        server = Server("http://host/v1", "model")
        for harness, effort, extra in (("codex", "max", []), ("claude", "minimal", []),
                                      ("opencode", "invalid", []), ("claude", "low", ["--effort=high"]),
                                      ("opencode", "low", ["run", "--variant", "high"]),
                                      ("codex", "low", ["-c", 'model_reasoning_effort="high"'])):
            with self.subTest(harness=harness, effort=effort), self.assertRaises(Error):
                launch_config(harness, server, "", extra, {}, effort=effort)

    def test_opencode_output_budget_and_reserve(self):
        for context, requested, expected in ((262144, None, 32768), (262144, 65536, 65536),
                                             (32768, None, 8192), (32768, 16384, 16384),
                                             (None, None, 32768)):
            with self.subTest(context=context, requested=requested):
                _, env = launch_config("opencode", Server("http://host/v1", "model", context), "", [],
                    {"OPENCODE_CONFIG_CONTENT": '{"compaction":{"auto":false,"reserved":1,"prune":true}}',
                     "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "8192"}, max_output_tokens=requested)
                config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                self.assertEqual(config["provider"]["vllmcode"]["models"]["model"]["limit"]["output"], expected)
                self.assertEqual(config["compaction"], {"auto": True, "reserved": expected, "prune": True})
                self.assertEqual(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], str(expected))

    def test_invalid_output_budgets(self):
        for budget in (0, -1, 1.5, 32768, 65536):
            with self.subTest(budget=budget), self.assertRaises(Error):
                launch_config("opencode", Server("http://host/v1", "model", 32768), "", [], {}, max_output_tokens=budget)
        with self.assertRaisesRegex(Error, "only to opencode"):
            launch_config("codex", Server("http://host/v1", "model"), "", [], {}, max_output_tokens=32768)

    def test_codex_uses_responses_and_no_global_credentials(self):
        cmd, env = launch_config("codex", Server("https://host:443/proxy/v1", 'org/model"name', 4096), "secret", [], {})
        self.assertIn('model_providers.vllmcode.wire_api="responses"', cmd)
        self.assertIn('model_providers.vllmcode.requires_openai_auth=false', cmd)
        self.assertNotIn("secret", " ".join(cmd))
        self.assertEqual(env["VLLMCODE_API_KEY"], "secret")

    def test_claude_maps_all_models_and_clears_cloud_routing(self):
        original = {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_AUTH_TOKEN": "cloud-secret",
                    "CLAUDE_CODE_EFFORT_LEVEL": "high"}
        cmd, env = launch_config("claude", Server("http://host:8000/prefix/v1", "org/model"), "local-key", [], original)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://host:8000/prefix")
        self.assertEqual(cmd[-2:], ["--effort", "medium"])
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", env)
        self.assertEqual(original["ANTHROPIC_AUTH_TOKEN"], "cloud-secret")
        for tier in ("OPUS", "SONNET", "HAIKU"):
            self.assertEqual(env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"], "org/model")

    def test_opencode_preserves_preferences_and_model_slash(self):
        cmd, env = launch_config("opencode", Server("http://host:8000/v1", "org/model", 32768), "secret", [],
                                {"OPENCODE_CONFIG_CONTENT": '{"theme":"dark","disabled_providers":["vllmcode"]}'})
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["model"], "vllmcode/org/model")
        self.assertEqual(config["disabled_providers"], [])
        self.assertNotIn("secret", env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["provider"]["vllmcode"]["models"]["org/model"]["limit"]["context"], 32768)


if __name__ == "__main__":
    unittest.main()
