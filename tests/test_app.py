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

class TestModelContract(unittest.TestCase):
    def test_model_selects_cli_and_default_effort(self):
        self.assertEqual(app._model_options({'model': 'gpt-6.1-sol'}),
                         ('gpt-6.1-sol', 'codex', 'medium'))
        self.assertEqual(app._model_options({'model': 'claude-sonnet-5-5', 'reasoning_effort': 'high'}),
                         ('claude-sonnet-5-5', 'claude', 'high'))

    def test_invalid_combinations_are_rejected_before_execution(self):
        for payload in (
            {'model': 'unknown'},
            {'model': 'claude-sonnet-5-5', 'reasoning_effort': 'ultra'},
            {'model': 'gpt-6-luna', 'reasoning_effort': 'ultra'},
            {'model': 'gpt-6.1-sol', 'provider': 'claude'},
            {'model': 'gpt-6.1-sol', 'reasoning_effort': 'none'},
        ):
            with self.subTest(payload=payload), self.assertRaises(app.HTTPException) as error:
                app._model_options(payload)
            self.assertEqual(error.exception.status_code, 400)

    def test_schema_is_passed_to_codex_as_temporary_file(self):
        import json
        import pathlib
        schema = {'type': 'object', 'properties': {'reply': {'type': 'string'}}}
        schema_paths = []
        def reply(**kwargs):
            schema_paths.append(kwargs['schema_path'])
            self.assertEqual(json.loads(pathlib.Path(kwargs['schema_path']).read_text()), schema)
            return app._CodexExecResult('ok', [])
        with patch('app._codex_exec_text', side_effect=reply), patch('app._claude_exec_text') as claude:
            app._cli_reply(request_id='schema', prompt='hello', model='gpt-6.1-sol',
                           provider='codex', reasoning_effort='medium', schema=schema)
        claude.assert_not_called()
        self.assertFalse(pathlib.Path(schema_paths[0]).exists())

    def test_claude_native_schema_effort_and_isolation(self):
        import json
        schema = {'type': 'object', 'properties': {'reply': {'type': 'string'}}}
        completed = subprocess.CompletedProcess(['claude'], 0,
            stdout=json.dumps({'is_error': False, 'structured_output': {'reply': 'ok'}}))
        with patch('app.shutil.which', return_value='/usr/local/bin/claude'), \
             patch('app.subprocess.run', return_value=completed) as run:
            result = app._claude_exec_text(request_id='claude', prompt='private prompt',
                model='claude-sonnet-5-5', reasoning_effort='medium', schema=schema)
        self.assertEqual(json.loads(result.text), {'reply': 'ok'})
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--model') + 1], 'claude-sonnet-5-5')
        self.assertEqual(cmd[cmd.index('--effort') + 1], 'medium')
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertEqual(json.loads(cmd[cmd.index('--json-schema') + 1]), schema)
        self.assertIn('--no-session-persistence', cmd)
        self.assertNotIn('private prompt', cmd)
        self.assertEqual(run.call_args.kwargs['input'], 'private prompt')
        self.assertNotIn('BRIDGE_API_KEY', run.call_args.kwargs['env'])

    def test_claude_error_is_not_a_success_or_secret_disclosure(self):
        completed = subprocess.CompletedProcess(['claude'], 1, stdout='secret', stderr='secret')
        with patch('app.shutil.which', return_value='/usr/local/bin/claude'), \
             patch('app.subprocess.run', return_value=completed), \
             self.assertRaises(app.HTTPException) as error:
            app._claude_exec_text(request_id='error', prompt='hello',
                model='claude-sonnet-5-5', reasoning_effort='medium')
        self.assertNotIn('secret', str(error.exception.detail))

    def test_malformed_output_format_is_rejected(self):
        for response_format in ('json', {'type': 'json_object'}, {'type': 'json_schema'}):
            with self.assertRaises(app.HTTPException):
                app._output_schema({'response_format': response_format})
