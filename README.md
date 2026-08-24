# GISELL Router

**Gisell Is a Simple and Easy LLM Router**  
Version **0.3.0** · © 2026 **Davide (gat)** · **CC BY-NC 4.0** License

GISELL Router is a local LLM router with an OpenAI-compatible API. It lets you configure multiple providers and models, order them in a global failover chain, and create model presets that are tried sequentially when the router reaches that preset.

The Web UI is available in **Italian and English**, shows live router status and session logs, manages providers, models and presets, and can install GISELL Router as a **Linux user systemd service**.

## What's new in version 0.3.0

- IT/EN Web UI with an `IT` / `EN` language button.
- Presets visually distinguished in the global order.
- Author, copyright, version and license information in the Web UI.
- Linux service installation/removal directly from the Web UI.
- Optional `systemd linger` support for boot startup before login.
- Preset routing aligned with the global list semantics.
- A model succeeding inside a preset **does not replace the preset as the active route**.
- If a model appears inside a preset and later again in the global list, it can be tried again when routing reaches that global position.
- Unnecessary Python comments removed from the source.

## Requirements

- Python **3.10 or newer**.
- `pip`.
- Service feature: Linux with `systemd` and `systemctl --user`.
- ChatGPT/Codex login: Codex CLI installed and authenticated with `codex login`.

Python dependencies:

```bash
pip install fastapi uvicorn httpx pydantic orjson
```

`orjson` accelerates JSON serialization. The router can work without it, but it is part of the recommended installation.

## Main files

```text
router.py          router backend and OpenAI-compatible API
webui.html         Web UI markup, styles and client logic
lang_it.json       all Italian user-facing text
lang_en.json       all English user-facing text
config.json        providers, models, presets, global order and settings
secrets.json       provider API keys and local router key (created locally)
README.md          documentation
LICENSE            Creative Commons CC BY-NC 4.0 license
```

`config.json`, `webui.html`, `lang_it.json`, and `lang_en.json` must live in the same directory as `router.py`. `secrets.json` is **not distributed**: it is created automatically on first use when needed and must remain local. It is included in `.gitignore` to reduce the risk of publishing credentials.

## Quick start

The simplest Linux startup is:

```bash
chmod +x run.sh
./run.sh
```

`run.sh` creates `.venv`, installs dependencies from `requirements.txt` on first launch, and starts the router. You can also create and manage the virtual environment manually.

Defaults:

```text
Web UI:   http://127.0.0.1:8765
API base: http://127.0.0.1:8765/v1
```

The router is designed to remain local. Do not expose it directly to the Internet without appropriate security controls.

## First launch: empty configuration

The public release intentionally starts with **zero configured providers, zero models, and zero presets**. `config.json` contains only the router's general settings.

Open the Web UI and use **Configuration** to:

1. add a provider using one of the available templates or a custom OpenAI-compatible endpoint;
2. enter an API key when required;
3. add one or more models;
4. optionally create presets and arrange the global failover chain.

API keys are stored in `secrets.json`, not in `config.json`. With no providers/models configured, the application starts normally; `/v1/models` exposes only the virtual `router` model (when enabled), while completion requests return a clear error because no backend is enabled.

## Web UI

The Web UI has four main areas.

### Models

Shows:

- router status;
- active route;
- actual model currently serving requests;
- session request count;
- errors;
- global failover order;
- presets with a distinct background;
- model health and latency;
- controls to reorder and select items.

The `EN` / `IT` button at the top switches the Web UI language. The preference is stored in the browser.

### Configuration

Manages:

- providers;
- Base URLs;
- API keys;
- models;
- labels;
- presets;
- preset membership and order;
- Linux systemd service.

### API

Shows:

- local Base URL;
- `curl` example;
- Python OpenAI SDK example;
- local API key status;
- key creation, regeneration and removal.

### Logs

Live session log for:

- requests;
- model attempts;
- failovers;
- successes;
- provider/model errors;
- latency.

Web UI logs are session-only and kept in memory.

## Supported providers

The UI provides templates for:

- OpenRouter
- Groq
- OpenAI
- Google AI Studio
- Mistral
- Together
- DeepInfra
- Fireworks AI
- Cerebras
- local or cloud Ollama
- Codex / ChatGPT OAuth login
- custom OpenAI-compatible endpoints

Multiple providers of the same type can be added.

## Models and routes

A model is represented by a route similar to:

```json
{
  "id": "<route-id>",
  "provider_id": "<provider-id>",
  "model": "provider/model-id",
  "label": "My model",
  "enabled": true
}
```

- `id`: stable internal identifier.
- `provider_id`: provider used by the route.
- `model`: model ID sent upstream.
- `label`: readable name and basis for the ID exposed by `/v1/models`.
- `enabled`: enables or disables the route.

## Presets

A preset is **one item in the global list**, just like a model, but contains an ordered list of routes to try.

Example:

```json
{
  "id": "<preset-id>",
  "kind": "preset",
  "label": "My preset",
  "members": [
    "<route-id-1>",
    "<route-id-2>"
  ],
  "enabled": true
}
```

Presets can also contain other presets. Cycles are rejected.

The published configuration contains no predefined presets; users create them from the Web UI.

## Exact failover semantics

The global list is processed from top to bottom, starting at the active item.

Example:

```text
1. MODEL 1
   └─ fails

2. PRESET 1
   ├─ model A → fails
   └─ model B → fails
      preset exhausted

3. MODEL 2
   └─ fails

4. MODEL 3
   └─ fails

5. PRESET 2
   ├─ model C → fails
   └─ model D → success
      STOP
```

Rules:

1. A global model is tried once when routing reaches that global position.
2. When routing reaches a preset, its members are tried in preset order.
3. The first successful model returns the response and stops the chain.
4. If every preset member fails, routing continues with the next global item.
5. A success inside a preset does not automatically change `active_route_id`.
6. The active route is the **selected global starting point**, not the last successful backend.
7. If a route is a preset member and also appears later as a global item, that global position remains valid and can be tried again.
8. Presets do not rearrange the global list. They are expanded only when reached.

At startup the router sets the first entry in `routes` as the active route.

## Cooldown

When a model fails, it is temporarily put into cooldown. Default:

```json
"cooldown_s": 30
```

Routes in cooldown are temporarily skipped when other alternatives are ready. If every route is in cooldown, the router retries them rather than immediately returning an error.

## Context reduction during failover

To avoid sending a very large conversation to every fallback backend, later attempts may use a reduced context:

```json
"context_messages_on_failover": 10
```

`system` and `developer` messages are preserved. The router also avoids cutting the conversation in the middle of a tool-call block.

## Virtual `router` model

Clients can use:

```json
"model": "router"
```

GISELL then follows the active item and the configured global failover chain.

`/v1/models` can also expose individual models and presets, with IDs derived from their labels.

## Explicit model requests

Setting:

```json
"failover_on_explicit_model": true
```

controls whether, after an explicitly requested model or preset fails, routing continues with the following global items.

Setting:

```json
"override_client_model": false
```

when set to `true`, always forces the starting item selected in the Web UI and ignores a specific model requested by the client.

## OpenAI-compatible API

Main endpoints:

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/responses
```

Example:

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "router",
    "messages": [{"role":"user","content":"Hello"}]
  }'
```

## OpenAI SDK usage

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="not-required",
)

response = client.chat.completions.create(
    model="router",
    messages=[{"role": "user", "content": "Hello"}],
)

print(response.choices[0].message.content)
```

If you enabled the local API key in the Web UI, use that value as `api_key`.

## Local API key

The local key protects `/v1/*` endpoints.

It can be created, regenerated or removed from the **API** tab.

When enabled, clients must send:

```text
Authorization: Bearer <key>
```

The key is stored in `secrets.json`, not `config.json`.

## Codex / ChatGPT OAuth

GISELL can reuse authentication created by the Codex CLI.

First run:

```bash
codex login
```

Default path:

```text
~/.codex/auth.json
```

A custom path can be configured in the Web UI.

This provider uses the Codex backend associated with the ChatGPT account. That endpoint is not a stable public API and may change over time.

The router handles:

- token loading;
- OAuth refresh;
- `chat/completions` → Responses translation;
- Responses stream → Chat Completions translation;
- tool calls.

## Google AI Studio

The Google AI Studio template uses Gemini's OpenAI-compatible endpoint. Enter a Gemini API key and configure model IDs supported by your account.

## Ollama

The Ollama template defaults to:

```text
http://127.0.0.1:11434/v1
```

It can be used with local models or compatible Ollama endpoints configured by the user.

## Installing as a Linux service

The **Configuration** tab contains a **Linux autostart** section.

### Install service

The button creates:

```text
~/.config/systemd/user/gisell-router.service
```

and enables it with:

```bash
systemctl --user enable gisell-router.service
```

The generated unit uses:

- the Python interpreter currently running GISELL;
- the absolute path of the currently running `router.py`;
- the router directory as `WorkingDirectory`.

**Important:** move GISELL to its final directory before installing the service. If you later move the script or its virtual environment, reinstall the service.

### Boot before login

A user systemd service normally starts when the user's systemd manager starts, usually at login.

To start it at boot even before login, the Web UI can enable `linger`, equivalent to:

```bash
loginctl enable-linger "$USER"
```

Disable it with:

```bash
loginctl disable-linger "$USER"
```

Some distributions may require PolicyKit authorization. If the Web UI operation is denied, run the command manually in a terminal.

### Useful systemd commands

```bash
systemctl --user status gisell-router.service
systemctl --user start gisell-router.service
systemctl --user stop gisell-router.service
systemctl --user restart gisell-router.service
journalctl --user -u gisell-router.service -f
```

### Removal

The **Remove service** button disables the service and removes its `.service` file.

The currently running GISELL process is not forcibly terminated by the HTTP removal request.

## Main settings

Example:

```json
{
  "host": "127.0.0.1",
  "port": 8765,
  "request_timeout_s": 90,
  "first_token_timeout_s": 45,
  "connect_timeout_s": 10,
  "cooldown_s": 30,
  "context_messages_on_failover": 10,
  "failover_on_explicit_model": true,
  "expose_virtual_router_model": true,
  "override_client_model": false
}
```

### `request_timeout_s`

Total upstream request timeout.

### `first_token_timeout_s`

For streaming requests, maximum time to receive a useful first event before treating the backend as failed and trying the next one.

### `connect_timeout_s`

Provider connection timeout.

### `cooldown_s`

How long a recently failed route is avoided when alternatives are available.

### `context_messages_on_failover`

Maximum number of conversational messages kept for attempts after the first.

### `expose_virtual_router_model`

When `true`, `/v1/models` exposes the virtual `router` model.

## Streaming

Streaming requests are supported.

Before sending bytes to the client, the router waits for the first valid SSE event. If the backend fails before that point, failover can occur.

After the first chunk has been sent to the client, the router **does not switch models for that response**, preventing a stream from being composed of output from multiple backends.

## Status and statistics

The Web UI uses local management endpoints to show:

- model currently in use;
- provider;
- in-flight requests;
- active route;
- uptime;
- request count;
- failures;
- average latency;
- last use.

Management endpoints are intended for the local Web UI.

## Security

Recommendations:

1. Keep `host` on `127.0.0.1` unless network access is required.
2. Never publish `secrets.json`.
3. Never publish provider API keys.
4. Enable the local API key when other processes or users can reach the router port.
5. Do not expose the Codex/ChatGPT backend to untrusted networks.
6. If using a reverse proxy, configure authentication and TLS.

`secrets.json` is written with restrictive permissions when supported by the filesystem.

## Troubleshooting

### `HTTP 429 usage_limit_reached`

The provider has reached a usage limit. The router marks that route as failed and tries the next backend.

### Codex model not supported with a ChatGPT account

If you receive an error such as:

```text
The '<model>' model is not supported when using Codex with a ChatGPT account
```

that model is not available through your Codex login. Disable/remove the route or use a supported model.

### `systemctl --user` does not work

Check:

```bash
systemctl --user status
```

If no user systemd session exists, the service feature cannot be used in that session.

### Service does not start

Check:

```bash
systemctl --user status gisell-router.service
journalctl --user -u gisell-router.service -n 100
```

Common causes:

- `router.py` was moved;
- the virtual environment was moved or deleted;
- port `8765` is already in use;
- Python dependencies are missing;
- `config.json` is not in the expected directory.

### Port already in use

```bash
ss -ltnp | grep 8765
```

Change the port in `config.json` if needed.

## License

GISELL Router is released under the **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** license.

The license permits sharing and adaptation for non-commercial purposes, provided proper attribution is given, changes are indicated, and the license reference is retained. Commercial use is not granted by CC BY-NC 4.0 and requires a separate agreement with the author.

Because it contains the NonCommercial restriction, **CC BY-NC 4.0 is not an OSI-approved open-source license**. Creative Commons also recommends against using CC licenses for software; this choice is intentional for GISELL Router.

See [LICENSE](LICENSE).
