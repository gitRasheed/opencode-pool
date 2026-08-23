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
# returns None on failure; the pool health-checks and respawns dead servers
```

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
re-enable them.

`POST /session/{id}/message` blocks and returns the final message, so no
polling or SSE is needed. HTTP 200 can still carry `info.error`; check it.

Never send two prompts to one session. The second silently joins the
in-flight run and receives its result. Use one session per call and delete
it afterward.

Servers are cattle. There are field reports of long-uptime wedges, so the
pool health-checks before use and respawns dead servers. Discard the state
DB rather than maintaining it.

## If you are scripting the CLI instead

Two footguns cost us hours. Always pass `--auto`, and always redirect
stdin (`< /dev/null`): `opencode run` blocks forever on an inherited
non-TTY stdin when the prompt is long. Python's
`subprocess.run(capture_output=True)` is immune because the child gets a
pipe.

## Related

[opencode-runtime](https://github.com/ashish16052/opencode-runtime) also
wraps the opencode server, but for a different problem: it isolates one
server per tenant. This pool goes the other way and pushes maximum
throughput through one shared server. If you need isolation between
callers, look there first.

## Caveats

Pinned to the 1.18.x server API, which is not a stable contract. Auth is
HTTP Basic via `OPENCODE_SERVER_PASSWORD` (the pool generates one per
`up`). Built with Claude; the operating facts come from measured tests,
not docs.
