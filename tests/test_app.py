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
    def test_live_web_access_keeps_read_only_sandbox_and_disables_shell(self):
        completed = subprocess.CompletedProcess(['codex'], 0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n')
        with patch('app.shutil.which', return_value='/usr/local/bin/codex'), \
             patch('app.subprocess.run', return_value=completed) as run:
            app._codex_exec_text(request_id='web', prompt='Read a public page',
                                 model='gpt-6.1-sol', web_search=True)
        cmd = run.call_args.args[0]
        self.assertIn('web_search="live"', cmd)
        self.assertIn('features.shell_tool=false', cmd)
        self.assertEqual(cmd[cmd.index('--sandbox') + 1], 'read-only')

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
    def test_web_access_requires_an_explicit_boolean(self):
        self.assertFalse(app._optional_web_search({}))
        self.assertFalse(app._optional_web_search({'web_search': False}))
        self.assertTrue(app._optional_web_search({'web_search': True}))
        for value in ('true', 1, None, {}, []):
            with self.subTest(value=value), self.assertRaises(app.HTTPException):
                app._optional_web_search({'web_search': value})

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
            self.assertTrue(kwargs['web_search'])
            self.assertEqual(json.loads(pathlib.Path(kwargs['schema_path']).read_text()), schema)
            return app._CodexExecResult('ok', [])
        with patch('app._codex_exec_text', side_effect=reply), patch('app._claude_exec_text') as claude:
            app._cli_reply(request_id='schema', prompt='hello', model='gpt-6.1-sol',
                           provider='codex', reasoning_effort='medium', schema=schema, web_search=True)
        claude.assert_not_called()
        self.assertFalse(pathlib.Path(schema_paths[0]).exists())

    def test_claude_native_schema_effort_and_isolation(self):
        import json
        schema = {'type': 'object', 'properties': {'reply': {'type': 'string'}}}
        completed = subprocess.CompletedProcess(['claude'], 0,
            stdout=json.dumps({'is_error': False, 'structured_output': {'reply': 'ok'}}))
        self.tokens.start(); self.addCleanup(self.tokens.stop)
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

    def test_claude_web_access_exposes_and_approves_only_web_tools(self):
        import json
        completed = subprocess.CompletedProcess(['claude'], 0,
            stdout=json.dumps({'is_error': False, 'result': 'Read the roaster page.'}))
        self.tokens.start(); self.addCleanup(self.tokens.stop)
        with patch('app.shutil.which', return_value='/usr/local/bin/claude'), \
             patch('app.subprocess.run', return_value=completed) as run:
            app._cli_reply(request_id='web', prompt='Read a public page',
                model='claude-sonnet-5-5', provider='claude', reasoning_effort='medium',
                schema=None, web_search=True)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--tools') + 1], 'WebSearch,WebFetch')
        self.assertEqual(cmd[cmd.index('--allowedTools') + 1], 'WebSearch,WebFetch')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(json.loads(cmd[cmd.index('--mcp-config') + 1]), {'mcpServers': {}})

    def _claude_home(self, tokens):
        import os, tempfile
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.makedirs(os.path.join(home.name, 'oauth-tokens'))
        for account, token in tokens.items():
            with open(os.path.join(home.name, 'oauth-tokens', account), 'w') as handle:
                handle.write(token + '\n')
        return home.name

    def _claude_call(self, home, *outputs):
        """Run one Claude completion against canned CLI outputs; return the tokens the CLI saw."""
        import os
        seen = []
        def run(cmd, **kwargs):
            seen.append(kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'])
            return outputs[len(seen) - 1]
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': home}), \
             patch('app.shutil.which', return_value='/usr/local/bin/claude'), \
             patch('app.subprocess.run', side_effect=run):
            try:
                result = app._claude_exec_text(request_id='t', prompt='hi', model='claude-sonnet-5-5',
                                               reasoning_effort='medium')
            except app.HTTPException as error:
                return seen, error
        return seen, result

    @staticmethod
    def _claude_out(returncode=0, **fields):
        import json
        body = {'is_error': False, 'result': 'ok', **fields}
        return subprocess.CompletedProcess(['claude'], returncode, stdout=json.dumps(body))

    def setUp(self):
        app._CLAUDE_ACCOUNTS = app._ClaudeAccounts()
        self.tokens = patch('app._claude_tokens', return_value={'test': 'sk-ant-oat01-' + 't' * 40})

    def test_claude_long_lived_token_file_reaches_only_the_claude_child(self):
        token = 'sk-ant-oat01-' + 'a' * 40
        seen, result = self._claude_call(self._claude_home({'sofi': token}), self._claude_out())
        self.assertEqual(seen, [token])
        self.assertEqual(result.text, 'ok')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', app._codex_child_env())

    def test_claude_malformed_token_is_skipped_without_leaking_it(self):
        good = 'sk-ant-oat01-' + 'g' * 40
        home = self._claude_home({'bad': 'sk-ant-oat01-split\nacross-lines-secret', 'good': good})
        with self.assertLogs('codex-bridge', 'WARNING') as logs:
            seen, result = self._claude_call(home, self._claude_out())
        self.assertEqual(seen, [good])
        self.assertNotIn('secret', ''.join(logs.output))

    def test_claude_without_any_token_fails_before_running_the_cli(self):
        seen, error = self._claude_call(self._claude_home({}))
        self.assertEqual(seen, [])
        self.assertEqual(error.status_code, 500)

    def test_claude_accounts_rotate_least_recently_used(self):
        tokens = {name: 'sk-ant-oat01-' + name * 40 for name in 'abc'}
        home = self._claude_home(tokens)
        seen = []
        for _ in range(4):
            calls, _ = self._claude_call(home, self._claude_out())
            seen += calls
        self.assertEqual(seen, [tokens[name] for name in 'abca'])

    def test_claude_limited_account_is_skipped_and_cooled(self):
        tokens = {name: 'sk-ant-oat01-' + name * 40 for name in 'ab'}
        home = self._claude_home(tokens)
        limited = self._claude_out(1, is_error=True, api_error_status=429,
                                   result="You've hit your weekly limit · resets Nov 20, 5am (UTC)")
        with self.assertLogs('codex-bridge', 'WARNING') as logs:
            seen, result = self._claude_call(home, limited, self._claude_out())
        self.assertEqual(seen, [tokens['a'], tokens['b']])
        self.assertEqual(result.text, 'ok')
        self.assertIn('account=a status=429', ''.join(logs.output))
        self.assertIn('weekly limit', ''.join(logs.output))
        for token in tokens.values():
            self.assertNotIn(token, ''.join(logs.output))
        # The limited account stays out of rotation until its cooldown ends.
        seen, _ = self._claude_call(home, self._claude_out())
        self.assertEqual(seen, [tokens['b']])

    def test_claude_rejected_token_is_skipped_but_other_errors_are_not_retried(self):
        tokens = {name: 'sk-ant-oat01-' + name * 40 for name in 'ab'}
        home = self._claude_home(tokens)
        rejected = self._claude_out(1, is_error=True, api_error_status=401, result='OAuth access token is invalid.')
        seen, result = self._claude_call(home, rejected, self._claude_out())
        self.assertEqual(seen, [tokens['a'], tokens['b']])
        broken = self._claude_out(1, is_error=True, api_error_status=500, result='boom')
        seen, error = self._claude_call(self._claude_home(tokens), broken)
        self.assertEqual(len(seen), 1)
        self.assertEqual(error.status_code, 500)

    def test_claude_all_accounts_limited_raises_exhausted_without_generating(self):
        tokens = {name: 'sk-ant-oat01-' + name * 40 for name in 'ab'}
        limited = self._claude_out(1, is_error=True, api_error_status=429, result='limit')
        seen, error = self._claude_call(self._claude_home(tokens), limited, limited)
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(error, app._Exhausted)
        self.assertEqual(error.status_code, 503)

    def test_level_selects_models_and_effort_without_a_model(self):
        self.assertEqual(app._requested_routes({}),
                         [('claude-sonnet-5-5', 'claude', 'medium'), ('gpt-6.2-sol', 'codex', 'medium')])
        self.assertEqual(app._requested_routes({'model': 'auto', 'reasoning_effort': 'low'}),
                         [('claude-sonnet-5-5', 'claude', 'low'), ('gpt-6.2-sol', 'codex', 'low')])
        self.assertEqual(app._requested_routes({'reasoning_effort': 'high'}),
                         [('claude-opus-5-5', 'claude', 'high'), ('gpt-6.2-sol', 'codex', 'high')])
        self.assertEqual(app._requested_routes({'model': 'gpt-6.1-sol'}), [('gpt-6.1-sol', 'codex', 'medium')])

    def test_level_requests_reject_unlisted_levels_and_providers(self):
        for payload in ({'reasoning_effort': 'max'}, {'reasoning_effort': 'none'}, {'provider': 'claude'}):
            with self.subTest(payload=payload), self.assertRaises(app.HTTPException) as error:
                app._requested_routes(payload)
            self.assertEqual(error.exception.status_code, 400)
        for routes in app._LEVEL_ROUTES.values():
            for model, effort in routes:
                self.assertIn(effort, app._MODELS[model][1])

    def test_claude_error_is_not_a_success_or_secret_disclosure(self):
        completed = subprocess.CompletedProcess(['claude'], 1, stdout='secret', stderr='secret')
        self.tokens.start(); self.addCleanup(self.tokens.stop)
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
