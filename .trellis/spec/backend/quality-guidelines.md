# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

- Python 3.12+. Runtime dependencies are exactly what `pyproject.toml` declares: `pydantic`, `typer`, `python-frontmatter`, `httpx` (remote provider), `tomli-w` (writing `config.toml`). `watchdog` is the `[watch]` extra, not a base dependency. **No new runtime dependency** without an explicit decision recorded here.
- No lint/typecheck toolchain is configured (`pyproject.toml` has only `pytest` as a dev dependency). **The verification gate is `python -m pytest tests/ -q`.**
- The verification environment must have every declared dependency installed: `httpx` and `tomli-w` were declared in 2026-10 but the machine they were verified on did not have them, and `forge init` failed with `ModuleNotFoundError: tomli_w`. A green suite is not proof that a declared dependency is present.
- Comment policy: **no explanatory comments for deleted code** and no decorative narration. A comment is justified only when it warns about a non-obvious trap that would otherwise be "fixed" back into a bug.

---

## Forbidden Patterns

### Don't: Trust raw frontmatter list values

**Problem**:
```python
patterns = metadata.get("paths", [])
if isinstance(patterns, str):
    patterns = [patterns]
if not patterns:          # [""] is truthy — falls through
    ...
if any(glob_match(path, p) for p in patterns):   # glob_match(x, "") is always False
```

An empty YAML list value (`paths: ` on its own line) parses to `""`, not to absence. The coercion above turns it into `[""]`, which is truthy, so the "no patterns" branch is skipped — and `glob_match(x, "")` compiles to `re.fullmatch("", x)`, always `False`. **Net effect: the record is never reachable under any query.**

**Why it's bad**: the failure is silent and total. There is no error, no log — the entry simply never matches, so it looks like "the feature produced nothing" rather than "the parser swallowed a field".

**Instead**:
```python
def _path_patterns(raw: object) -> tuple[str, ...]:
    """Normalize a frontmatter list value; empty entries are absence, not wildcards."""
    if raw is None:
        return ()
    values = raw if isinstance(raw, list) else [raw]
    return tuple(v for v in (str(x).strip() for x in values) if v)
```

Covered cases: `None` → `()`, `""` → `()`, `[""]` → `()`, `["a", ""]` → `("a",)`, missing key → `()`, `[1, "a"]` → `("1","a")`.

**Also split comma-joined values — and share ONE implementation.** Frontmatter lists are stored both as YAML lists and as comma-joined strings (`paths: a.tsx,b/**/*.tsx`). This repo briefly had **four** parsers where three split on `,` and one did not. The odd one compared a *glued* glob against prose and therefore never matched, silently mis-deciding 6 rules.

```python
# Wrong — three other call sites split on ',', this one does not
def _paths(raw): return tuple(raw.split()) if raw else ()      # "a,b" stays one token

# Correct — one normalization, every reader delegates to it
def list_values(raw: object) -> tuple[str, ...]:
    """Split comma-joined scalars, drop empties. The single reader for frontmatter lists."""
    if raw is None: return ()
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for v in values:
        out.extend(p for p in (x.strip() for x in str(v).split(',')) if p)
    return tuple(out)
```

**Rule**: grep for every reader of a frontmatter list key before adding a new one. They must all call `list_values` (or `_path_patterns`). A second implementation is a bug waiting to diverge.

**The same applies to "what counts as a member of the collection."** Eight vault walkers each decided for themselves:

```python
# four sites — only the generated nav file is excluded
for path in root.rglob("*.md"):
    if path.name == "index.md": continue
# three sites — a top-level allowlist
relative.parts[0] in {"knowledge", "rules", "reviews"}
```

Both look reasonable in isolation; together they meant `.retag-backup/` (migration backups, inside the vault root) was **indexed as content** — 480 documents for a 240-document vault, and `forge search` returned every hit twice.

```python
# index.py — the single definition
VAULT_TOP_LEVEL = frozenset({"knowledge", "rules", "reviews"})

def iter_vault_documents(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if not relative.parts or path.name == "index.md":
            continue
        if relative.parts[0] not in VAULT_TOP_LEVEL:
            continue
        yield path
```

**Rule**: any question of the form "how many / which documents exist?" goes through this one function. Tests must assert the *excluded* case, not just the included one — a fixture with only real documents passes under either convention.

---

### Don't: Serialize values without a safe scalar writer

**Problem**:
```python
lines.append(f"{key}: {value}")      # value is "**/*", or "a: b", or ""
```

**Why it's bad**: hand-rolled YAML emits are silently invalid, and the reader then throws — or worse, parses something else. Three cases that were actually shipped and only found later:

| Value | Written plain | Real YAML result |
|-------|---------------|------------------|
| `**/*` | `paths: **/*` | leading `*` parsed as an **alias** → parse error |
| `a quote: 不用…` | `sources: … user quote: 不用…` | `mapping values are not allowed here` |
| `\|pipe` / `>fold` | `x: \|pipe` | invalid **block scalar** |
| `ends with colon:` | `x: ends with colon:` | parse error |
| `""` | `x: ` | empty, then the *reader* turns it into `[""]` (see above) |

> **Warning (Python trap)**: `'' in '[{'` evaluates to `True`, so an empty-string guard written as `if value in '[{...}]'` silently falls into a flow-collection branch. Test the empty case explicitly.

**Instead**: one writer, `index.yaml_scalar()`, used by every emitter (`vault`, `vault_retag`). It quotes `*`, `&`, `!`, `#`, leading `:`/`-`/`?`/`,`, `": "`, `" #"`, `|`/`>` block-scalar leaders, trailing `:`, empty, and multi-line. Left plain on purpose: `true` / `42` (real YAML coerces types — matching the reader) and whitespace-only values.

**Idempotence**: the writer must only rewrite **unsafe unquoted** scalars. Rewriting already-quoted values churns every file on every run and destroys the "0 changes" idempotency signal that migration dry-runs depend on.

---

### Don't: Keep two definitions of "is this a credential"

**Problem**: `transcript._SECRET_PATTERNS` (the outbound sanitizer) and `vault_retag._SECRET`
(the vault privacy tag) both answered it, and they disagreed: the vault one knew the bare key
shapes — `sk-…`, `ghp_…`, `AKIA…` — that the outbound one did not. The weaker definition sat on
the path that sends text to a remote model.

**Instead**: `secrets._patterns(min_assignment_value)` is the only definition. `redact()` and
`contains_secret()` differ only in the assignment threshold — 1 character for redaction, 8 for a
privacy tag, because tagging a whole document private must not trip on Chinese prose after a colon.

---

### Don't: Let one caller silently swallow a new field

**Problem**:
```python
# write_rule_candidate writes it…
frontmatter["confidence"] = rule.confidence
# …but the second consumer drops it
MemoryDraft(body=..., role="claim")          # confidence lost
```

**Why it's bad**: a field added to a shared input object must be threaded through **every** consumer. Grep the field name across callers before calling the work done — a partial wiring is a data-fidelity bug, not a style issue.

**Instead**: wire it everywhere in the same change, and add an assertion in each consumer's test.

---

## Required Patterns

### Convention: `limit=0` means "no cap" — everywhere

**What**: across `RuleMatcher.match`, `DocumentIndex.search`, and `context_forge_memory_search`, `limit <= 0` disables truncation.

**Why**: three layers briefly disagreed — `MemoryStore` used `None`, the MCP tool used `0`, and `DocumentIndex.search` compiled `LIMIT 0`, which in SQL returns **zero rows**. A future `--limit 0` would have silently returned nothing.

**Contract**:

| Layer | Signature | "No cap" |
|-------|-----------|----------|
| `MemoryStore.search` | `limit: int \| None` | `None` (raises `ValueError` on `0`) |
| `RuleMatcher.match` | `limit: int = 8` | `limit <= 0` |
| `DocumentIndex.search` | `limit: int = 20` | `limit <= 0` |
| MCP `context_forge_memory_search` | `limit: int = 8` | `limit == 0` → `None` |

**Wrong vs Correct**:
```python
# Wrong — LIMIT 0 returns zero rows
sql += f" LIMIT {int(limit)}"

# Correct — only append a LIMIT clause when a cap is wanted
if limit > 0:
    sql += " LIMIT ?"
    params.append(limit)
```

---

### Convention: an escape-hatch flag must document what it does NOT revert

**What**: any `--legacy-*` / `*_filter` rollback flag states both its scope and its exclusions in the docstring and in the design contract.

**Why**: `legacy_filter=True` reverts **sort + cap only**. It does **not** revert the visibility fixes. A docstring saying "restores old behavior" invites an operator to conclude the fix broke things when rules stop disappearing — the opposite of the truth.

**Instead**:
```python
def match(self, ..., legacy_filter: bool = False):
    """legacy_filter=True reverts sort + cap only (the escape hatch).
    Visibility fixes are NOT reverted — reverting them would make
    project-level rules vanish again.
    """
```

---

### Gotcha: a long-running daemon keeps the code it started with

`forge watch-transcripts` is a resident process launched once by the Startup VBS. Python holds the imported modules in memory, and the VBS only relaunches the process when it **exits** — so a running daemon never picks up your edits.

Measured: the watcher had been up **21 hours** across a fix that changed YAML quoting in `vault.py`. Every document it deposited in that window carried the already-fixed defect, and nothing in the test suite could see it — tests import fresh.

**Consequence for verification**: "the fix is committed" and "the fix is running" are different claims. After changing anything under `context_forge/`:

```bash
taskkill /F /IM pythonw.exe          # VBS relaunches within 30s, with current code
forge doctor                         # confirm the new code is what's live
```

**When a daemon writes files, check the files it wrote AFTER your fix landed — not just the ones in your fixture.** A `git log` timestamp on the fix, compared against the process start time, is the cheapest signal.

## Testing Requirements

### Every non-trivial branch gets one minimal runnable check

No fixtures framework, no mocks, no parametrize suites. Small local helpers plus `assert`. A one-liner needs no test.

### Sort/rank guards must be proven by mutation, not by assertion shape

A test that asserts `result == [a, b, c]` proves nothing about *which* key ordered them — a pure-`mtime` implementation may pass by coincidence of the fixture's timestamps.

**Required technique**: build the fixture so the expected order is the **inverse** of `mtime`, then mutate the sort key and confirm the test goes red.

```
Mutation          Expected
────────────────  ────────────────────────────────
pure mtime        3 tests FAIL  ← proves ranking is real
confidence first  1 test FAIL   ← proves precedence
mtime before hit  1 test FAIL   ← proves hit leads
fallback → low    1 test FAIL   ← proves fallback
```

**Assertion points** (three distinct properties, one test each):
1. relevance outranks evidence strength (path hit + `low` beats project-level + `high`)
2. evidence strength layers within one relevance tier (`high` > unstated > `low`)
3. unstated value falls back to the domain default (`medium`), **never below** an explicit `low`

### Guard the upgrade branch, not just the happy path

Any "rebuild on shape mismatch" path needs a test that constructs the **legacy** shape. Without it the branch is decorative.

### Every hard-filter predicate must survive a **deletion** mutation

A filter with several `return False` branches looks like defense in depth. Some branches are logically **entailed** by an earlier one and are therefore dead code — deleting them leaves the whole suite green.

This actually shipped: `if space.owner != owner: return False` followed by `if privacy == 'private' and space.owner != owner: return False`. Given the first line, `space.owner == owner` holds, so the second predicate is **always** false. Deleting it changed nothing.

**Required technique**: for each predicate in a hard filter, delete it and run the suite.

```
Delete predicate N    Expected
────────────────────  ─────────────────────────────
1 (owner)             some test RED   ← live
2 (privacy)           all GREEN       ← DEAD, entailed by 1
3 (exclusions)        some test RED   ← live
```

(`selected_projects` was the third live predicate until v12 removed the project-level
access policy; the table above is the post-removal state.)

If a deletion leaves everything green you must do one of:
1. **Redesign** so the predicate has independent effect (e.g. make the earlier check a bank/namespace match instead of an owner match), or
2. **Pin it in source** — a guard test asserting the predicate exists, so removing it goes red. Record in a comment that it is currently entailed and when it becomes live.

Never leave an entailed predicate silently dead. A future reader will delete it as "cleanup" at exactly the moment it becomes load-bearing.

### A gate you cannot fail is not a gate

Before trusting any threshold-based check, ask: **what implementation change would make this fail?** If no realistic change exists, the gate is a constant and its "pass" carries no information.

This shipped: an evaluation gate `T3 = wrong_returned / labelled_returned <= 0.30` was declared a hard gate. But every group's labelled-item count (6) was below the recall `limit` (8) and the noise was never admitted, so **all labelled items were returned for any implementation** — numerator 4 and denominator 18 were both corpus constants. `T3 ≡ 0.2222` regardless of code. The companion gate T1 *did* discriminate (stripping all warning emission turned it red), which is what made the difference easy to miss.

**Required technique** — for each gate, build a mutation that should trip it:

```
Gate   Mutation that should RED it                Result
──────────────────────────────────────────────────────────
T1     strip all risk-warning emission             RED   ← real
T2     drop the per-`why_wrong` warning mapping    RED   ← real
T3     (none exists)                               GREEN ← constant, not a gate
```

If the "none exists" row is real, either rebuild the fixture so the gate can move (labels > `limit`, or items the system can actually suppress) or **demote it in the docs** — never leave it listed as a hard gate.

**Corollary — record gate power, not just gate results.** "T1 ∧ T2 ∧ T3 all passed" reads as three validated properties. Write down which ones can move.

### A gate must read the thing it is named for

`A gate you cannot fail is not a gate` covers gates that are *constants*. A gate can also move while measuring something else entirely — and that is harder to notice, because it goes red for the right reason on the wrong question.

This shipped: `doctor`'s `check_mcp()` was named for the host's MCP wiring, but its only test was `importlib.util.find_spec("mcp")` — whether the **Python package** imports. On the machine where it ran, the host had **zero** registration (the entry sat in a file the host does not read), so the whole feature was inert while `doctor` printed `[OK] mcp: mcp package available`. The check *could* fail — uninstall the package and it does — which is exactly why "is it a constant?" did not catch it.

**Technique** — for every check, state the property in the check's name, then ask whether a **failure of that property** produces the input the check rejects:

```
Check          Property named              Input it actually rejects     Verdict
──────────────────────────────────────────────────────────────────────────────────
check_mcp      host serves context_forge   `mcp` package not installed   ✗ wrong input
check_mcp'     host serves context_forge   entry absent from every file  ✓ the property
                                            the host reads
```

If the named property is not the one being rejected, either rename the check to what it measures or make it measure the property. Prefer the second: a diagnostic exists to fail when the system is broken, and a proxy that stays green through total failure is worse than no check, because it is read as evidence.

**Corollary — a proxy metric may be kept, but never as the verdict.** "The `mcp` package is importable" is real information; it belongs in the detail string next to the verdict, not as the verdict.

### Record the gate's *resolution*, not just that it moves

A gate can move yet still be mostly corpus-determined. After proving it *can* fail, check **how much of its range is actually implementation-sensitive**.

Shipped example: a rate gate `wrong / confirmed <= 0.20` over a fixed corpus was fixable at exactly 5 values — `{0.1538, 0.1923, 0.2308, 0.2692, 0.3077}` — because only 4 of the returned items were implementation-sensitive. The nominal "continuous rate" was really the boolean *"does ranking block ≥3 of the 4 wrong items from the top-K"*.

Passing a gate at 0.1538 is not evidence that the system holds a 15% misuse rate. Two numbers to record:

| Quantity | Meaning |
|----------|---------|
| **floor** | the value any correct implementation reaches (here 0.1538) |
| **headroom** | how far the implementation may drift before the gate trips (here 1 more wrong item) |

State them next to the threshold. If headroom is one item, say so — a green gate with one item of headroom is a much weaker claim than the number suggests.

### A column holding `DEFAULT + delta`: the reset value is `DEFAULT`, not `0`

**Problem**: a column is documented as "reset this to roll back", but the column stores `DEFAULT + signed_delta`.

```sql
-- schema
weight REAL NOT NULL DEFAULT 1.0            -- DEFAULT + signed delta; neutral = 1.0

-- documented rollback ("clear the weight column") written literally:
UPDATE memories SET weight = 0.0;           -- WRONG: scoring reads (weight - 1.0) => -1.0 penalty
UPDATE memories SET weight = 1.0;           -- RIGHT: delta 0 == neutral, matches pre-migration totals
```

**Why it matters**: `0.0` is not a neutral reset for a signed-delta column — it is a maximal negative adjustment. The existing totals silently shift for every record, which looks like a scoring bug rather than a botched rollback.

**Rule**: when a column's semantics are `DEFAULT + delta`, every doc that says "clear/reset/zero" must name `DEFAULT`. Grep for the word "清零" / "zero" / "clear" in rollback docs and check the column's neutral value. Prefer documenting the literal statement (`UPDATE memories SET weight = 1.0`) over an abstraction like "clear the column".


### Source pins for architectural separations

When two code paths must never call into each other (e.g. "the elimination filter must not know about the admission ladder, and vice versa"), behavioural tests alone don't hold the line — someone can later add a call that no existing test covers.

```python
import inspect

def test_elimination_and_admission_never_mix():
    elimination = inspect.getsource(boundary_visible)
    admission = inspect.getsource(admitted)
    for banned in ('derive_level', 'admitted', 'principle', 'pattern', 'instance'):
        assert banned not in elimination
    for banned in ('owner', 'privacy', 'visibility', 'exclusions'):
        assert banned not in admission
```

Pin the **source text of the two functions**, not the module. A module-level grep would trip on unrelated code and get "fixed" by moving the call elsewhere.

**When to use**: the separation is a design invariant that is invisible to behaviour on current data (so no behavioural test can distinguish the correct from the incorrect wiring).

---

## Resident-watcher contracts

These are the invariants of the one long-running process (`forge watch-transcripts`, also
launched by the Windows VBS autostart). They are easy to undo by "simplifying" one call site.

### A resident process writes its failures to a file, not only to stdout

`pythonw` from the Startup folder has no console: `print()` goes nowhere. A scan error could
repeat for weeks while `doctor` showed only the instantaneous state — there was literally
nothing to look at. Every message that matters in `watcher.py`, `worker.py` and
`scheduled.py` goes through `runtime_log.report()`, which prints **and** appends to
`runtime_dir()/runtime.log` (rotating once at 1 MB). `report` never raises: a diagnostic
must not be the reason a pass fails. `forge doctor` prints the log path in the watcher line.

### Liveness must come from the lock, not the heartbeat

`doctor.check_watcher` must read **who holds `runtime_dir()/watcher.lock`** (`watcher.lock_holder_pid()`),
never the heartbeat file. The heartbeat writes `{"pid","at"}` on every pass and the last writer wins —
with two processes running it reports whichever wrote most recently and calls the other one healthy.
That is exactly `A gate must read the thing it is named for`: "is the watcher running" is a question
about mutual exclusion, and only the lock answers it. A leftover lock file with **no holder** must read
as *not running* — never as alive.

### The instance lock is per-library, never global

`watcher.lock_path()` is `config.runtime_dir()/"watcher.lock"`. `runtime_dir()` honours
`CONTEXT_FORGE_HOME`, and that is what keeps the test suite, alternate profiles, and this repo's own
concurrency tests from deadlocking each other. A global path here is a self-inflicted outage, not a
stricter guard. The acquire must happen **before anything mutating** in `run()` (baseline writes,
`scan_once`, `scheduled.run_due`, `process_pending_finalizations`) — a losing instance must not write
anything at all. Known residual: `--all-history` calls `reset_baseline()` in `cli.py` before
`watch_run()`, so that flag can still scribble before losing the race.

### Windows byte-range locks deny *reads* of the locked byte

```python
# WRONG: the holder cannot read its own pid back from another handle
lock_byte_0(fd); write_pid_at_offset_0(fd)   # reads from a second handle raise PermissionError

# RIGHT: lock byte 0 for exclusion, keep the payload at offset 1
lock_byte_0(fd); write_pid_at_offset_1(fd)   # locked byte range is denied, the rest stays readable
```

`msvcrt.locking` denies reads of the byte range it covers, and the failure is a `PermissionError` that
reads-then-returns-None if swallowed — which degrades silently into "no holder". `fcntl.flock` on POSIX
has no such behaviour, so the offset-1 scheme must be used unconditionally or the two platforms diverge.

### A cumulative counter is a sticky gate — say so where the count lives

`scan_failures` accumulates for the life of the derived DB and never resets, so `count > 0` fails
`doctor.check_host_silence` **forever**. That is intended (the acceptance criteria say "跨轮累积" and
"fail when a count exists"), but a future reader will read a bare counter as a rolling window. State the
semantics on the accessor, and note that a *windowed* count needs per-event rows — not a reset knob.
Same family as the `DEFAULT + delta` rule above: the trap is in what the number means, not in the code.

### Liveness is a *suppressor*, never a precondition (inferred finalization)

`EpisodeCollector.advance_inferred` decides on **file evidence first** — 30/30-second silence, a
`new_activity` transcript, and the trailing-`tool_use` check. A host-reported liveness signal
(process confirmed alive, or a tool outstanding) only *suppresses* a close; it never licenses one.

The shipped bug was the inverse gate `if session_process is not False or session_tools is not False:
continue`. When the host registered nothing the probe returns `None`, `None is not False` is true,
and the file-evidence branch below is **structurally unreachable** — episodes stay `open` forever
(the bug 8c exists to fix) and finalization is silently pinned on host-hook cooperation, which is
the probabilistic trigger the red line forbids. The fix is `if session_process is True or
session_tools is True: continue`.

Rule: an *unknown* fact is not a *negative* fact. If a gate consumes an optional signal that is
`None` when absent, ask whether absence **blocks** the gate or **falls through** it — and make the
test pin the absent case, not just the present one.

---

## Code Review Checklist

- [ ] New field on a shared input object: wired through **all** consumers (grep the name)?
- [ ] Any `snippet`/`highlight`/`offsets` call: numeric column index, verified against live `PRAGMA table_info` (including `UNINDEXED` columns)?
- [ ] Any `LIMIT`: `limit <= 0` semantics consistent with the layer table above?
- [ ] Any derived-index path: delegated to `Vault._index_path`, not re-derived?
- [ ] Any frontmatter list value: normalized (empties dropped) before use as patterns?
- [ ] Any escape-hatch flag: docstring says what it reverts **and what it does not**?
- [ ] Any sort/rank logic: fixture order is inverse to `mtime`, and a pure-`mtime` mutation would fail?
- [ ] Any threshold/gate/acceptance metric: can you name an implementation change that fails it? (If not, it's a constant — demote it in the docs)
- [ ] Any gate that *does* move: is its **floor** and **headroom** recorded next to the threshold?
- [ ] Any column documented as "reset/clear/zero" in a rollback path: is its **neutral** value `DEFAULT` rather than `0`?
- [ ] Any substitution of a proxy for the design's stated metric: is it recorded **in the threshold text, before** the implementation, not only in the report?
- [ ] Any architectural separation that behaviour can't distinguish on current data: pinned via `inspect.getsource` on the two functions?
- [ ] Any "rebuild on mismatch" branch: has a legacy-shape test?
- [ ] Any liveness/watcher check: does it read the **lock holder**, not the last-written heartbeat?
- [ ] Any new lock/mutex path: scoped under `runtime_dir()` (per-library), acquired before any write?
- [ ] Any cumulative counter that feeds a gate: are its never-reset semantics stated on the accessor?
- [ ] Any gate consuming an optional signal: does **absence** fall through (not block), and is the absent case pinned by a test?
- [ ] Comment density matches the surrounding code; no comments explaining deleted code?
