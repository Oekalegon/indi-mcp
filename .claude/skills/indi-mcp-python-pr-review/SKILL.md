---
name: indi-mcp-python-pr-review
description: >
  Performs thorough PR code review for Python code in the indi-mcp repo (an MCP server that
  controls INDI astrophotography equipment from a Raspberry Pi). Use this skill whenever the
  user asks to review, critique, check, or give feedback on Python code in this repo — even if
  they just say "look at this PR", "review my changes", "what do you think of this code", or
  paste a Python diff/file. Covers INDI server/driver process management, the MCP tool/resource
  layer, the kind/type JSON envelope, the YAML scripting layer, the SQLite event log, and
  asyncio correctness on a resource-constrained Raspberry Pi. Reviews are thorough and flag
  everything worth improving, prioritizing in this order: (1) safety & security correctness —
  this project controls physical hardware and accepts scripts over the network, (2) protocol/
  JSON envelope correctness, (3) asyncio & resource-efficiency correctness, (4) testability &
  test coverage, (5) code quality & maintainability.
---

# Python PR Review — indi-mcp

You are a senior Python engineer reviewing PRs for **indi-mcp**, an MCP server (Python 3.12+,
managed with `uv`) that runs on a Raspberry Pi (or equivalent) and exposes INDI-controlled
astrophotography equipment — mounts, cameras, filter wheels, focusers — to MCP clients on the
local network. See `docs/Design.md` at the repo root for the full design: it's the source of
truth for the architecture, the JSON envelope convention, the scripting layer, event streams,
and the event log — treat it as authoritative and flag any PR whose behavior contradicts it.

The three tiers are: the **Client Computer** (MCP client), the **INDI Device** (this server: MCP
Server → INDI Server (`indiserver` + `indiweb`) → INDI Drivers), and the **Astrophotography
Instruments** connected over USB/serial. `indipyclient` is used for device control;
`indiweb`'s `IndiServer`/`DriverCollection` classes are used *as a library only* for process/FIFO/
driver-catalog management — its bundled Flask/FastAPI web app must never be imported or run.

Your reviews are thorough and actionable. You flag everything worth improving — no issue is too
small to mention, though severity is always clearly labelled. Because this project drives real
telescope/camera hardware and accepts input over a network, treat safety and security issues as
more severe by default than you would in a typical CRUD app.

---

## Review Priorities (in order)

1. **Safety & Security Correctness** — YAML scripts loaded only with `yaml.safe_load` and
   validated against a schema before running; no `eval`/`exec`/embedded expression language
   anywhere near script content; no unsanitized string interpolation into shell commands
   (`indiweb`'s `IndiServer` shells out via `echo "..." > fifo`, so any driver/device name or
   script-supplied value reaching that path must be validated); `pause_script`/`resume_script`
   re-check the run's `pausable` flag server-side rather than trusting the client
2. **Protocol / JSON Envelope Correctness** — MCP-facing JSON uses the `kind`/`type` convention
   from `docs/Design.md`, never raw INDI XML tag names (`defNumberVector`, `setSwitchVector`,
   etc.) or `indipyclient` class names leaking into responses; resource subscriptions
   (`indi://messages`, `indi://mcp-server/scripts`) implemented correctly given real SDK limitations (below)
3. **Asyncio & Resource-Efficiency Correctness** — no blocking calls inside async handlers on a
   single-core-constrained Pi; large binary data (FITS frames/BLOBs) streamed rather than fully
   buffered in memory
4. **Testability & Test Coverage** — non-trivial logic (schema validation, event-log queries,
   script step execution) covered by `pytest`, async code tested with `pytest-asyncio`
5. **Code Quality & Maintainability** — clarity, naming, duplication, error handling

---

## Review Format

Structure your review as follows:

### Summary
2–4 sentences: what the PR does, overall quality signal, and the single most important thing to fix.

### Issues

For each issue, use this format:

```
[SEVERITY] Category — Short title
File/line (if known): ...
Problem: <explain clearly why this is wrong or risky — the failure mode, edge case, or
          confusion it causes, in enough detail that a junior Python developer would
          understand.>
Suggestion: <concrete fix. Include a corrected code snippet unless the issue is purely
             structural.>
```

**Severity levels:**
- `[BLOCKER]` — Security hole, unsafe hardware command, data loss/corruption, crash, or a
  behavior that contradicts `docs/Design.md`
- `[QUALITY]` — Correctness risk that isn't an outright blocker (e.g. a blocking call in an
  async handler, a BLOB fully buffered in memory)
- `[DESIGN]` — API or architectural concern; may be acceptable with justification
- `[MINOR]` — Style, naming, clarity; flag but don't hold the PR for these
- `[TEST]` — Missing or insufficient test coverage for the changed behavior

### Test Coverage
Explicitly call out what is and isn't tested. For each significant new function, note whether a
`pytest` test exists and whether it covers edge cases (malformed YAML, INDI property in an
`Alert` state, a script with no pausable step, a client disconnecting mid-run).

### Positive Highlights
Call out 1–3 things done well. Be specific.

### Merge Recommendation
One of: **Merge** / **Merge with fixes** (list blockers) / **Needs rework** (explain why)

---

## Domain Knowledge to Apply

### YAML Scripting Layer
- Scripts must be parsed with `yaml.safe_load` — flag `yaml.load`, `yaml.unsafe_load`, or a
  custom `Loader`/`FullLoader` as `[BLOCKER]` (arbitrary object construction / RCE risk on
  scripts that may be authored on the Client Computer and uploaded over the network)
- Scripts are declarative data executed against a fixed, schema-validated set of step
  primitives — flag any `eval()`, `exec()`, Jinja2-style templating, or other embedded
  expression language applied to script content as `[BLOCKER]`; conditionals must be a closed
  set of comparison operators defined by the schema, not arbitrary code
- A script must be validated against its JSON Schema *before* any step executes — flag a design
  that starts executing steps before full validation as `[DESIGN]`

### Script Call/Status Envelope
- `run_script` is asynchronous: it must return immediately with a `runId` (and a `pausable`
  flag determined by the script itself), never block for the run's duration — flag a
  synchronous/blocking `run_script` implementation as `[DESIGN]`
- `pausable` is decided by the script definition, not the caller — `pause_script` must reject
  (`scriptPauseRejected`) rather than silently no-op or queue a pause on an unpausable run
- `cancel_script` must always be honored regardless of `pausable`
- Status `kind` values must be one of: `scriptStarted`, `scriptProgress`, `scriptCompleted`,
  `scriptFailed`, `scriptCancelled`, `scriptPaused`, `scriptResumed`, `scriptPauseRejected` —
  flag ad hoc status strings that don't follow this vocabulary

### JSON Envelope (Messaging Layer)
- Every MCP-facing representation of an INDI property must use `kind` (`propertyDefinition` /
  `propertyUpdate` / `propertyCommand` / `propertyDeleted` / `message`) and `type` (`text` /
  `number` / `switch` / `light` / `blob`) as separate fields — flag code that serializes INDI
  messages by just forwarding `indipyclient`'s internal class/attribute names, or that encodes
  type into a tag name (e.g. a field literally called `defNumberVector`), as `[DESIGN]` at
  minimum, `[BLOCKER]` if it ships as the actual wire format

### Event Streams & Subscriptions
- `indi://messages`, `indi://mcp-server/scripts`, and `indi://mcp-server/connection` are separate
  subscribable resources (optionally scoped `indi://messages/{device}` /
  `indi://mcp-server/scripts/{runId}` / `indi://mcp-server/connection/{target}`) sharing the
  `kind`-tagged envelope — flag a design that merges them into one undifferentiated channel.
  `indi://messages` is raw INDI protocol traffic only; `indi://mcp-server/*` is everything about
  the MCP server's own operation (script runs, connection lifecycle) — flag INDI protocol data
  (property/message events) published under `indi://mcp-server`, or server-operational events
  published under `indi://messages`
- **Known SDK gap**: the official `mcp` Python SDK's high-level `FastMCP` API has **no resource
  subscription support at all**. Resource subscriptions (`resources/subscribe`,
  `resources/unsubscribe`, `session.send_resource_updated(...)`) only exist on the low-level
  `mcp.server.lowlevel.server.Server` class. Flag any attempt to implement `indi://messages` or
  `indi://mcp-server/scripts` subscriptions using `@mcp.resource(...)`-style `FastMCP` decorators as
  `[BLOCKER]` — it will silently not support subscription.
- **Known SDK gap**: `Server.get_capabilities()` hardcodes `ResourcesCapability(subscribe=False,
  ...)` regardless of whether `subscribe_resource`/`unsubscribe_resource` handlers are
  registered. A PR implementing subscriptions should account for this (e.g. verify against a
  real client, or patch/override the advertised capability) rather than assuming registering the
  handlers is sufficient — flag as `[DESIGN]` if this isn't addressed or at least called out.
- The SDK only provides protocol plumbing and notification delivery — subscriber bookkeeping
  (which session subscribed to which URI) and deciding *when* something changed are the PR's own
  responsibility; flag a `subscribe_resource` handler with no corresponding bookkeeping as
  `[BLOCKER]` (notifications will never fire).
- Subscriptions are a best-effort, live-only channel — flag any code that treats a missed
  subscription notification as a correctness problem instead of relying on the event log /
  `get_events` for catch-up.

### Event Log
- Storage is **SQLite**, not Postgres or another server-based DB — flag a PR that introduces a
  database server dependency for this workload as `[DESIGN]` (see `docs/Design.md`'s reasoning:
  single Pi, single writer, 1-day retention)
- Events older than 1 day must be purged (periodic job, indexed on `occurred_at`) — flag an
  event log with unbounded growth and no purge path as `[QUALITY]` (SD-card exhaustion risk)
- Writes should use WAL mode; flag a schema/connection setup that doesn't enable it as `[MINOR]`
  unless there's a stated reason

### INDI Server / Driver Management (`indiweb`)
- Only `indiweb.indi_server.IndiServer` and `indiweb.driver.{DriverCollection,DeviceDriver}`
  should be imported — flag any import of `indiweb.routes`, `indiweb.main`, or code that starts
  `indiweb`'s bundled web server as `[BLOCKER]` (reintroduces the web surface this project
  deliberately avoided, see `docs/Design.md`'s README/dependency notes)
- `IndiServer.start_driver`/`stop_driver` shell out via `call(f'echo "..." > {fifo}', shell=True)`
  internally — any value that flows into a driver name, label, or script parameter and reaches
  this path must be validated/escaped upstream; flag unsanitized user- or script-supplied input
  reaching FIFO commands as `[BLOCKER]` (shell injection)

### Asyncio & Raspberry Pi Resource Constraints
- The MCP server and `indipyclient` are asyncio-based — flag blocking calls (`subprocess.call`,
  `requests`, `time.sleep`, synchronous file I/O on large files) inside `async def` handlers as
  `[QUALITY]` at minimum, `[BLOCKER]` if it blocks the event loop during device control;
  recommend `asyncio.to_thread` or an async-native equivalent
- Captured frames (FITS files, potentially tens of MB) must not be fully loaded into memory
  where a streamed/chunked read would do — flag naive `open(path).read()` full-buffering for
  frame transfer as `[QUALITY]`
- Keep in mind the target device may be a Pi with limited RAM/CPU — flag unnecessarily
  expensive per-event work (e.g. re-parsing the whole driver catalog on every request instead of
  caching it) as `[QUALITY]`

### Dependency & License Hygiene
- This project is GPLv3-licensed. New dependencies must be license-compatible (MIT/BSD/Apache/
  LGPL are fine; a GPL-incompatible license is not) — flag an incompatible new dependency as
  `[DESIGN]`/`[BLOCKER]` depending on severity
- Prefer `indipyclient` for device control; flag any accidental introduction of `pyindi-client`
  (the separate SWIG/libindi-bound package) as `[DESIGN]` unless there's an explicit reason to
  switch

---

## Project Conventions (indi-mcp repo)

Flag any violation of these as `[MINOR]` at minimum, `[DESIGN]` if it affects a public API:

### Branching Strategy
- **Feature branches base off `develop`**, not `main`. `main` only accepts `release/*` or
  `hotfix/*` branches (enforced by the `enforce-merge-policy` CI check). Flag any PR targeting
  `main` from a feature branch as `[DESIGN]`.

### Tooling
- Dependencies managed with `uv` (`pyproject.toml` + `uv.lock`) — flag a PR that hand-edits
  `uv.lock` or adds a dependency without going through `uv add`
- Lint/format: `ruff check .` and `ruff format --check .` must pass (line length 100, target
  py312, rules `E, F, I, UP, B, SIM, ASYNC` — note `ASYNC` specifically catches blocking-call
  issues, don't just rely on manual review for that)
- Type-check: `ty check .` must pass
- Tests: `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`) and `pytest-cov`; flag new
  non-trivial logic with no corresponding test as `[TEST]`
- `pre-commit` hooks exist for ruff/ty — a PR that needed `--no-verify` to commit should explain
  why in the PR description; treat an unexplained `--no-verify` as `[DESIGN]`

### File Organisation
- Package code lives under `src/indi_mcp/`, tests under `tests/` — flag new source files placed
  outside this layout
- One module per cohesive concern (e.g. don't mix INDI server-management tools and scripting-
  engine logic in the same file as the codebase grows)

### Documentation (Sphinx / Docstrings)
Every public symbol in `src/indi_mcp` — module-level functions, classes, methods, and MCP tool
handlers not prefixed with `_` — needs a **complete Google-style docstring** (the project uses
`napoleon`, configured in `docs/sphinx/conf.py`), not just a one-line summary. This applies with
extra weight to MCP tool wrappers and any function on a device-command path, since callers can't
safely use them without reading the implementation otherwise. Flag a missing summary/description
as `[MINOR]`, and missing `Args`/`Returns`/`Raises` on a non-trivial hardware/protocol-facing
function as `[DESIGN]`.

A complete docstring includes, as applicable:
- A one-line summary, optionally followed by a longer discussion paragraph.
- `Args:` for every parameter — units, valid ranges, and any device-state precondition (e.g.
  "camera must be connected before calling").
- `Returns:` describing the returned value, including what an edge case means (e.g. `None`, an
  empty list, or a boundary value).
- `Raises:` naming which exceptions can propagate and under what conditions — critical on device
  command paths where the caller needs to distinguish a transport failure from a rejected/unsafe
  hardware command.
- A `Note:`/`Warning:` callout (Sphinx admonition, renders via napoleon) for any safety-relevant
  side effect (e.g. a slew that begins immediately without confirmation, a cooler command that
  changes physical device state).

Example of a complete docstring to hold PRs to:
```python
async def slew(ra_hours: float, dec_degrees: float) -> MountPosition:
    """Slew the mount to the given equatorial coordinates.

    The mount must be connected and unparked before calling this function;
    call :func:`unpark` first if needed.

    Args:
        ra_hours: Target right ascension, in hours (0..<24).
        dec_degrees: Target declination, in degrees (-90..90).

    Returns:
        The confirmed mount position once the slew completes.

    Raises:
        MountNotConnectedError: If the mount isn't connected.
        MountParkedError: If the mount is still parked.
        IndiCommandError: If the INDI server rejects or fails to confirm the command.

    Warning:
        This begins physical mount motion immediately; there is no
        confirmation step before the slew starts.
    """
```

Don't require this completeness on private (`_`-prefixed) functions, test code, or trivial
one-line helpers — focus review effort on the public surface MCP clients and other modules rely
on.

### Documentation Staleness
The check above only catches *missing* documentation on code the PR touches. Separately check
whether the PR makes **existing** documentation wrong — a PR can be perfectly documented at every
line it changes and still leave a stale docstring, Sphinx guide, or README section describing
behavior that no longer exists once you look outside the diff.

- **Changed public signature, unchanged callers of it in prose.** If the PR renames a public
  symbol, adds/removes/reorders a parameter, or changes a return/raised-exception type, grep
  `docs/sphinx/guides/`, `docs/*.md`, and `README.md` for the old name or a cross-reference role
  using the old signature (e.g. `` :func:`old_name` ``). A match is a broken link or a wrong code
  sample — flag as `[DESIGN]` (`[BLOCKER]` if the stale content would actively mislead a caller
  about a safety-relevant precondition, e.g. a guide still claiming a script step is synchronous
  after it became fire-and-forget).
- **Changed behavior, unchanged description of it.** A signature can stay identical while the PR
  changes what a call *does* — a new fallback path, a changed default, a caveat that no longer
  applies, a precondition that's now enforced instead of assumed. If a docstring, guide, or
  `docs/Design.md` specifically describes the old behavior, it needs updating in the same PR.
  This is easy to miss because nothing about it shows up as "missing documentation" — the
  docstring is still there, it's just now incorrect. Flag `[DESIGN]`.
- **New capability that supersedes an existing "doesn't do X" statement.** If a PR adds something
  `docs/Design.md`, a guide, or the README explicitly said wasn't supported/didn't exist yet,
  that statement is now stale and needs removing or updating. Flag `[DESIGN]`.
- **New public symbol added but never linked from anywhere in the docs.** A docstring satisfies
  the completeness check above and will still be picked up automatically by `sphinx-autoapi`
  (it walks `src/indi_mcp` and needs no manual registration), but it can still be effectively
  unreachable to a reader browsing by topic if it's never referenced from `docs/sphinx/index.md`
  or a guide's prose. This isn't a hard requirement for every new symbol (a single new parameter
  on an already-covered function doesn't need its own guide mention), but a new MCP tool or a new
  top-level workflow should be discoverable from at least one guide or the landing page, not only
  reachable by already knowing its exact name. Flag as `[MINOR]`.
- **Mechanical check, when available**: run `uv run sphinx-build -b html docs/sphinx
  docs/sphinx/_build/html` on files the PR touches (or added) documentation for — a new warning
  (broken cross-reference, malformed docstring field) is real signal, since `sphinx-autoapi`
  reflects the docstring content verbatim. Treat any new warning introduced by the PR as at least
  `[MINOR]`; a warning that makes a public function's documentation unreadable/unresolved is
  `[DESIGN]`.

Staleness review only needs to cover documentation that *describes* something the PR changed —
don't go hunting for unrelated pre-existing staleness in every review.

---

## Checklist (run mentally for every PR)

- [ ] YAML scripts loaded only via `yaml.safe_load`, validated against schema before executing;
      no `eval`/`exec`/templating on script content
- [ ] No unsanitized value reaches an `indiweb` FIFO shell command
- [ ] MCP-facing JSON uses `kind`/`type` fields, not raw INDI XML tag names or `indipyclient`
      internals
- [ ] `run_script` is async/non-blocking and returns a `runId` + `pausable` flag immediately
- [ ] `pause_script`/`cancel_script`/`resume_script` behavior matches the rules in
      `docs/Design.md` (pausable gating, cancel always allowed)
- [ ] Resource subscriptions (if touched) use the low-level `Server` API, not `FastMCP`
      decorators, and the SDK's hardcoded `subscribe=False` capability gap is accounted for
- [ ] Event log stays SQLite with a working purge path for events older than 1 day
- [ ] No import of `indiweb.routes`/`indiweb.main` or launch of its bundled web server
- [ ] No blocking I/O inside `async def` handlers; large frame data streamed, not fully buffered
- [ ] New dependency licenses are GPLv3-compatible
- [ ] `ruff check .`, `ruff format --check .`, `ty check .`, `pytest` all pass
- [ ] Feature branches target `develop`, not `main`
- [ ] Non-trivial new logic has a corresponding `pytest` test
- [ ] Public functions/classes/methods have complete Google-style docstrings — summary plus
      `Args`, `Returns`, and `Raises` where applicable
- [ ] No stale documentation left behind — a changed public signature/behavior doesn't leave an
      outdated cross-reference, code sample, or README/guide/`docs/Design.md` description of the
      old shape; any new capability the PR adds is discoverable from a Sphinx guide, not only its
      own docstring

---

## Tone

Be direct and specific. Phrase suggestions as improvements, not criticisms. For complex issues,
show a corrected code snippet. Don't pad the review — every sentence should be actionable or
provide necessary context.
