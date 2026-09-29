# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | SQLite contracts: FTS5 column indices, derived-index shape upgrades, migration rules | Implemented contracts; verify tests before changes |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Frontmatter list/scalar contracts, `limit=0` semantics, escape-hatch docs, mutation-proven guards (sort keys **and** hard-filter predicates) | Implemented contracts; verify tests before changes |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |
| [Memory Store](./memory-store.md) | SQLite memory versions, evidence, migration and recovery | Implemented contracts; verify tests before changes |
| [Memory Lifecycle](./memory-lifecycle.md) | Candidate formation, publication and human correction | Implemented contracts; verify tests before changes |
| [Memory Recall](./memory-recall.md) | Scoped retrieval units and stage-separated exposure tracking | Implemented contracts; verify tests before changes |
| [Memory Merge](./memory-merge.md) | Conservative duplicate governance and deterministic quality reports | Implemented contracts; verify tests before changes |
| [Host Integration](./host-integration.md) | Claude Code MCP registration: real config locations and precedence, delegate the write, never parse host output | Implemented contracts; verify tests before changes |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
