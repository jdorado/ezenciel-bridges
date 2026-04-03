# Bridge Stdin Prompt Fix

## LLM coding instructions
- Problem: `ezenciel-bridges/app.py` appended the fully rendered prompt as the final `codex exec` CLI argument. Large prompt payloads from `messages` can exceed the OS argv limit and fail before Codex starts with `OSError: [Errno 7] Argument list too long`.
- Fix path: keep the bridge thin and preserve the same OpenAI-compatible HTTP contract, but change `_codex_exec_text` in `ezenciel-bridges/app.py` to pass `-` as the prompt argument and send the real prompt through `subprocess.run(..., input=prompt, text=True)`.
- Invariants:
  - one Codex subprocess per request
  - no prompt content in argv
  - same timeout / logging / error envelope behavior
  - same event parsing and response formatting
- Add a minimal regression test at `ezenciel-bridges/tests/test_app.py` that patches `shutil.which` and `subprocess.run`, calls `_codex_exec_text`, and asserts:
  - the command uses `-` instead of the full prompt
  - `input` equals the prompt
  - the returned completion is still parsed from JSONL output
- Keep dependencies minimal; use `unittest` from stdlib rather than adding new tooling.

## Reviewer notes
- This is a transport bug, not an auth bug. `BRIDGE_API_KEY` can be valid while the request still fails before execution.
- Main approval point: stdin support for `codex exec` is confirmed by local CLI help, so this is a supported interface rather than a workaround.
- Residual risk is low and limited to bridge subprocess invocation behavior.
