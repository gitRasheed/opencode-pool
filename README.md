# opencode-pool

Run many parallel [opencode](https://opencode.ai) generations through one
shared server instead of one ~500MB CLI process per call.

Each `opencode run` invocation boots a full node runtime, so twenty parallel
calls cost about 10GB of RAM and hundreds of process boots per hour. One
`opencode serve` process multiplexes dozens of concurrent generations in a
flat ~600MB, because session state is cheap and the server locks per
session rather than globally.

## Quickstart

```bash
./oc_pool.py up 1        # start the pool (N servers)
./oc_pool.py status
./oc_pool.py down
```

```python
from oc_pool import generate
text = generate("openai/gpt-5.6-sol-fast", "your prompt", variant="high")
```

`generate` returns None on failure. The pool health-checks servers before
use and respawns dead ones.

Scaling rule: `N = ceil(peak concurrent calls / 16)`. One server measured
comfortable at ~32 in flight (23x parallelism, flat latency). Past that,
per-call latency inflates before throughput stops climbing.

## What this encodes (measured on opencode 1.18.21)

All sessions share one working directory. The server keys state by
directory and never evicts it: each distinct directory leaks ~52MB forever
and runs about 5x slower. A single shared directory stays flat across
hundreds of sessions.

A permission ruleset at session creation is the server-side equivalent of
`--auto`. The CLI flag has no server representation; it works by answering
permission events client-side. Over raw HTTP, an unanswered permission ask
hangs the blocking call forever. The pool sends allow-all plus the three
denies the CLI itself always sends (`question`, `plan_enter`,
`plan_exit`). Rules match last-to-first, so a bare allow-all would
re-enable them. One subtlety: when a subagent spawns, opencode keeps only
the parent rules named `external_directory` or carrying a deny (matched by
name, not wildcard), so the ruleset also carries an explicit
`external_directory` allow. The pool additionally sets
`OPENCODE_PERMISSION` on the server process as a backstop for ask paths
a session ruleset never reaches, such as `doom_loop`.

`POST /session/{id}/message` blocks and returns the final message, so no
polling or SSE is needed. HTTP 200 can still carry `info.error`; check it.

Never send two prompts to one session. The second silently joins the
in-flight run and receives its result. Use one session per call and delete
it afterward.

Servers are cattle. There are field reports of long-uptime wedges, so the
pool health-checks before use and respawns dead servers. Discard the state
DB rather than maintaining it.

## If you are scripting the CLI instead

Two footguns cost us hours:

```bash
opencode run --auto -m <model> "your prompt" < /dev/null
```

Always pass `--auto`, and always redirect stdin: whenever fd 0 is a
non-TTY that never reaches EOF (a socket, an open pipe), `opencode run`
reads stdin to EOF before doing anything, even when the prompt was passed
as an argument. Prompt length is irrelevant. From Python, pass
`stdin=subprocess.DEVNULL` explicitly; `capture_output=True` does not
cover stdin, and only looks safe when the parent's own stdin happens to
be /dev/null.

## Related

[opencode-runtime](https://github.com/ashish16052/opencode-runtime) also
wraps the opencode server for a different problem: one isolated server per
tenant. This pool shares servers to cut process and memory overhead. If
callers need isolation from each other, look there first.

## Caveats

Pinned to the 1.18.x server API, which is not a stable contract. Auth is
HTTP Basic via `OPENCODE_SERVER_PASSWORD` (the pool generates one per
`up`). The documented behavior comes from measured tests, not
the server docs.
