# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

- Python 3.12+, stdlib + `pydantic` / `typer` / `python-frontmatter` only. **No new runtime dependency** without an explicit decision.
- No lint/typecheck toolchain is configured (`pyproject.toml` has only `pytest` as a dev dependency). **The verification gate is `python -m pytest tests/ -q`.**
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
3 (selected_projects) some test RED   ← live
4 (exclusions)        some test RED   ← live
```

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
    for banned in ('owner', 'privacy', 'allowed_projects', 'exclusions'):
        assert banned not in admission
```

Pin the **source text of the two functions**, not the module. A module-level grep would trip on unrelated code and get "fixed" by moving the call elsewhere.

**When to use**: the separation is a design invariant that is invisible to behaviour on current data (so no behavioural test can distinguish the correct from the incorrect wiring).

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
- [ ] Comment density matches the surrounding code; no comments explaining deleted code?
