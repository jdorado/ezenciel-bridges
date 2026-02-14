# ezenciel-bridges

Small, Docker-friendly "bridge" services used across ezenciel projects.

## codex-openai-proxy

OpenAI-compatible HTTP proxy that fulfills chat-completions by spawning the Codex CLI
(`codex exec --json`).

Endpoints:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`

Run (Docker):

```sh
# One-time: writes auth into the `codex_config` Docker volume (persists across redeploys).
docker compose run --rm codex-openai-proxy codex login --device-auth

# Optional: verify login state
docker compose run --rm codex-openai-proxy codex login status

docker compose up -d --build
curl -sS http://127.0.0.1:8100/health
```

Notes:

- Login persists as long as you do not delete the volume (avoid `docker compose down -v`).

## Hooking Up BAML / OpenAI Clients

This proxy speaks the OpenAI `v1/chat/completions` shape, so you can point BAML's
`provider openai` (or the OpenAI Python SDK) at it via `base_url`.

Example `clients.baml`:

```baml
client<llm> LocalCodexProxy {
  provider openai
  options {
    base_url "http://127.0.0.1:8100/v1"

    // BAML requires an api_key field. The proxy ignores it, but it must be non-empty.
    api_key env.OPENROUTER_API_KEY

    // This is forwarded to `codex exec --model <model>`.
    model "gpt-5.3-codex"
  }
}
```

Example OpenAI Python SDK:

```py
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8100/v1", api_key="sk-dummy")
resp = client.chat.completions.create(
    model="gpt-5.3-codex",
    messages=[{"role": "user", "content": "Say OK"}],
)
print(resp.choices[0].message.content)
```
