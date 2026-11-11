## Memory bank (Hindsight-Crew, bank `__BANK__`)

The `__MCP_NAME__` MCP server is this machine's long-term memory (bank `__BANK__`, owner `__OWNER__`).
Use it without being asked.

- **recall first**: before work that touches a named project or system, or when the user refers to earlier
  work ("last time", "continue"), `recall` what is already known. Skip it for pure one-off questions.
- **retain** a finished unit of work, a reasoned decision, a costly-to-rediscover finding, or a new rule or
  preference of the user. Skip intermediate steps and anything cheap to re-derive from the repo or git.
- **retain format** (the gateway rejects anything else):
  - `content` is one line starting with a short bracketed label: `[task kind] what - result`.
    Example: `[fix] gateway tag normalizer - first attempt passed`.
  - `tags` are `prefix:slug` with a lowercase `[a-z0-9-]` slug. Prefixes: `project`, `kind`, `topic`, `user`.
    Send at least one `kind:` tag; `kind` is a closed list: __KIND_VALUES__. `user` accepts only `__OWNER__`.
  - Do not send `strategy`.
- **recall**: leave `max_tokens` unset, or send 4096 or more.
- A rejection names the broken rule; fix the call and retry once. Never retain secrets or tokens.
