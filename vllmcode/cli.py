"""Dependency-free discovery, capability checks, and per-process configuration."""

import argparse
import json
import math
import os
import shlex
import shutil
import socket
import ssl
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PORTS = (8000, 8001, 8002, 8020, 8030)
FLAGS = ("reasoning_parser", "tool_call_parser", "enable_auto_tool_choice")
FLAG_HELP = ("Start vLLM with --reasoning-parser <model-specific-parser> "
             "--tool-call-parser <model-specific-parser> --enable-auto-tool-choice.")
PROBE_FUNCTION = {
    "name": "vllmcode_probe", "description": "Report the computed answer.",
    "parameters": {"type": "object", "properties": {"answer": {"type": "integer"}},
                   "required": ["answer"]},
}
PROBE_PROMPT = ("Think through 17 * 19, then call vllmcode_probe with the integer "
                "answer. You must call the tool.")


class Error(Exception):
    pass


class RequestError(Error):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    # Never forward the user's API key to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def display(value):
    """Escape terminal control characters from remote data."""
    return str(value).encode("unicode_escape").decode("ascii")


def candidates(server):
    value = server.strip()
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise Error("Server must be a hostname or HTTP(S) URL without whitespace.")
    explicit_scheme = "://" in value
    try:
        u = urlsplit(value if explicit_scheme else "http://" + value)
        port = u.port
        host = u.hostname
    except ValueError as exc:
        raise Error(f"Invalid server: {exc}. Bracket IPv6 addresses, e.g. [::1]:8000.") from exc
    if u.scheme not in ("http", "https") or not host:
        raise Error("Server must use http:// or https:// and include a hostname.")
    if u.username is not None or u.password is not None or u.query or u.fragment:
        raise Error("Do not include credentials, query strings, or fragments in the server URL.")
    if u.netloc.endswith(":") or port == 0:
        raise Error("Specify a port between 1 and 65535, or omit it entirely.")
    host = f"[{host}]" if ":" in host else host
    path = u.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    # Explicit ports are authoritative. For no port, try the requested common
    # ports in order, then the scheme's default (for reverse proxies).
    ports = (port,) if port is not None else (*PORTS, None)
    return [urlunsplit((u.scheme, host + (f":{p}" if p else ""), path + "/v1", "", ""))
            for p in ports]


class Client:
    def __init__(self, key="", timeout=3):
        self.key = key
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, url, payload=None, timeout=None):
        headers = {"Accept": "application/json", "User-Agent": "vllmcode/0.1.0"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        if payload is not None:
            headers.update({"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        request = Request(url, None if payload is None else json.dumps(payload).encode(), headers)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise RequestError("Response exceeded 4 MiB.")
                return json.loads(raw)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", "replace")
            if self.key:
                detail = detail.replace(self.key, "[redacted]")
            raise RequestError(f"HTTP {exc.code}: {display(detail)}", exc.code) from exc
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise RequestError(display(exc)) from exc
        except (ValueError, UnicodeError) as exc:
            raise RequestError("Endpoint did not return valid JSON.") from exc


@dataclass
class Server:
    base: str
    model: str
    context: int | None = None

    @property
    def root(self):
        return self.base[:-3]


def discover(client, server, model=None):
    errors = []
    for base in candidates(server):
        print(f"Checking {display(base)} ...", file=sys.stderr, flush=True)
        try:
            result = client.request(base + "/models")
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list) or not rows:
                raise RequestError("No models advertised by /v1/models.")
            rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]
            if not rows:
                raise RequestError("No valid model IDs in /v1/models.")
        except RequestError as exc:
            if exc.status in (401, 403):
                raise Error(f"{base} requires authorization. Set VLLM_API_KEY (or --api-key-env).") from exc
            errors.append(f"  {base}: {exc}")
            continue
        if model:
            selected = next((r for r in rows if r["id"] == model), None)
            if selected is None:
                raise Error(f"Model {display(model)} is not served here. Available: " +
                            ", ".join(display(r["id"]) for r in rows))
        else:
            # Prefer the base model over dynamically loaded LoRA adapters.
            base_models = [r for r in rows if r.get("parent") is None]
            choices = base_models or rows
            if len(choices) != 1:
                raise Error("Multiple models advertised; select --model from: " +
                            ", ".join(display(r["id"]) for r in choices))
            selected = choices[0]
        context = selected.get("max_model_len")
        return Server(base, selected["id"], context if type(context) is int and context > 0 else None)
    raise Error("No accessible model server found. Check DNS/VPN, bind address and firewall.\n" + "\n".join(errors))


def exposed_flags(value):
    found = {}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.replace("-", "_")
            if normalized in FLAGS:
                found[normalized] = item
            elif isinstance(item, (dict, list)):
                found.update(exposed_flags(item))
            elif isinstance(item, str) and item.startswith("{"):
                try:
                    found.update(exposed_flags(json.loads(item)))
                except ValueError:
                    pass
    elif isinstance(value, list):
        for item in value:
            found.update(exposed_flags(item))
    return found


def validate_flags(client, server, strict=False):
    try:
        flags = exposed_flags(client.request(server.root + "/server_info?config_format=json"))
    except RequestError:
        flags = {}
    for flag, value in flags.items():
        valid = (value is True or value == "true") if flag == "enable_auto_tool_choice" else (
            isinstance(value, str) and value.strip().lower() not in ("", "none", "null", "false"))
        if not valid:
            raise Error(f"Server reports --{flag.replace('_', '-')} disabled or unset. {FLAG_HELP}")
    missing = [f for f in FLAGS if f not in flags]
    if missing:
        note = "Startup settings not exposed: " + ", ".join(missing) + "."
        if strict:
            raise Error(note + " Strict verification requires these fields in /server_info JSON.")
        print(note + " Checking observable reasoning/tool behavior instead.", file=sys.stderr, flush=True)
    else:
        print("Startup settings verified: " + ", ".join(f"{k}={display(v)}" for k, v in flags.items()), flush=True)


def valid_call(name, arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return False
    return name == "vllmcode_probe" and isinstance(arguments, dict) and arguments.get("answer") == 323


def probe(client, server, harness, timeout=60):
    body = {"model": server.model, "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "tools": [{"type": "function", "function": PROBE_FUNCTION}],
            "tool_choice": "auto", "max_tokens": 1024, "temperature": 0}
    try:
        reply = client.request(server.base + "/chat/completions", body, timeout)
        message = reply["choices"][0]["message"]
        reasoning = message.get("reasoning") or message.get("reasoning_content") or message.get("reasoning_text")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise Error("No separated reasoning in probe response. " + FLAG_HELP)
        calls = message.get("tool_calls") or []
        if not any(valid_call(c.get("function", {}).get("name"), c.get("function", {}).get("arguments")) for c in calls):
            raise Error("Automatic tool-call probe failed (no valid structured tool call). " + FLAG_HELP)
        print("Verified: separated reasoning and automatic tool calling.", flush=True)
        # Test tool support in the actual harness protocol, not merely whether
        # an endpoint exists. The dummy tool is never executed.
        if harness == "codex":
            body = {"model": server.model, "input": PROBE_PROMPT,
                    "tools": [{"type": "function", **PROBE_FUNCTION}], "tool_choice": "auto",
                    "max_output_tokens": 1024, "store": False, "temperature": 0}
            result = client.request(server.base + "/responses", body, timeout)
            if not any(c.get("type") == "function_call" and valid_call(c.get("name"), c.get("arguments"))
                       for c in result.get("output", [])):
                raise Error("Responses API tool-call probe failed. This Codex integration needs /v1/responses tool support.")
        elif harness == "claude":
            body = {"model": server.model, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                    "tools": [{"name": PROBE_FUNCTION["name"], "description": PROBE_FUNCTION["description"],
                               "input_schema": PROBE_FUNCTION["parameters"]}],
                    "tool_choice": {"type": "auto"}, "max_tokens": 1024, "temperature": 0,
                    "output_config": {"effort": "medium"}}
            result = client.request(server.base + "/messages", body, timeout)
            if not any(c.get("type") == "tool_use" and valid_call(c.get("name"), c.get("input"))
                       for c in result.get("content", [])):
                raise Error("Anthropic Messages API tool-call probe failed. Update vLLM to a version with /v1/messages support.")
    except RequestError as exc:
        raise Error(f"{harness} compatibility probe failed: {exc}\n{FLAG_HELP}") from exc
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise Error(f"Malformed {harness} compatibility response; server protocol is incompatible.") from exc
    print(f"Verified: {harness} API tool calling.", flush=True)


def output_budget(context, requested=None):
    if requested is not None:
        if type(requested) is not int or requested <= 0:
            raise Error("--max-output-tokens must be a positive integer.")
        if context and requested >= context:
            raise Error(f"--max-output-tokens must be smaller than the server context ({context} tokens) to leave room for input.")
        return requested
    return min(32768, max(1, context // 4)) if context else 32768


def launch_config(harness, server, key, extra, inherited=None, max_output_tokens=None):
    if max_output_tokens is not None and harness != "opencode":
        raise Error("--max-output-tokens currently applies only to opencode.")
    env = dict(os.environ if inherited is None else inherited)
    env["VLLMCODE_API_KEY"] = key or "vllmcode-no-key"
    model = server.model
    if harness == "codex":
        settings = {"model_provider": "vllmcode", "model": model,
                    "model_providers.vllmcode.name": "vLLM",
                    "model_providers.vllmcode.base_url": server.base,
                    "model_providers.vllmcode.wire_api": "responses",
                    "model_providers.vllmcode.env_key": "VLLMCODE_API_KEY",
                    "model_providers.vllmcode.requires_openai_auth": False}
        if server.context:
            settings["model_context_window"] = server.context
        cmd = ["codex"]
        for k, v in settings.items():
            cmd.extend(["-c", k + "=" + json.dumps(v, ensure_ascii=False)])
    elif harness == "claude":
        for k in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                  "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                  "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_EFFORT_LEVEL"):
            env.pop(k, None)
        env.update({"ANTHROPIC_BASE_URL": server.root, "ANTHROPIC_API_KEY": key or "vllmcode-no-key",
                    "ANTHROPIC_AUTH_TOKEN": key or "vllmcode-no-key", "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_SMALL_FAST_MODEL": model, "CLAUDE_CODE_SUBAGENT_MODEL": model,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        for tier in ("OPUS", "SONNET", "HAIKU"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
        if server.context:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(server.context)
        # Claude defaults to high; some vLLM models reject high but accept medium.
        # Keep this in sync with the Messages compatibility probe.
        cmd = ["claude", "--model", model, "--effort", "medium"]
    else:
        try:
            config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        except ValueError as exc:
            raise Error("Existing OPENCODE_CONFIG_CONTENT must be valid JSON.") from exc
        if not isinstance(config, dict) or not isinstance(config.get("provider", {}), dict):
            raise Error("Existing OpenCode inline configuration must contain JSON objects.")
        budget = output_budget(server.context, max_output_tokens)
        entry = {"name": model, "tool_call": True, "reasoning": True,
                 "limit": {"context": server.context or 0, "output": budget}}
        compaction = config.setdefault("compaction", {})
        if not isinstance(compaction, dict):
            raise Error("Existing OpenCode compaction configuration must be a JSON object.")
        compaction.update({"auto": True, "reserved": budget})
        # OpenCode clamps actual generations separately from model.limit.output.
        env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(budget)
        config.setdefault("provider", {})["vllmcode"] = {
            "npm": "@ai-sdk/openai-compatible", "name": "vLLM",
            "options": {"baseURL": server.base, "apiKey": "{env:VLLMCODE_API_KEY}"},
            "models": {model: entry}}
        config.update({"model": "vllmcode/" + model, "small_model": "vllmcode/" + model})
        # Inline enabled/disabled lists override conflicting provider filters.
        config["enabled_providers"] = ["vllmcode"]
        config["disabled_providers"] = []
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
        cmd = ["opencode", "--model", "vllmcode/" + model]
    return cmd + list(extra), env


def positive(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be a positive number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number.")
    return number


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be a positive integer.") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Launch a coding agent against a running vLLM server.")
    parser.add_argument("--version", action="version", version="vllmcode 0.1.0")
    subs = parser.add_subparsers(dest="command", required=True)
    run = subs.add_parser("run", help="Discover, validate and launch an agent",
                          epilog="Pass agent arguments after --. Example: vllmcode run codex host -- exec 'Say hello'")
    run.add_argument("harness", choices=("codex", "claude", "opencode"))
    run.add_argument("server", help="hostname, host:port, or http(s)://host[:port][/prefix][/v1]")
    run.add_argument("--model", help="Choose an advertised model when the server lists multiple models")
    run.add_argument("--max-output-tokens", type=positive_integer,
                     help="OpenCode generation budget, including reasoning (default: 32768, at most 1/4 of known context)")
    run.add_argument("--api-key-env", default="VLLM_API_KEY", help="Environment variable holding the vLLM key")
    run.add_argument("--timeout", type=positive, default=3, help="Seconds per discovery request (default: 3)")
    run.add_argument("--probe-timeout", type=positive, default=60, help="Seconds per inference probe (default: 60)")
    run.add_argument("--strict-flags", action="store_true", help="Require all three startup settings in server_info")
    run.add_argument("--dry-run", action="store_true", help="Discover and validate, then print launch configuration without starting the agent")
    args_list = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in args_list:
        index = args_list.index("--")
        args_list, extra = args_list[:index], args_list[index + 1:]
    args = parser.parse_args(args_list)
    try:
        if args.max_output_tokens is not None and args.harness != "opencode":
            raise Error("--max-output-tokens currently applies only to opencode.")
        key = os.environ.get(args.api_key_env, "")
        if args.api_key_env != "VLLM_API_KEY" and not key:
            raise Error(f"Environment variable {args.api_key_env} is empty or unset.")
        if not args.dry_run and not shutil.which(args.harness):
            raise Error(f"{args.harness} is not installed or not on PATH. Install it before running vllmcode.")
        client = Client(key, args.timeout)
        server = discover(client, args.server, args.model)
        print(f"Server: {display(server.base)}\nModel: {display(server.model)}", flush=True)
        cmd, env = launch_config(args.harness, server, key, extra, max_output_tokens=args.max_output_tokens)
        if args.harness == "opencode":
            budget = env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"]
            print(f"Context: {server.context or 'unknown'} tokens; output budget: {budget}; compaction reserve: {budget}.", flush=True)
            if not server.context:
                print("Server did not advertise context length; OpenCode cannot determine when to auto-compact.", file=sys.stderr)
        validate_flags(client, server, args.strict_flags)
        print("Running small inference probes (no tools are executed) ...", flush=True)
        probe(client, server, args.harness, args.probe_timeout)
        if args.dry_run:
            print("Command: " + display(shlex.join(cmd)))
            # Only print keys we configure; never dump inherited environment.
            relevant = [k for k in env if k.startswith("ANTHROPIC_") or k == "OPENCODE_CONFIG_CONTENT"]
            if args.harness == "claude":
                relevant = [k for k in relevant if k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL") or k.startswith("ANTHROPIC_DEFAULT_")]
                for k in relevant:
                    print(f"{k}={display(env[k])}")
            elif args.harness == "opencode":
                config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                print("OpenCode provider: " + display(json.dumps(config["provider"]["vllmcode"])))
                print("OpenCode compaction: " + display(json.dumps(config["compaction"])))
                print("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=" + env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"])
            print("API key: " + (f"from {args.api_key_env} (redacted)" if key else "not required / placeholder for agent"))
            return 0
        print(f"Starting {args.harness} ...", flush=True)
        os.execvpe(cmd[0], cmd, env)
    except (Error, OSError) as exc:
        print(f"vllmcode: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
