"""OpenAI-compatible local proxy that serves completions via the Codex CLI.

This is a thin bridge intended to let existing OpenAI-compatible clients talk to a local
HTTP endpoint while the underlying "model" call is performed by spawning
`codex exec --json` and extracting the final agent message. Prompts are sent via stdin,
not argv, so large message payloads do not hit the OS argument-length limit.

Design constraints:
- No fallbacks and no silent retries: one subprocess call per request.
- Keep request handling pass-through: only require `model` and `messages`; ignore other fields.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": message})


_LOGGER = logging.getLogger("codex-bridge")
_CODEX_FAILURE_TAIL_LINES = 120
_CODEX_FAILURE_LOG_LIMIT = 2000
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})

_REQUIRED_BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY")
_CODEX_CHILD_ENV_NAMES = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "TZ")


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _LOGGER.warning("invalid_env_int name=%s raw=%s using_default=%s", name, raw, default)
        return default
    if value <= 0:
        _LOGGER.warning("non_positive_env_int name=%s raw=%s using_default=%s", name, raw, default)
        return default
    return value


_DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = _read_positive_int_env(
    "CODEX_EXEC_TIMEOUT_SECONDS",
    1200,
)


def _bad_unauthorized(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": message})


def _codex_child_env() -> dict[str, str]:
    """Return the minimum non-secret environment required by the Codex child process."""
    return {
        name: value
        for name in _CODEX_CHILD_ENV_NAMES
        if (value := os.getenv(name)) is not None
    }


def _truncate(text: str, *, max_chars: int = _CODEX_FAILURE_LOG_LIMIT) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...(+{len(text) - max_chars} chars)"


def _tail_text(text: str, *, max_lines: int = _CODEX_FAILURE_TAIL_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _codex_exec_http_error(
    *, code: str, request_id: str, model: str, detail_message: str, **extra: Any
) -> HTTPException:
    detail = {
        "code": code,
        "message": detail_message,
        "request_id": request_id,
        "model": model,
        "details": extra,
    }
    return HTTPException(status_code=500, detail=detail)


def _require_bridge_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if not _REQUIRED_BRIDGE_API_KEY:
        _LOGGER.error("bridge_api_key_missing")
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
        _LOGGER.warning("bridge_auth_missing")
        raise _bad_unauthorized(
            "Missing API key. Provide Authorization: Bearer <key> or X-API-Key: <key>."
        )

    if not hmac.compare_digest(
        provided.encode("utf-8"), _REQUIRED_BRIDGE_API_KEY.encode("utf-8")
    ):
        _LOGGER.warning("bridge_auth_invalid request_key_len=%s", len(provided))
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


def _optional_reasoning_effort(payload: dict[str, Any]) -> str | None:
    value = payload.get("reasoning_effort")
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().lower() not in _REASONING_EFFORTS:
        raise _bad_request(
            "INVALID_REQUEST",
            "reasoning_effort must be one of: " + ", ".join(sorted(_REASONING_EFFORTS)),
        )
    return value.strip().lower()


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


def _codex_exec_text(
    *,
    request_id: str,
    prompt: str,
    model: str,
    reasoning_effort: str | None = None,
    timeout_s: int = _DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
) -> _CodexExecResult:
    codex_path = shutil.which("codex")
    if codex_path is None:
        _LOGGER.error("codex_exec_missing request_id=%s model=%s", request_id, model)
        raise _codex_exec_http_error(
            code="CODEX_NOT_FOUND",
            request_id=request_id,
            model=model,
            detail_message="`codex` not found on PATH",
        )

    cmd = [
        codex_path,
        "-a",
        "never",
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
    ]
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.append("-")

    _LOGGER.info(
        "codex_exec_start request_id=%s model=%s reasoning_effort=%s timeout_s=%s prompt_chars=%s cmd=%s",
        request_id,
        model,
        reasoning_effort,
        timeout_s,
        len(prompt),
        " ".join(cmd[:-1]),
    )

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=os.getcwd(),
            env=_codex_child_env(),
            text=True,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
    except OSError as exc:
        _LOGGER.error(
            "codex_exec_oserror request_id=%s model=%s error=%s",
            request_id,
            model,
            str(exc),
        )
        raise _codex_exec_http_error(
            code="CODEX_EXEC_OS_ERROR",
            request_id=request_id,
            model=model,
            detail_message="Failed to run codex executable",
            error=str(exc),
            command=[cmd[0], "-a", "exec", "--model", model],
        ) from exc
    except subprocess.TimeoutExpired as exc:
        tail = _tail_text(exc.stdout if isinstance(exc.stdout, str) else "")
        _LOGGER.error(
            "codex_exec_timeout request_id=%s model=%s timeout_s=%s output_tail=%s",
            request_id,
            model,
            timeout_s,
            _truncate(tail, max_chars=600),
        )
        raise _codex_exec_http_error(
            code="CODEX_EXEC_TIMEOUT",
            request_id=request_id,
            model=model,
            detail_message=f"codex exec timed out after {timeout_s}s",
            returncode=None,
            timeout_s=timeout_s,
            tail=_truncate(tail),
        ) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    output = proc.stdout or ""
    tail = _tail_text(output)
    if proc.returncode != 0:
        _LOGGER.error(
            "codex_exec_failed request_id=%s model=%s returncode=%s elapsed_ms=%s output_tail=%s",
            request_id,
            model,
            proc.returncode,
            elapsed_ms,
            _truncate(tail),
        )
        raise _codex_exec_http_error(
            code="CODEX_EXEC_FAILED",
            request_id=request_id,
            model=model,
            detail_message=f"codex exec exited {proc.returncode}",
            returncode=proc.returncode,
            elapsed_ms=elapsed_ms,
            output_tail=tail,
            command=[cmd[0], "-a", "exec", "--model", model],
        )

    events = list(_iter_json_objects_from_mixed_output(output))
    if events:
        _LOGGER.info(
            "codex_exec_events request_id=%s model=%s event_count=%s last_event_type=%s",
            request_id,
            model,
            len(events),
            events[-1].get("type") if isinstance(events[-1], dict) else None,
        )
    text = _extract_last_agent_message(events)
    if text is None:
        _LOGGER.error(
            "codex_exec_no_agent_message request_id=%s model=%s event_count=%s output_tail=%s",
            request_id,
            model,
            len(events),
            _truncate(tail),
        )
        raise _codex_exec_http_error(
            code="CODEX_EXEC_NO_AGENT_MESSAGE",
            request_id=request_id,
            model=model,
            detail_message="codex exec produced no agent_message event",
            event_count=len(events),
            event_types=[e.get("type") for e in events if isinstance(e, dict)],
            output_tail=tail,
        )

    _LOGGER.info(
        "codex_exec_success request_id=%s model=%s elapsed_ms=%s event_count=%s completion_chars=%s",
        request_id,
        model,
        elapsed_ms,
        len(events),
        len(text),
    )

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
                    "id": "gpt-6-luna",
                    "object": "model",
                    "created": now,
                    "owned_by": "openai",
                },
                {
                    "id": "gpt-6-sol",
                    "object": "model",
                    "created": now,
                    "owned_by": "openai",
                },
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(_require_bridge_api_key)])
    async def chat_completions(
        payload: dict[str, Any], request: Request, response: Response
    ) -> dict[str, Any]:
        request_id = request.headers.get("x-request-id") or request.headers.get(
            "X-Request-ID"
        ) or str(uuid.uuid4())
        response.headers["X-Request-ID"] = request_id
        started = time.time()
        _LOGGER.info(
            "chat_completions_start request_id=%s model=%s stream=%s message_count=%s",
            request_id,
            payload.get("model"),
            payload.get("stream", False),
            len(payload.get("messages", [])),
        )

        model = _require_str(payload, "model").strip()
        messages = _require_messages(payload)
        stream = _optional_stream(payload)
        reasoning_effort = _optional_reasoning_effort(payload)

        prompt = _build_prompt(messages)
        completion_id = f"chatcmpl_{uuid.uuid4().hex}"
        created = int(time.time())
        try:
            result = await asyncio.to_thread(
                _codex_exec_text,
                request_id=request_id,
                prompt=prompt,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except HTTPException:
            _LOGGER.warning(
                "chat_completions_failed request_id=%s model=%s",
                request_id,
                model,
            )
            raise
        except Exception as exc:
            _LOGGER.exception(
                "chat_completions_unexpected request_id=%s model=%s error=%s",
                request_id,
                model,
                str(exc),
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "CODEX_EXEC_FAILED",
                    "message": str(exc),
                    "request_id": request_id,
                    "model": model,
                },
            ) from exc

        elapsed_ms = int((time.time() - started) * 1000)
        _LOGGER.info(
            "chat_completions_success request_id=%s model=%s elapsed_ms=%s completion_chars=%s",
            request_id,
            model,
            elapsed_ms,
            len(result.text),
        )
        if stream:
            return StreamingResponse(
                _stream_chat_completion(
                    model=model,
                    completion_text=result.text,
                    completion_id=completion_id,
                    created=created,
                ),
                media_type="text/event-stream",
                headers={"X-Request-ID": request_id},
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
