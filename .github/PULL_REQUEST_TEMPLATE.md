## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Type

- [ ] Bug fix
- [ ] New feature (all services)
- [ ] New / changed service adapter (`backends/…`)
- [ ] Card
- [ ] Docs / CI

## Checklist

- [ ] The public contract is unchanged, or the change is additive and works for **every** service (docs/ARCHITECTURE.md §1 updated)
- [ ] Service-specific code stays in `backends/<service>/`
- [ ] Unknown task properties/tags are preserved; concurrent edits are not silently overwritten
- [ ] No credentials, tokens or credential-bearing URLs in logs (INFO+) or diagnostics
- [ ] Tests added/updated; `./scripts_test.sh` passes
- [ ] `strings.json` and `translations/en.json` match
- [ ] README / ARCHITECTURE updated if behaviour changed
