# Agent Memory Guidelines (Claude Code / Codex)

This repository uses **dreamer** to manage agent memory based on Anthropic's research: *"The components of agent memory"*.

## How to use MEMORY.md:
1. **Always read `MEMORY.md` first**: It contains high-level invariants, verified rules, and topic index pointers (capped at 200 lines).
2. **Never log transient state**: Do not record temporary progress, file diffs, or pending test states. Use git log and test runners for state.
3. **Record only durable invariants**: If you discover an undocumented build requirement, runtime constraint, or verified fix for a recurring failure mode, add it as a rule to `MEMORY.md`.
4. **Deep topics**: If a rule or architecture context is large, look up or create a detailed document in `memory/<topic>.md` and leave a one-line pointer in `MEMORY.md`.
