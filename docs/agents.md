# Agents

GreyQueue ships ten Claude Code subagents in `.claude/agents/`. Each owns one area and one doc, reviews changes in that area, and follows the same shared rules: read-only by default, never start or stop the database or containers, never overwrite `docs/results/`, never print a secret, and cite `path:line` for every claim.

| Agent | Purpose | Owns |
| --- | --- | --- |
| `prd` | Scope, user stories, and whether a claim or number may be published | `docs/prd.md`; claims in the README, `validation.md`, `benchmarks.md` and the portfolio page |
| `architecture` | Which module owns a change; engine invariants; ADRs | `architecture.md`, `design-decisions.md` and the engine design docs |
| `agents` | This roster, and guardrails for any model/LLM feature | `docs/agents.md`, `.claude/agents/` |
| `security` | Threats, controls and known gaps; security review of CI, Docker, Compose and `docker/` | `security.md` |
| `code-style` | How lines are written (Python under ruff, dashboard JS/CSS) | `docs/code-style.md`; `[tool.ruff]` |
| `database` | Schema, indexes, migrations, grants, query cost, restore consistency | `docs/database.md`, `persistence.md`, `transactions.md`; the grants in `docker/db-roles.sql` |
| `api` | HTTP contract, OpenAPI, worker wire protocol, metric names, every client | `docs/api.md`, `observability.md` |
| `design-system` | Dashboard tokens, patterns and how each state is shown | `docs/design-system.md` |
| `testing` | Running and reporting the suite, test design, flakes, evidence currency | `docs/testing.md`; pytest markers |
| `operations` | How the stack is built, started, pinned, observed and backed up | `operations.md`; Compose, Dockerfile, CI, `monitoring/`, `.env.example`, run instructions, dev scripts |

All ten have the tools Read, Grep, Glob, Bash, Write and Edit, because each writes its own doc; their prompts restrict them to that file. `security` always reviews alongside `api`, `design-system`, `operations`, `database` (for grants) and `architecture` (for tasks, executors and configuration), never instead of them. A doc marked in the table but missing is created by its owner on first use.

Agent files are loaded when a Claude Code session starts in this directory. Other projects of the same author define agents with the same names, so a session started elsewhere should run a GreyQueue role through a general-purpose agent that reads the role file first.

GreyQueue contains no model or LLM. Any proposal to add one goes to `agents` first: an LLM call is an external side effect under at-least-once delivery, must fit the task allowlist and executor contract, and must never hold the client or worker token.
