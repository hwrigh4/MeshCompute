# MeshCompute Codex Instructions

MeshCompute's product and architecture source of truth is `PROJECT.md`.

Use `PROJECT.md` when a task involves:
- architecture or service boundaries
- scheduler behavior
- worker behavior
- job/attempt states
- resource accounting
- provider preemption
- security assumptions
- executor design
- MVP scope or development phases

Do not reread `PROJECT.md` for trivial edits that do not depend on product or architectural context.

## Current development philosophy

- Optimize for a working end-to-end MVP before broadening scope.
- Python is intentionally acceptable for the MVP.
- Linux + Docker is the first worker target.
- Preserve the generic executor boundary; do not tightly couple the worker architecture to Docker.
- Provider resources are always revocable and preemptible.
- Local provider control takes precedence over controller consistency.
- Workers and workloads are mutually untrusted.
- Do not add payments, blockchain, GPU scheduling, WASM execution, Kubernetes, appliance support, or other explicitly deferred features unless the task specifically asks for them.
- Do not add a large automated test suite yet unless explicitly requested. Testing/hardening is a later project phase.
- Prefer simple implementations over speculative abstractions, except for boundaries explicitly required by `PROJECT.md`.

When implementation decisions expose a meaningful conflict with `PROJECT.md`, call out the conflict instead of silently changing the architecture.
