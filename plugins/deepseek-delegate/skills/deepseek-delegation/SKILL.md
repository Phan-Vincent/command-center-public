---
name: deepseek-delegation
description: Compatibility entry point for explicit DeepSeek delegation requests using the consolidated Reasonix execution and verification policy.
---

# DeepSeek Delegation

Apply [Reasonix Orchestrator](../reasonix-orchestrator/SKILL.md) and its relevant
references. This entry point has no separate routing, retry, Git, or authorization
policy. External work runs in the user's logged-in ChatGPT tab under that policy;
Reasonix handles bounded local access.

The compatibility `scripts/delegate.py` invokes the shared engine. Authorized
writes require `--authorize-writes`; runs perform one attempt and retain changes
for independent supervisor review. There is no automatic commit, stash, reset,
branch, push, or model cascade. `--branch` and repeated `--attempts` are rejected.
Claude-specific environment variables are adapter details, not supervisor policy.

The standalone compatibility package bundles a generated copy of the same core,
engine, and references. It does not depend on name-only discovery of another
installed plugin. Never edit the generated bundle; rebuild from canonical source.
