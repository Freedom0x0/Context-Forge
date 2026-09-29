# Thinking Guides

> **Purpose**: Expand your thinking to catch things you might not have considered.

---

## Why Thinking Guides?

**Most bugs and tech debt come from "didn't think of that"**, not from lack of skill:

- Didn't think about what happens at layer boundaries → cross-layer bugs
- Didn't think about code patterns repeating → duplicated code everywhere
- Didn't think about edge cases → runtime errors
- Didn't think about future maintainers → unreadable code

These guides help you **ask the right questions before coding**.

---

## Available Guides

| Guide | Purpose | When to Use |
|-------|---------|-------------|
| [Code Reuse Thinking Guide](./code-reuse-thinking-guide.md) | Identify patterns and reduce duplication | When you notice repeated patterns |
| [Cross-Layer Thinking Guide](./cross-layer-thinking-guide.md) | Think through data flow across layers | Features spanning multiple layers |

---

## Quick Reference: Thinking Triggers

### When to Think About Cross-Layer Issues

- [ ] Feature touches 3+ layers (API, Service, Component, Database)
- [ ] Data format changes between layers
- [ ] Multiple consumers need the same data
- [ ] You're not sure where to put some logic
- [ ] You are adding an event kind, JSONL record, RPC payload, or config field
- [ ] UI / command code starts casting raw payload fields directly

→ Read [Cross-Layer Thinking Guide](./cross-layer-thinking-guide.md)

### When to Think About Code Reuse

- [ ] You're writing similar code to something that exists
- [ ] You see the same pattern repeated 3+ times
- [ ] You're adding a new field to multiple places
- [ ] **You're modifying any constant or config**
- [ ] **You're creating a new utility/helper function** ← Search first!
- [ ] Two files read the same untyped payload field with local casts
- [ ] Multiple branches update the same derived state from `kind` / `action`

→ Read [Code Reuse Thinking Guide](./code-reuse-thinking-guide.md)

### When Verifying AI Cross-Review Results

- [ ] Reviewer claims "user input can be malicious" → Check the actual data source (internal manifest? user config? external API?)
- [ ] Reviewer flags "missing validation" → Is the data from a trusted internal source?
- [ ] Reviewer says "behavior change" → Read the code comments — is it intentional design?
- [ ] Reviewer identifies a "bug" in test → Mentally delete the feature being tested — does the test still pass? If yes → tautological test

**Common AI reviewer false-positive patterns**:
1. **Trust boundary confusion**: Treating internal data (bundled JSON manifests) as untrusted external input
2. **Ignoring design comments**: Flagging intentional behavior documented in code comments as bugs
3. **Variable misreading**: Not tracing a variable to its actual definition (e.g., Map keyed by path vs name)
4. **Impact inflation**: Reporting a *latent* code defect as an *active* incident. A bug with 0 victims in current data is still worth fixing — but "the pipeline produces nothing usable" is a different claim from "this branch would misbehave if an empty value ever appeared". **Always re-verify the affected-record count against real data before repeating the narrative.**

**Verification rule**: Every CRITICAL/WARNING finding must be verified against the actual code before prioritizing. Budget ~35% false-positive rate for AI reviews.

---

### When Adding a Rollback Flag / Escape Hatch

- [ ] Does the docstring say **what it reverts** and **what it deliberately does not**?
- [ ] If a bug fix landed in the same area, is the fix inside or outside the escape hatch? Say which.
- [ ] Would an operator reading "restores old behavior" conclude the recent fix broke things?

→ Concretely: `legacy_filter=True` reverts sort + cap **only**. Visibility fixes stay on. See [Quality Guidelines](../backend/quality-guidelines.md) → "an escape-hatch flag must document what it does NOT revert".

### When Adding a Hard-Filter Predicate

- [ ] Delete each existing predicate in turn and run the suite — does anything go red?
- [ ] If a deletion is all-green, that branch is **entailed** by an earlier one: redesign it or pin it in source
- [ ] Would a future reader delete it as "cleanup" the moment it becomes load-bearing?

→ Technique and a shipped example in [Quality Guidelines](../backend/quality-guidelines.md) → "Every hard-filter predicate must survive a deletion mutation".

### When One Field Carries Two Concepts

- [ ] Does this field mean X *or* Y depending on context? (e.g. `paths` = applicability **and** evidence provenance)
- [ ] If you're writing a heuristic over it (e.g. "is this glob named in the prose?"), what class of record will it silently misclassify?
- [ ] Is the heuristic a **proxy** for a distinction the data model doesn't express?

→ A heuristic over a conflationated field always has a residual. If the residual matters, **split the field** — do not keep tuning the heuristic. Real case: `paths` conflating applicability with provenance caused 6 rules to be mis-tagged and still leaves 1 (a genuine `src/pages/**/*.tsx` applicability glob whose literal string never appears in the instruction text).

### When Declaring a Threshold, Gate, or Acceptance Metric

- [ ] What implementation change would make this **fail**? Name one.
- [ ] If you cannot name one, the metric is a **constant** — it carries no information
- [ ] Would a *worse* implementation score the same or better on it?
- [ ] Is the numerator or denominator fixed by the **fixture** rather than by the code?

→ A "hard gate" that no implementation can trip is worse than no gate: its pass reads as a validated property. Also: if you substitute a **proxy** for the metric the design specified, say so *in the threshold text itself, before implementing* — not only in the report afterwards. See [Quality Guidelines](../backend/quality-guidelines.md) → "A gate you cannot fail is not a gate".

**Verification discipline**: for each gate, build the mutation that should red it and record the result. "T1 ∧ T2 ∧ T3 passed" is not evidence until you've shown each can move.

---

## Pre-Modification Rule (CRITICAL)

> **Before changing ANY value, ALWAYS search first!**

```bash
# Search for the value you're about to change
grep -r "value_to_change" .
```

This single habit prevents most "forgot to update X" bugs.

---

## How to Use This Directory

1. **Before coding**: Skim the relevant thinking guide
2. **During coding**: If something feels repetitive or complex, check the guides
3. **After bugs**: Add new insights to the relevant guide (learn from mistakes)

---

## Contributing

Found a new "didn't think of that" moment? Add it to the relevant guide.

---

**Core Principle**: 30 minutes of thinking saves 3 hours of debugging.
