# Maintaining the Context Brain

This tree is source-controlled project memory, not generated prose. Any change to a workflow route, state field, agent input/output, tool integration, API contract, runtime setting, or operational risk must update its focused Markdown document in the same change.

Prefer adding a small document in the nearest existing folder over extending a general overview indefinitely. Each document should state: purpose, implementation files, inputs/outputs or contract, related references, and failure/safety constraints.

Do not document credentials, personal data, raw prompts, or transient debug artifacts. Keep paths concrete and remove stale assertions as soon as code changes.
