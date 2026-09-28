"""Regression tests for the Codex bridge subprocess contract."""

from __future__ import annotations

import subprocess
import sys
import types
import unittest
from unittest.mock import patch

if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def get(self, *args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator

        def post(self, *args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: object):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.Depends = lambda value=None: value
    fastapi.FastAPI = _FastAPI
    fastapi.Header = lambda default=None, alias=None: default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = type("Request", (), {})
    fastapi.Response = type("Response", (), {})
    sys.modules["fastapi"] = fastapi

if "fastapi.responses" not in sys.modules:
    fastapi_responses = types.ModuleType("fastapi.responses")
    fastapi_responses.StreamingResponse = type("StreamingResponse", (), {})
    sys.modules["fastapi.responses"] = fastapi_responses

import app


class TestCodexExecText(unittest.TestCase):
    def test_prompt_is_sent_via_stdin_instead_of_argv(self) -> None:
        prompt = "USER:\nhello\n" * 2000
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
        )

        with (
            patch("app.shutil.which", return_value="/usr/local/bin/codex"),
            patch("app.subprocess.run", return_value=completed) as run_mock,
        ):
            result = app._codex_exec_text(
                request_id="req-1",
                prompt=prompt,
                model="gpt-5.4",
                timeout_s=30,
            )

        self.assertEqual(result.text, "OK")
        _, kwargs = run_mock.call_args
        self.assertEqual(kwargs["input"], prompt)
        self.assertEqual(kwargs["cmd"] if "cmd" in kwargs else run_mock.call_args.args[0][-1], "-")
        self.assertNotIn(prompt, run_mock.call_args.args[0])

    def test_reasoning_effort_is_forwarded_to_codex_config(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
        )

        with (
            patch("app.shutil.which", return_value="/usr/local/bin/codex"),
            patch("app.subprocess.run", return_value=completed) as run_mock,
        ):
            app._codex_exec_text(
                request_id="req-2",
                prompt="hello",
                model="gpt-6-luna",
                reasoning_effort="max",
                timeout_s=30,
            )

        self.assertIn('-c', run_mock.call_args.args[0])
        self.assertIn('model_reasoning_effort="max"', run_mock.call_args.args[0])

    def test_codex_child_environment_excludes_bridge_key(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BRIDGE_API_KEY": "not-for-child-process",
                "HOME": "/home/app",
                "PATH": "/usr/local/bin:/usr/bin",
                "LANG": "C.UTF-8",
            },
            clear=True,
        ):
            child_env = app._codex_child_env()

        self.assertEqual(
            child_env,
            {
                "HOME": "/home/app",
                "PATH": "/usr/local/bin:/usr/bin",
                "LANG": "C.UTF-8",
            },
        )
        self.assertNotIn("BRIDGE_API_KEY", child_env)


if __name__ == "__main__":
    unittest.main()
