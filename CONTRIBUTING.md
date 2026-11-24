# Contributing to Family Tasks

Thanks for helping! This project is small and maintained in spare time, so please open an issue to discuss bigger changes before you start.

## The one rule

Family Tasks has **one interface for every task service**. Entities, actions, events, the websocket API, the task object, the card and the options are identical whether a list lives in Nextcloud, Microsoft To Do, Google Tasks or Home Assistant. Each service is an adapter that *maps* that interface. It never extends it on its own.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything structural. Recorded decisions are in [docs/decisions/](docs/decisions/).

## Branches and pull requests

- `main` is always releasable. CI (tests, lint, hassfest, HACS validation) must pass before a change merges.
- Work on a branch and open a pull request against `main`:
  - fork the repo if you aren't a collaborator
  - `fix/…`, `feat/…` or `docs/…` for general work
  - `backend/<service>` for service adapters (long-running branches for planned services already exist)
- Keep pull requests focused, and fill in the checklist in the PR template.
- Pull requests are squash-merged. Write the PR title as the commit message you'd want in history.

## Development setup

You need Docker. The test script runs everything in a Python 3.14 container, alongside a throwaway Radicale CalDAV server:

```bash
./scripts_test.sh                     # ruff lint + format check + full test suite
./scripts_test.sh tests/test_ics.py   # a subset
docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest
```

To try it in Home Assistant, copy `custom_components/family_tasks` into your config's `custom_components/` folder and restart.

## Adding a task service

Follow the checklist in docs/ARCHITECTURE.md §6. In short:
1. Create an adapter in `backends/<key>/`.
2. Declare its capabilities honestly.
3. Add a `connect_<key>` config-flow step with its own texts.
4. Keep data the integration doesn't manage intact.
5. Detect conflicts.
6. Never log secrets.
7. Pass the shared contract tests.

Please open a "New task service" issue first, so the capability mapping can be agreed.

## Code style

- `ruff` lint and format (configured in `pyproject.toml`), typed Python, and Home Assistant integration conventions.
- `strings.json` and `translations/en.json` must be identical.
- The card is a single dependency-free file (`frontend/family-tasks-card.js`) with no build step. It uses HA theme variables, and escapes user text.

## Security

Never put real credentials in tests, fixtures, issues or logs. Report vulnerabilities privately (see [SECURITY.md](SECURITY.md)).
