# Family Tasks: working rules

A Home Assistant custom integration (`custom_components/family_tasks`): shared family task lists with assignees, on several task services. Read `docs/ARCHITECTURE.md` before changing anything structural.

## Standing requirements

1. **One interface for every service.** Entities, actions (fields and responses), events (payloads), websocket commands, the task object, the card and the options are the same no matter which service a list lives on. Only the backend's *mapping* of parameters and settings differs.
2. **Each service is an adapter** under `backends/<key>/` implementing `TaskBackend` + `BackendSpec` (`backends/base.py`). Service-specific code, wording and settings never leak into `model.py`, `coordinator.py`, `services.py`, `websocket_api.py`, the entities or the card.
3. **Differences are expressed as capabilities** (`native` / `emulated` / `none`, plus flags). The card hides what's unsupported, and actions reject `none` fields with `unsupported_field`. Never silently drop data.
4. **Contract changes are additive and apply to all backends at once.** Update `docs/ARCHITECTURE.md` §1 and `docs/usage.md` when you make one.
5. **Planned services:** CalDAV (done), Microsoft To Do, Google Tasks, HA to-do entities (Local To-do). Keep the interface workable for all four.
6. **Data integrity:** edit only what changed and keep unknown properties and tags. Detect conflicts, or document the last-write-wins window.
7. **Security:** never log or expose passwords, tokens or credential-bearing URLs (nothing at INFO or above; redact diagnostics). Never ask the user to paste secrets into chat.
8. **Generic by default:** no personal hostnames, usernames or list names in code or defaults. This is meant for any HA user.

## Workflow

- Every change reaches `main` through a pull request, never a direct push. Work on a branch, commit as you go with clear messages, push the branch, and open a PR. `main` is protected: the required CI checks (`tests`, `lint`, `card-lint`, `hassfest`, `hacs`) must pass and a code owner must approve. Don't merge with `--admin` (bypassing review) unless the user says so for that PR.
- One long-running branch per planned service: `backend/ha-todo` (next), `backend/microsoft-todo`, `backend/google-tasks`. Merge them to `main` through a pull request when done.
- Record significant design decisions in `docs/decisions/NNNN-title.md`.
- Contributor-facing rules are in `CONTRIBUTING.md`; keep them consistent with this file.
- Docs layout: the README is a short front page (HACS renders it). User reference is in `docs/usage.md`, per-service details in `docs/services/<key>.md`, design in `docs/ARCHITECTURE.md`.
- Releases come from `main` (see CONTRIBUTING "Releases"). Bump `manifest.json` `version`, `const.VERSION` and the card's `CARD_VERSION` together. Betas (`X.Y.Zb1`) are GitHub pre-releases, and `release.yml` enforces both rules. Label PRs for the generated release notes (`.github/release.yml`).
- The GitHub account name is going to change: don't hard-code the account name anywhere new; derive it with `gh api user -q .login`.

## Commands

- `./scripts_test.sh [pytest args]`: ruff + tests in a Python 3.14 container with a throwaway Radicale.
- `docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest`: HA manifest/translation validation.
- `strings.json` and `translations/en.json` must stay identical.
