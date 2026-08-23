# opencode-pool

Run many parallel [opencode](https://opencode.ai) generations through one
shared server instead of one ~500MB CLI process per call.

Each `opencode run` invocation boots a full node runtime. Twenty parallel
calls means ~10GB of RAM and hundreds of process boots per hour. One
`opencode serve` process multiplexes dozens of concurrent generations in a
flat ~600MB, because session state is cheap and the server locks per
session, not globally.

## Quickstart

```bash
./oc_pool.py up 1        # start the pool (N servers)
./oc_pool.py status
./oc_pool.py down
```

```python
from oc_pool import generate
text = generate("openai/gpt-5.6-sol-fast", "your prompt", variant="high")
# None on failure; the pool health-checks and respawns dead servers itself
```

Scaling rule: `N = ceil(peak concurrent calls / 16)`. One server is measured
comfortable to ~32 in flight (23x parallelism, flat latency); past that,
per-call latency inflates before throughput stops climbing.

## What this encodes (measured, opencode 1.18.21)

- **One shared working directory for all sessions.** The server keys state
  by directory and never evicts it: distinct directories leak ~52MB each,
  forever, and run ~5x slower. One directory stays flat across hundreds of
  sessions.
- **A permission ruleset at session-create is the server-side `--auto`.**
  The CLI flag has no server representation; it works by answering
  permission events client-side. Over raw HTTP, an unanswered permission ask
  hangs the blocking call forever. The pool sends allow-all plus the three
  denies the CLI itself always sends (`question`, `plan_enter`, `plan_exit`;
  rules are last-match-wins, so a bare allow-* would re-enable them).
- **`POST /session/{id}/message` blocks and returns the final message.** No
  polling or SSE needed. HTTP 200 can still carry `info.error`; check it.
- **Never send two prompts to one session.** The second silently joins the
  in-flight run and receives its result. One session per call; delete after.
- **Servers are cattle.** There are field reports of long-uptime wedges;
  the pool health-checks before use and respawns dead servers. Discard the
  state DB rather than maintaining it.

## If you are scripting the CLI instead

Two footguns cost us hours: always pass `--auto`, and always redirect stdin
(`< /dev/null`) — `opencode run` blocks forever on an inherited non-TTY
stdin when the prompt is long. `subprocess.run(capture_output=True)` is
immune because the child gets a pipe.

## Caveats

Pinned to the 1.18.x server API, which is not a stable contract. Auth is
HTTP Basic via `OPENCODE_SERVER_PASSWORD` (the pool generates one per `up`).
Built with Claude; the operating facts come from measured tests, not docs.
