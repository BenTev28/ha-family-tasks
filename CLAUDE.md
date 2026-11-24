# Family Tasks: working rules

A Home Assistant custom integration (`custom_components/family_tasks`): shared family task lists with assignees, on several task services. Read `docs/ARCHITECTURE.md` before changing anything structural.

## Standing requirements

1. **One interface for every service.** Entities, actions (fields and responses), events (payloads), websocket commands, the task object, the card and the options are the same no matter which service a list lives on. Only the backend's *mapping* of parameters and settings differs.
2. **Each service is an adapter** under `backends/<key>/` implementing `TaskBackend` + `BackendSpec` (`backends/base.py`). Service-specific code, wording and settings never leak into `model.py`, `coordinator.py`, `services.py`, `websocket_api.py`, the entities or the card.
3. **Differences are expressed as capabilities** (`native` / `emulated` / `none`, plus flags). The card hides what's unsupported, and actions reject `none` fields with `unsupported_field`. Never silently drop data.
4. **Contract changes are additive and apply to all backends at once.** Update `docs/ARCHITECTURE.md` §1 and the README when you make one.
5. **Planned services:** CalDAV (done), Microsoft To Do, Google Tasks, HA to-do entities (Local To-do). Keep the interface workable for all four.
6. **Data integrity:** edit only what changed and keep unknown properties and tags. Detect conflicts, or document the last-write-wins window.
7. **Security:** never log or expose passwords, tokens or credential-bearing URLs (nothing at INFO or above; redact diagnostics). Never ask the user to paste secrets into chat.
8. **Generic by default:** no personal hostnames, usernames or list names in code or defaults. This is meant for any HA user.

## Commands

- `./scripts_test.sh [pytest args]`: ruff + tests in a Python 3.14 container with a throwaway Radicale.
- `docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest`: HA manifest/translation validation.
- `strings.json` and `translations/en.json` must stay identical.
