# ezenciel-bridges

Small, Docker-friendly "bridge" services used across ezenciel projects.

## codex-openai-proxy

OpenAI-compatible HTTP proxy that fulfills chat-completions by spawning the Codex CLI
(`codex exec --json`).

Endpoints:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`

Required environment:

- `BRIDGE_API_KEY` must be set to a strong secret shared by clients.
- `CODEX_EXEC_TIMEOUT_SECONDS` optional. Defaults to `1200` (20 minutes).
- `BRIDGE_BIND_ADDRESS` optional. Defaults to `127.0.0.1`; the trusted VM
  deployment sets it to `0.0.0.0` so authenticated remote clients can connect.

## Security and deployment

This bridge is intended only for trusted clients. Anyone with `BRIDGE_API_KEY` can submit
prompts to the authenticated Codex session, so treat that key as privileged access rather
than an ordinary application password.

- The supplied Compose configuration binds the service to `127.0.0.1` by default. Put it
  behind an authenticated reverse proxy or a private network if remote clients need access.
- Keep `BRIDGE_API_KEY`, the Docker `codex_config` volume, and any local `.env` files private.
  The volume contains the Codex login state and must never be committed, copied into images,
  or shared with untrusted users.
- The Codex subprocess intentionally receives only a small, non-secret environment; do not
  add credentials to that allowlist without a concrete runtime requirement.
- Report suspected vulnerabilities privately to the repository owner rather than in a public
  issue.

Run (Docker):

```sh
# One-time: writes auth into the `codex_config` Docker volume (persists across redeploys).
docker compose run --rm codex-openai-proxy codex login --device-auth

# Optional: verify login state
docker compose run --rm codex-openai-proxy codex login status

# Start proxy (export BRIDGE_API_KEY in your environment first).
docker compose up -d --build
# Example:
# curl -H "Authorization: Bearer $BRIDGE_API_KEY" http://127.0.0.1:8100/health
curl -sS -H "Authorization: Bearer $BRIDGE_API_KEY" http://127.0.0.1:8100/health
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

    // Must match BRIDGE_API_KEY configured on the proxy.
    api_key env.BRIDGE_API_KEY

    // This is forwarded to `codex exec --model <model>`.
    model "gpt-6-luna"
    reasoning_effort "max"
  }
}
```

Example OpenAI Python SDK:

```py
from openai import OpenAI
import os

client = OpenAI(
    base_url="http://127.0.0.1:8100/v1",
    api_key=os.environ["BRIDGE_API_KEY"],
)
resp = client.chat.completions.create(
    model="gpt-6-luna",
    reasoning_effort="max",
    messages=[{"role": "user", "content": "Say OK"}],
)
print(resp.choices[0].message.content)
```
