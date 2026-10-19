# AI assistant setup — providers, keys and endpoints

The browser assistant on `#/assistant` is a **read-only** diagnostics copilot. It
can read this tool's stored data and, only when you allow it, the vehicle bus. It
never writes. You bring your own key (or run a local model); nothing is sent
anywhere until you configure a provider.

!!! info "Privacy defaults"
    No provider is configured out of the box. "Send bus data to a remote model"
    is **off**, VIN redaction is **on**, and conversations are not saved. A local
    model (Ollama, LM Studio, vLLM) keeps every byte on this machine.

## Providers you can pick

`GET /api/ai/providers` returns this table; the settings form is rendered from it.

| Provider id | Label | Default base URL | Key required | Local | Example models |
|---|---|---|---|---|---|
| `openai` | OpenAI | `https://api.openai.com/v1` | yes | no | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1`, `o4-mini` |
| `anthropic` | Anthropic (Claude) | `https://api.anthropic.com` | yes | no | `claude-3-5-sonnet-latest`, `claude-3-5-haiku-latest` |
| `gemini` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | yes | no | `gemini-2.0-flash`, `gemini-2.5-flash`, `gemini-2.5-pro` |
| `ollama` | Ollama (local) | `http://127.0.0.1:11434/v1` | no | yes | `llama3.2`, `qwen2.5`, `mistral`, `phi4` |
| `lmstudio` | LM Studio (local) | `http://127.0.0.1:1234/v1` | no | yes | `local-model` |
| `vllm` | vLLM (self-hosted) | `http://127.0.0.1:8000/v1` | no | yes | *your served model* |
| `openai_compat` | OpenAI-compatible endpoint | *(you supply it)* | no | no | *any* |
| `fake` | Offline deterministic | — | no | yes | `fake-1` |

Every hosted provider speaks either the OpenAI-compatible format (`openai`,
`gemini`, `ollama`, `lmstudio`, `vllm`, `openai_compat`) or the native Anthropic
Messages API (`anthropic`); `fake` never touches the network.

## The setup flow

1. Open **Settings → Assistant provider**.
2. Pick a provider. The **model** and **base URL** fields prefill from the preset
   (OpenAI → `gpt-4o-mini` at `https://api.openai.com/v1`, Ollama →
   `llama3.2` at `http://127.0.0.1:11434/v1`, …). Change them if you need to.
3. For a hosted provider, paste the key in **API key** and press **Save key**.
   The key is write-only: it is stored `0600` and only a hint (`…f3a2`) is shown.
   A local server needs no key.
4. Press **Test connection**. This calls `POST /api/ai/test`, which validates the
   settings and probes the endpoint (a model list, or a one-token ping if the
   server exposes no model list) **without starting a chat**. It never returns the
   provider's response body.
5. Press **Save AI settings**. The toggles below control privacy and spend.
6. Only then does `#/assistant` have a provider to talk to.

### Toggles

| Setting | Default | Meaning |
|---|---|---|
| Send bus data to a remote model | off | Required before any vehicle data can reach a non-local provider. |
| Redact VIN before egress | on | Replaces the VIN (ASCII and F190 hex) with a stable pseudonym. |
| Allow the assistant to read the live bus | off | Live reads need this **and** an idle bus and a per-turn cap. |
| Save conversations locally (opt-in) | off | Persists turns to the local SQLite DB (never to a provider). |
| Local models only | off | Refuses any remote provider outright. |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ai/providers` | Preset registry with requirements, defaults and key state. |
| GET | `/api/ai/config` | Current configuration (never a key). |
| PUT | `/api/ai/config` | Partial update; an explicit `null` clears `provider`/`model`/`base_url`. |
| POST | `/api/ai/keys` | Set a key (write-only; returns a masked hint). |
| DELETE | `/api/ai/keys/{provider}` | Remove a key (takes effect immediately). |
| POST | `/api/ai/test` | Validate + probe the configuration without a chat. |
| POST | `/api/ai/chat` | Stream a turn (SSE). |
| POST | `/api/ai/tasks/{name}` | `diagnose`/`explain`/`report`/`compare`/`pretrip` (SSE). |

All state-changing or spending routes (`/api/ai/config`, `/api/ai/keys`,
`/api/ai/test`, `/api/ai/chat`, `/api/ai/tasks/*`) require the CSRF header
`X-Pacifica-Request: 1` and a same-origin `Origin`.

`POST /api/ai/test` body (all fields optional — they are transient overrides so
the form can test before saving):

```json
{ "provider": "ollama", "model": "llama3.2",
  "base_url": "http://127.0.0.1:11434/v1", "probe": true }
```

It answers `200` with a structured result; `probe: false` validates the settings
only and opens no socket:

```json
{ "ok": true, "provider": "ollama", "local": true, "model": "llama3.2",
  "base_url": "http://127.0.0.1:11434/v1", "requires_key": false,
  "checks": [{"name": "configuration", "ok": true, "detail": "…"},
             {"name": "key", "ok": true, "detail": "not required for this provider"},
             {"name": "endpoint", "ok": true, "detail": "reachable; 3 model(s)"}],
  "models": ["llama3.2", "qwen2.5", "mistral"], "error": null }
```

## Base-URL rules

* A remote endpoint must be `https://`.
* `http://` is allowed **only** for a loopback host (`127.0.0.1`, `localhost`,
  `::1`, `0.0.0.0`).
* The URL must include a host, and must **not** embed credentials
  (`https://user:pass@host` is refused — put the key in the key field). A
  link-local/metadata address (`169.254.x.x`, e.g. `169.254.169.254`) is refused.
* `base_url` never contains a key, so it is safe to show back to the browser.
  (If a URL carries a query-string credential, the configured key values are
  masked before it is returned.)

## Environment variables

Values in the environment win over the saved file, for CI and one-off runs.

| Variable | Effect |
|---|---|
| `PACIFICA_OBD_AI_PROVIDER` | Provider id (any preset id above). |
| `PACIFICA_OBD_AI_MODEL` | Model id. |
| `PACIFICA_OBD_AI_BASE_URL` | Base URL override. |
| `PACIFICA_OBD_AI_OFFLINE=1` | Force the `fake` provider (never contacts a network). |

A key can come from the environment instead of the file. The generic name is
`PACIFICA_OBD_AI_KEY_<PROVIDER>` (upper-cased id), and a vendor-standard name is
also honoured:

| Provider | Environment key names honoured |
|---|---|
| `openai` | `PACIFICA_OBD_AI_KEY_OPENAI`, `OPENAI_API_KEY` |
| `anthropic` | `PACIFICA_OBD_AI_KEY_ANTHROPIC`, `ANTHROPIC_API_KEY` |
| `gemini` | `PACIFICA_OBD_AI_KEY_GEMINI`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| `ollama` | `PACIFICA_OBD_AI_KEY_OLLAMA` |
| `lmstudio` | `PACIFICA_OBD_AI_KEY_LMSTUDIO` |
| `vllm` | `PACIFICA_OBD_AI_KEY_VLLM` |
| `openai_compat` | `PACIFICA_OBD_AI_KEY_OPENAI_COMPAT`, `PACIFICA_OBD_AI_KEY_OPENAI` |
| `fake` | `PACIFICA_OBD_AI_KEY_FAKE` |

## Where the key lives

`$PACIFICA_OBD_HOME/ai.json` (else `$XDG_CONFIG_HOME/pacifica-obd`, else
`~/.config/pacifica-obd`), written atomically with mode `0600` and a `0700`
parent. A file readable by other users is **refused**, not silently used.

The key is never logged, never returned by the API, never written to a report or
the diagnostic store, and never emitted on the SSE stream: a logging filter
rewrites key-shaped text (and any configured literal) before it reaches a
handler, and the chat engine scrubs every outbound token, tool payload and error
before serialising it.
