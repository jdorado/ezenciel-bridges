"""OpenAI-compatible local proxy that serves completions via the Codex CLI.

This is a thin bridge intended to let existing OpenAI-compatible clients talk to a local
HTTP endpoint while the underlying "model" call is performed by spawning
`codex exec --json` and extracting the final agent message.

Design constraints:
- No fallbacks and no silent retries: one subprocess call per request.
- Keep request handling pass-through: only require `model` and `messages`; ignore other fields.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": message})


_REQUIRED_BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY")


def _bad_unauthorized(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": message})


def _require_bridge_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if not _REQUIRED_BRIDGE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "MISCONFIGURED_SERVER",
                "message": "BRIDGE_API_KEY environment variable is not set",
            },
        )

    provided = (x_api_key or "").strip()
    if not provided and authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        else:
            provided = auth

    if not provided:
        raise _bad_unauthorized(
            "Missing API key. Provide Authorization: Bearer <key> or X-API-Key: <key>."
        )

    if provided != _REQUIRED_BRIDGE_API_KEY:
        raise _bad_unauthorized("Invalid API key.")


def _iter_text_chunks(text: str, chunk_size: int = 96) -> Iterable[str]:
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]


@dataclass(frozen=True)
class _CodexExecResult:
    text: str
    raw_json_events: list[dict[str, Any]]


def _iter_json_objects_from_mixed_output(text: str) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from a stream that may contain non-JSON log lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _extract_last_agent_message(events: list[dict[str, Any]]) -> str | None:
    last: str | None = None
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            last = text
    return last


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _bad_request("INVALID_REQUEST", f"{key} must be a non-empty string")
    return value


def _require_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad_request("INVALID_REQUEST", "messages must be a non-empty list")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise _bad_request("INVALID_REQUEST", f"messages[{i}] must be an object")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            raise _bad_request("INVALID_REQUEST", f"messages[{i}].role must be a non-empty string")
        if "content" not in msg:
            raise _bad_request("INVALID_REQUEST", f"messages[{i}].content is required")
    return messages


def _optional_stream(payload: dict[str, Any]) -> bool:
    stream = payload.get("stream", False)
    if stream is False:
        return False
    if stream is True:
        return True
    raise _bad_request("INVALID_REQUEST", "stream must be a boolean")


def _build_prompt(messages: list[dict[str, Any]]) -> str:
    # Keep formatting deterministic and explicit.
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "")).upper()
        parts.append(f"{role}:\n{_normalize_message_content(msg.get('content')).strip()}")
    return "\n\n".join(parts).strip()


def _normalize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        inner = content.get("content")
        if isinstance(inner, str):
            return inner
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    if not isinstance(content, list):
        return "" if content is None else str(content)

    output: list[str] = []
    for block in content:
        if isinstance(block, str):
            output.append(block)
            continue
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                output.append(text)
            elif "content" in block and isinstance(block["content"], str):
                output.append(block["content"])
            else:
                output.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
            continue
        output.append(str(block))

    return "".join(output)


def _codex_exec_text(*, prompt: str, model: str, timeout_s: int = 180) -> _CodexExecResult:
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise RuntimeError("`codex` not found on PATH")

    cmd = [
        codex_path,
        "-a",
        "on-failure",
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
        model,
        prompt,
    ]

    proc = subprocess.run(
        cmd,
        cwd=os.getcwd(),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
    )
    output = proc.stdout or ""
    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-80:])
        raise RuntimeError(f"codex exec exited {proc.returncode}. Output tail:\n{tail}")

    events = list(_iter_json_objects_from_mixed_output(output))
    text = _extract_last_agent_message(events)
    if text is None:
        tail = "\n".join(output.splitlines()[-80:])
        raise RuntimeError(f"codex exec produced no agent_message event. Output tail:\n{tail}")

    return _CodexExecResult(text=text, raw_json_events=events)


def _stream_chat_completion(
    *, model: str, completion_text: str, completion_id: str, created: int
) -> Iterable[bytes]:
    index = 0

    for chunk in _iter_text_chunks(completion_text):
        delta: dict[str, object] = {"content": chunk}
        if index == 0:
            delta["role"] = "assistant"
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": None,
                }
            ],
        }
        index += 1
        yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    final_payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(final_payload)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def create_app() -> FastAPI:
    """Create a small FastAPI app exposing a subset of the OpenAI API surface."""
    app = FastAPI(title="Codex CLI OpenAI Proxy", version="0.1.0")

    @app.get("/health", dependencies=[Depends(_require_bridge_api_key)])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models", dependencies=[Depends(_require_bridge_api_key)])
    async def models() -> dict[str, Any]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5-codex",
                    "object": "model",
                    "created": now,
                    "owned_by": "openai",
                },
                {
                    "id": "gpt-5",
                    "object": "model",
                    "created": now,
                    "owned_by": "openai",
                },
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(_require_bridge_api_key)])
    async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
        model = _require_str(payload, "model").strip()
        messages = _require_messages(payload)
        stream = _optional_stream(payload)

        prompt = _build_prompt(messages)
        completion_id = f"chatcmpl_{uuid.uuid4().hex}"
        created = int(time.time())
        try:
            result = await asyncio.to_thread(_codex_exec_text, prompt=prompt, model=model)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "CODEX_EXEC_FAILED", "message": str(exc)},
            ) from exc

        if stream:
            return StreamingResponse(
                _stream_chat_completion(
                    model=model,
                    completion_text=result.text,
                    completion_id=completion_id,
                    created=created,
                ),
                media_type="text/event-stream",
            )

        # OpenAI-ish response.
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    return app


app = create_app()
