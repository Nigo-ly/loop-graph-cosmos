# Loop Graph Cosmos

A local-first research and knowledge workbench: submit a public link and a question, collect and assess evidence, produce a useful conclusion, and preserve versioned knowledge that both people and Agents can reuse.

**Developer preview, v0.1.0.** Primarily tested on macOS. Python backend, Obsidian desktop UI, optional Kimi Code / Codex CLI integration. Chinese UI and default note directories. This is not a hosted service, an arbitrary repository installer, or an autonomous self-modifying platform.

Start with the [offline synthetic demo and setup guide](QUICKSTART.md). The demo makes no network or model calls, creates one knowledge item and a correction, preserves both versions, and exposes reusable Agent guidance. Dependency installation needs internet access.

[Architecture](ARCHITECTURE.md) · [Agent read API](AGENT-USAGE.md) · [Limitations](LIMITATIONS.md) · [Validation](VALIDATION.md)

Live research is explicitly enabled and uses the caller's own configured subscription CLI. No credentials or personal knowledge base are included. Local APIs must stay on loopback; they are not a multi-user authentication boundary.

MIT licensed. Contributions are most useful as reproducible failures: wrong conclusions, missing evidence, stuck progress, fresh-machine setup, and failures to reuse previously stored knowledge.
