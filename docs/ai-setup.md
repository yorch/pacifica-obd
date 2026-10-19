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
2. Pick a provider (the provider pills, or the provider picker). The **model** and
   **base URL** fields prefill from the preset (OpenAI → `gpt-4o-mini` at
   `https://api.openai.com/v1`, Ollama → `llama3.2` at
   `http://127.0.0.1:11434/v1`, …). Change them if you need to.
3. For a hosted provider, paste the key in **API key** and press **Save key**.
   The key is write-only: it is stored `0600` and only a hint (`…f3a2`) is shown.
   **Saving a key also selects that provider when no provider is selected yet**,
   and fills `model`/`base_url` from the preset defaults — but only fields that
   are still empty, so a model or endpoint you typed is never clobbered. This is
   the one-step path: a saved key is immediately usable.
4. If a **different** provider is already selected, saving a key does *not*
   switch it. The key is stored and the response says so:
   `{"selected": false, "reason": "another provider is configured"}`. The screen
   offers **Use <provider> instead**, which calls `POST /api/ai/select` — a
   single explicit selection. Nothing is switched silently.
5. Press **Test connection** (optional). This calls `POST /api/ai/test`, which
   validates the settings and probes the endpoint (a model list, or a one-token
   ping if the server exposes no model list) **without starting a chat**. It never
   returns the provider's response body.
6. Press **Save AI settings** only to change the privacy/spend toggles or to edit
   the model or base URL by hand. It is not required to make a saved key usable.

### Setup states

A key being stored and a provider being configured are **two different facts**.
`GET /api/ai/config` reports which one is missing with an explicit `state`, so the
assistant and Settings can say the specific thing that is wrong instead of a
generic "no provider configured":

| `state` | Meaning | What the screen offers |
|---|---|---|
| `unconfigured` | No provider and no key saved. | "No provider configured…" and a link to Settings. |
| `key_without_provider` | A key is saved (file **or** environment) but no provider is selected, so the key is present and useless. | "An Anthropic key is saved, but no provider is selected" plus a one-click **Use Anthropic**. |
| `provider_without_key` | A provider is selected that requires a key, and none is saved for it. | "Anthropic is selected but has no key — add one." |
| `ready` | A provider is selected, its key situation is complete, and the configuration validates. | The local-model note, or the remote-consent warning. |
| `invalid` | A provider is selected but the saved configuration cannot be used (unknown provider, missing model, bad base URL or cap). `state_reason` is a fixed phrase — never a value from the file or the request. | "The saved AI configuration is incomplete — …" and a link to Settings. |

The payload also carries the ids needed to act on that state without guessing:

```json
{ "provider": null, "state": "key_without_provider",
  "saved_key_providers": ["anthropic"], "suggested_provider": "anthropic",
  "keys": { "anthropic": { "configured": true, "hint": "…1234", "source": "file" } },
  "defaults": { "base_urls": { "anthropic": "https://api.anthropic.com", "…": "…" },
                "models":    { "anthropic": "claude-3-5-sonnet-latest", "…": "…" } } }
```

* `saved_key_providers` includes keys supplied by the environment, not just the
  file; a key that only exists as `ANTHROPIC_API_KEY` still produces
  `key_without_provider` and can be selected with one click.
* `suggested_provider` is the id a "Use <provider>" action should select.
* `defaults.models` / `defaults.base_urls` let a screen decide whether a
  one-click select can succeed (a custom endpoint with no default model needs the
  model field first) without hard-coding a preset.

`POST /api/ai/keys` keeps its original `provider` + `key` fields and adds
`selected` and `reason`:

```json
{ "provider": "anthropic",
  "key": { "configured": true, "hint": "…1234", "source": "file" },
  "selected": true, "reason": null }
```

`reason` is a fixed phrase (for example `another provider is configured` or
`a model id is required before this provider can be used`) — never a value from
the request or the file, so a key can never come back through it. No endpoint
returns the key value — only a masked hint (`…f3a2`) — and every response and SSE
payload is scrubbed for the configured literals before it is written, because an
absolute "never returns the key" claim was false on one URL-echoing path before
that path was fixed.

`POST /api/ai/select` takes only `{"provider": "anthropic"}`. Selecting the
provider that is already configured, or the first selection when none was
configured, preserves a `model`/`base_url` already saved and fills only the
gaps; switching from another provider replaces **both** with the new preset's
defaults (so the old provider's model or endpoint is never reused). Only the id
is sent, so a value redacted on the way out can never be written back as
configuration.

No write path (`POST /api/ai/keys`, `POST /api/ai/select`, `PUT /api/ai/config`)
copies an environment override into the file: `PACIFICA_OBD_AI_PROVIDER`,
`..._MODEL` and `..._BASE_URL` stay runtime overrides.

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
| GET | `/api/ai/config` | Current configuration (never a key), plus the setup `state`. |
| PUT | `/api/ai/config` | Partial update; an explicit `null` clears `provider`/`model`/`base_url`. |
| POST | `/api/ai/select` | Select a provider explicitly (the "Use <provider>" action); fills only empty preset defaults. |
| POST | `/api/ai/keys` | Set a key (write-only; returns a masked hint, plus `selected`/`reason`). |
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

The key is not logged, not written to a report or the diagnostic store, and not
emitted on the SSE stream: a logging filter rewrites key-shaped text (and any
configured literal, exception text included) before it reaches a handler, and the
chat engine scrubs every outbound token, tool payload and error — the structured
value before it is serialised, not the JSON string after. No endpoint returns the
key value; responses carry a masked hint.
