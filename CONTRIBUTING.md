# Contributing

Start with a reproducible user problem. Include the minimal public/synthetic input, expected answer or transition, actual result, OS/runtime versions, and relevant redacted error code.

- Run `python -m pytest -q` in `backend` and `npm test && npm run build` in `obsidian`.
- Tests must use temporary vaults/databases and fake transports; do not use another person's live credentials, notes or services.
- Add a counterexample for changes to evidence coverage, execution scope, recovery, history or permissions. A successful build alone does not prove the user's question was answered.
- Prefer existing mechanisms and standard libraries. Avoid new orchestration frameworks or dependencies without a concrete unmet need.
- Preserve source identity, authorization scope, CAS history and truthful blocked states. Never turn a missing result into success to make tests green.
- Do not commit `.env`, credentials, raw production logs, personal notes, database files or screenshots of private work.

The public repository is a clean source export; personal deployment history and integration fixtures are intentionally absent. Historical code labels may remain where changing them would break protocol compatibility.
