# Initial Codex task

Read `SPEC.md` and `AGENTS.md` completely before modifying code.

Your objective is to build TorrWatch 1.0 according to the specification, but do **not** attempt to implement the entire product in one pass.

First:

1. Inspect the current repository state.
2. Read the full specification and agent rules.
3. Create `docs/implementation-plan.md` that breaks Phase 0–10 into concrete milestones, dependencies, expected files/modules, tests and completion criteria.
4. Create the initial ADR only if a genuine specification-level ambiguity blocks a sound implementation.
5. Then implement **Phase 0 — Bootstrap** only.

Phase 0 must establish a clean, production-oriented foundation:

- Python project metadata and locked dependencies;
- FastAPI web entrypoint;
- independent worker entrypoint;
- SQLite/SQLAlchemy/Alembic foundation;
- initial admin-authentication skeleton;
- Dockerfile;
- Docker Compose with separate `web` and `worker` services using the same image;
- `.env.example`;
- Ruff/mypy/pytest configuration;
- GitHub Actions CI;
- documentation skeleton;
- basic health endpoints;
- tests for the Phase 0 behavior.

Constraints:

- clean-room implementation; do not copy TorrentMonitor source code;
- do not add torrent search, VPN, Redis, message brokers, Kubernetes, SPA frameworks or any out-of-scope feature;
- do not request real tracker credentials during Phase 0;
- do not disable security mechanisms to simplify development;
- do not perform destructive operations outside the repository/project-owned Docker resources;
- follow `AGENTS.md` for Git, tests, documentation and completion reporting.

Before declaring Phase 0 complete, run all applicable quality gates and Docker validation, fix failures, update documentation, and create logical commits.

At the end, provide a concise Phase 0 completion report and identify Phase 1 as the next task. Do not automatically start Phase 1 until Phase 0 is internally complete.
