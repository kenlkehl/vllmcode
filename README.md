# vllmcode

Launch Codex, Claude Code, OpenCode, or Pi against an **already running, reachable vLLM server**. Python 3.10+, no runtime dependencies. Install the coding harness separately and put it on `PATH`.

```bash
pipx install .
# Or: uv tool install .
# Or, without installing: ./bin/vllmcode run codex sn4622130540

vllmcode run codex sn4622130540
vllmcode run claude http://sn4622130540:8000/v1
vllmcode run opencode sn4622130540:8000
vllmcode run pi sn4622130540:8000
```

You can also run `python3 -m vllmcode` from this directory. The launcher prints the chosen endpoint and model ID, checks compatibility, and replaces itself with the harness, preserving terminal input, signals, working directory, and exit status. It does not edit your harness configuration files or start/restart vLLM.

## Server addresses

These explicit-port forms are equivalent:

```text
hostname:8000
http://hostname:8000
hostname:8000/v1
http://hostname:8000/v1/
```

Without a port, it tries **8000, 8001, 8002, 8020, 8030**, in that order. If none respond with a model list, it also tries the scheme's default port (80 for HTTP, 443 for HTTPS), for reverse proxies. An explicit port is authoritative: the launcher does not scan other ports. Specify `https://host:443` to use a TLS proxy immediately. An omitted scheme means HTTP; there is no HTTPS-to-HTTP downgrade.

HTTPS certificate validation stays enabled. Bracket IPv6 addresses (`[::1]:8000`). Proxy path prefixes are preserved (`https://host:443/inference/v1`). Redirects are rejected; use the final endpoint URL. Credentials in URLs, query strings, and fragments are rejected.

The first working `/v1/models` determines the endpoint. A single base model is preferred over LoRA adapters. If several base models exist, choose one explicitly:

```bash
vllmcode run codex host --model 'organization/model-id'
```

Authentication failures stop discovery so an incorrect key does not silently send the user to another server.

## Authentication and agent arguments

```bash
read -rs VLLM_API_KEY
export VLLM_API_KEY
vllmcode run claude host

# Use a key already stored under another environment variable:
vllmcode run codex host --api-key-env MY_VLLM_KEY

# Launcher options precede --; everything after it goes to the harness verbatim.
vllmcode run codex host -- exec 'Explain this repository'
vllmcode run claude host -- -p 'Explain this repository'
vllmcode run opencode host -- run 'Explain this repository'
vllmcode run pi host -- -p 'Explain this repository'
```

Keys go through the child environment, never command-line arguments or configuration files. With no key set, discovery/probes are unauthenticated and harnesses receive a placeholder. Existing cloud credentials are not used as vLLM credentials. Normal harness permissions remain in effect. Explicit harness options after `--` can override launcher settings; avoid supplying conflicting model/provider options unless intentional.

## One-shot terminal commands

The same launcher supports noninteractive invocations. Put launcher options before `--` and the harness's native command and options after it:

```bash
vllmcode run codex sn4622130540 -- exec 'Explain this repository'
vllmcode run claude sn4622130540 -- -p 'Explain this repository'
vllmcode run opencode sn4622130540 -- run 'Explain this repository'
vllmcode run pi sn4622130540 -- -p 'Explain this repository'

# Capture the harness's native JSON output (the formats differ between harnesses).
vllmcode run codex host -- exec --json 'Review this code' > events.jsonl
vllmcode run claude host -- -p 'Review this code' --output-format json > result.json
vllmcode run opencode host -- run --format json 'Review this code' > events.jsonl
vllmcode run pi host -- -p --mode json 'Review this code' > events.jsonl

# Combine launcher settings with a one-shot request.
vllmcode run opencode host --max-output-tokens 65536 -- run 'Explain this repository'
```

Discovery, model selection, and compatibility checks still run before each invocation. Launcher status (including the model ID) goes to **stderr**, leaving **stdout** for the harness's output. `--dry-run` prints its launch plan to stdout instead. Stdin, the working directory, and the harness's exit code pass through unchanged. Input piping and JSON formatting follow the selected harness's native behavior. One-shot mode does not bypass the harness's permissions or sandbox settings.

## Reasoning effort

Use `--effort` before the `--` separator for interactive or one-shot sessions:

```bash
vllmcode run codex sn4622130540 --effort xhigh
vllmcode run claude sn4622130540 --effort low -- -p 'Explain this repository'
vllmcode run opencode sn4622130540 --effort xhigh --max-output-tokens 65536 -- run 'Review this code'
```

All four accept `low`, `medium`, `high`, and `xhigh` at the launcher level. Codex, OpenCode, and Pi also accept `minimal`; Claude, OpenCode, and Pi also accept `max`. The server/model must support the selected value: the launcher forwards it exactly, without translating `max` to `xhigh` or silently lowering effort. These values are effort hints, not token counts; `--max-output-tokens` independently controls OpenCode's and Pi's total generation ceiling.

Codex receives `model_reasoning_effort` plus enabled reasoning metadata for custom model IDs. Claude receives `--effort`. Pi receives `--thinking` with an explicit map that preserves the selected effort value. OpenCode receives model `options.reasoningEffort`, which its compatible provider sends as `reasoning_effort`. Both the Chat Completions check and the harness-specific probe use the chosen level (`reasoning.effort` for Responses, `output_config.effort` for Messages). A server rejection blocks launch and reports the requested effort. A successful probe establishes request acceptance and tool behavior, not whether a model actually changes its reasoning depth.

Without `--effort`, Claude still uses `medium`; Codex and OpenCode keep their prior configuration/default behavior. Do not combine the first-class option with a native effort override after `--`; the launcher rejects those conflicting overrides. Explicit user/project agent settings or plugins can still alter subsequent requests. Startup diagnostics and the dry-run launch plan show the selected level. Probes remain capped at 1,024 output tokens, so a model that reasons too long can fail the tool-call check even with a valid effort level.

## Codex automatic approval review (experimental)

```bash
vllmcode run codex sn4622130540 --auto-review --effort xhigh
vllmcode run codex sn4622130540 --auto-review --effort xhigh -- exec 'Fix the failing tests'
```

Requires **Codex CLI 0.153.4 or newer**; 0.153.4 is the version tested. `--auto-review` enables Codex's native `approvals_reviewer="auto_review"`, `approval_policy="on-request"`, and `sandbox_mode="workspace-write"`. Eligible approval requests go to a separate Codex reviewer session using the **same vLLM endpoint, API key, and detected model**. Codex retains its review policy, sandbox enforcement, and decision/error handling. Actions permitted inside the sandbox do not necessarily need a review. This is permission review, distinct from `codex review` code review.

Codex selects its review model separately from its coding model. The launcher supplies a packaged, authoritative model catalog containing only a hidden, non-API placeholder. This excludes bundled/cached hosted reviewer models and makes Codex use its built-in fallback metadata and instructions for the active vLLM model, including reviewer selection. Codex requires a nonempty catalog, so the placeholder is necessary; it is never an inference model. The reserved ID `vllmcode-catalog-placeholder` cannot be used with this option. No global configuration or catalog files are edited. Codex may print its usual warning about fallback model metadata.

With this fallback metadata, an explicit launcher `--effort` also reaches the reviewer. Without it, Codex's effective effort/default applies. The detected context override is retained because the coding and review model IDs match. Explicit native model, provider, sandbox, or approval overrides that conflict with this mode are rejected. Use the launcher option instead of relying on native `--approve-for-me` alone to configure local reviewer selection. Existing managed restrictions still apply.

This integration depends on Codex's reviewer-selection behavior and may need adjustment for future releases. Startup checks verify version and inference/tool compatibility; they do **not** test a real approval on every launch or establish a model's ability to make sound permission decisions. Reviewer quality depends on your model. Claude, OpenCode, and Pi are not supported by `--auto-review`. Without this flag, the launcher leaves approval settings unchanged.

Implementation follows the [official OpenAI documentation for auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review) and the installed Codex version's native behavior.

Live validation on `sn4622130540:8000` with `Inferact/Qwen3.8-27B-NVFP4` and Codex 0.153.4 captured an actual Guardian request at the same `/v1/responses` endpoint, with the detected model ID, native review JSON schema, and `xhigh` effort. An explicitly escalated `pwd` command ran after review. In a second test, injected HTTP 503 responses for Guardian requests caused Codex to decline the command without executing it. **Codex exec still returned 0 after reporting the refusal**: automation that needs to detect declined actions should inspect `--json` events (`command_execution` status `declined`), not just the process exit code. These are routing and failure-handling tests, not a reviewer-quality evaluation.

## What is verified

The requested startup settings are interpreted as:

```text
--reasoning-parser <parser appropriate for the model>
--tool-call-parser <parser appropriate for the model>
--enable-auto-tool-choice
```

The launcher first requests `/server_info?config_format=json` and checks these named fields if exposed. An explicitly disabled or unset setting blocks launch.

**Many vLLM versions/deployments do not expose these CLI settings**, even when `/server_info` exists. Neither `/v1/models` nor an advertised OpenAPI route proves a parser is enabled. In that case, the default mode explicitly reports that the literal settings are unavailable and validates observable behavior instead:

1. Submit a short arithmetic prompt with `tool_choice: auto` to Chat Completions.
2. Require nonempty, separately parsed reasoning plus a structured dummy tool call with valid JSON arguments and the expected answer. The dummy tool is never executed.
3. For Codex and Claude, repeat the tool-call check using their actual API protocol.

Each run sends one small inference request for OpenCode or two for Codex/Claude, capped at 1,024 output tokens each. This consumes server inference capacity. Requests time out after 60 seconds each by default. A failed probe blocks launch and prints guidance. A probe failure can also mean the model ignored the prompt or exhausted the token budget; it is not proof that a particular CLI flag is missing. These checks establish observed behavior, not the exact startup command or compatibility with every agent feature.

If you need literal configuration evidence, use:

```bash
vllmcode run codex host --strict-flags
```

This refuses to launch unless all three settings are explicitly exposed in `/server_info` JSON. Parser names are model-specific; the launcher does not guess them or modify the server. Models that do not produce separated reasoning will not pass the requested reasoning requirement.

## Harness configuration

| Harness | API | Per-launch configuration |
| --- | --- | --- |
| Codex | `/v1/responses` | CLI `-c` overrides define a custom `vllmcode` provider, model, base URL, `wire_api="responses"`, and a dedicated key environment variable. |
| Claude Code | `/v1/messages` | `ANTHROPIC_BASE_URL` points to the server root, without `/v1`. Main, Opus, Sonnet, Haiku, and subagent model settings point to the discovered model. |
| Pi | `/v1/chat/completions` | A bundled extension registers a `vllmcode` provider using child environment configuration. `--provider`, `--model`, and `--thinking` select it without editing Pi configuration files. |
| OpenCode 1.x | `/v1/chat/completions` | `OPENCODE_CONFIG_CONTENT` adds an `@ai-sdk/openai-compatible` provider. Main and small models use the discovered ID. Only this provider is enabled for the run. |

The server's advertised `max_model_len`, when available, sets the harness context limit. Claude and Pi default to `medium`, also checked by their probes: Claude’s native default `high` was rejected by the tested model. Use the launcher `--effort <level>` to choose another supported value. Other user preferences remain available; existing inline OpenCode preferences are merged. Claude cloud-routing environment switches and OAuth overrides are cleared for the child process. Managed policies or explicitly conflicting user/project harness settings can still impose restrictions.

Codex needs vLLM Responses API support; Claude needs Anthropic Messages API support. There is no translating proxy. OpenCode configuration targets its current stable 1.x schema, not the separate v2 preview schema.

Pi support targets the current custom-provider extension API and is tested with Pi 0.85.1. Its existing settings, extensions, and session location remain available. The provider declares text input, reasoning, zero local token costs, and OpenAI compatibility settings for vLLM. When the server omits context length, Pi uses a reported 32,768-token fallback; this is an assumption, not a detected limit. Its default output budget is 32,768 tokens, capped at one quarter of the configured context. `--max-output-tokens` can override it, but must remain below that context. Pi retains its native compaction settings.

```bash
vllmcode run pi host --effort high --max-output-tokens 16384
vllmcode run pi host --dry-run
```

## Diagnostics and tests

OpenCode defaults to **32,768 output tokens**, including reasoning. For smaller known context windows, the default is reduced to one quarter of the context. Choose a larger budget explicitly when your model needs more reasoning time:

```bash
vllmcode run opencode sn4622130540 --max-output-tokens 65536
```

For OpenCode, this option sets both the model output limit and the child process's `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` (otherwise OpenCode 1.17.4 separately caps generation at 32,000). Automatic compaction is enabled, with `compaction.reserved` matching the selected output budget; other compaction preferences are preserved. With a 262,144-token context, the default leaves 229,376 tokens before the output reserve; a 65,536 budget leaves 196,608. Actual compaction timing depends on OpenCode's accounting. These are generation ceilings, not minimum response lengths or separately detected model output limits.

Explicit budgets must be positive integers smaller than the advertised context, leaving room for input. When the server omits context length, the output budget still applies, but OpenCode cannot calculate automatic compaction from an unknown context; the launcher prints that limitation. Startup output and `--dry-run` show the effective budget and reserve. Inference probes retain their small 1,024-token cap.

```bash
# Runs discovery and inference checks, prints redacted config, does not launch:
vllmcode run codex sn4622130540 --dry-run

# Longer timeouts for remote or busy servers:
vllmcode run claude host --timeout 5 --probe-timeout 120

python3 -m unittest discover -s tests -v
```

Tests use local HTTP fixtures and a fake executable to exercise discovery, authentication, capability failures, model ambiguity, redaction, redirects, argument forwarding, and process exit codes. Running them requires permission to bind loopback sockets. They do not require GPUs. An additional streaming integration test runs the installed Pi CLI against a local HTTP fixture when Pi is on PATH; otherwise it is skipped.

Live validation on September 6, 2026: `sn4622130540:8000`, vLLM 0.28.0, model `Inferact/Qwen3.8-27B-NVFP4`, advertised context 262,144. Codex 0.153.0, Claude Code 2.1.251, and OpenCode 1.17.4 each passed the launcher's probes and an isolated noninteractive reply-only run. Both streaming agent responses and the nonstreaming tool probes worked. This server did not expose `/server_info`; literal flag verification was unavailable. These were smoke tests, not a full coding benchmark or a multi-turn tool-execution test.

Pi live validation on September 7, 2026: Pi 0.85.1 against `sn4622130540:8000`, model `Inferact/Qwen3.8-27B-NVFP4`, advertised context 262,144, with medium effort and a 32,768-token output budget. Discovery and reasoning/tool probes passed. Two isolated noninteractive runs exited successfully: a streamed text response and a complete `read` tool round trip that returned the expected value from a temporary fixture file. User extensions and context files were disabled for these smoke tests; interactive UI behavior was not tested. Startup flags were not exposed, so verification covered observable behavior.

If discovery fails, check VPN/DNS access, firewall rules, and whether vLLM binds an accessible interface. Short cluster hostnames must resolve on the machine running `vllmcode`; use its FQDN/IP or an existing SSH tunnel if needed.

## Sources

Configuration was checked against these official documents on September 6, 2026:

- [Codex custom model providers](https://developers.openai.com/codex/config-advanced/)
- [vLLM Claude Code integration](https://docs.vllm.ai/en/latest/serving/integrations/claude_code/)
- [Claude Code effort configuration](https://code.claude.com/docs/en/model-config)
- [OpenCode providers](https://opencode.ai/docs/providers/)
- [OpenCode configuration precedence](https://opencode.ai/docs/config/)
- [vLLM server information implementation, v0.11](https://docs.vllm.ai/en/v0.11.0/api/vllm/entrypoints/openai/api_server.html#vllm.entrypoints.openai.api_server.show_server_info)

Pi configuration was checked on September 7, 2026 against the official [custom-provider documentation](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/custom-provider.md) and [CLI reference](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md#cli-reference).
