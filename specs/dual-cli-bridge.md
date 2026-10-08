# Dual CLI completion bridge

Objective: trusted applications choose a Codex or Claude model and effort through
the existing completion endpoint, including native structured output.

Constraints: preserve private authentication and existing Stocks callers; one CLI
per request with no provider fallback; no credentials in source or response;
Claude completions have no tools, hooks, MCP connections, or persisted sessions;
keep each CLI's login in its own private volume.

Owner: ezenciel-bridges owns model validation, CLI invocation and image versions.

Simplest path: reuse the HTTP request/response contract; full model IDs select
the CLI; expose the supported model/effort table via `/v1/models`; pass schemas
to the CLI's native structured-output option.

Proof: contract checks reject invalid model/effort/provider combinations and
verify both subprocess adapters. Build from the reviewed commit, then send one
authenticated VM request to GPT-6.1 Sol and one to Claude Sonnet 5.5 at medium.

Stop: Claude subscription authorization requires the owner's browser login.
