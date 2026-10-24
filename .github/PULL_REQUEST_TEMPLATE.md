## Summary

<!-- Describe the change and why it is needed. Link the issue if one exists. -->

Closes #

## Type of change

- [ ] Documentation
- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Build, CI, or tooling
- [ ] Hardware or compatibility report

## Testing performed

<!-- List the commands you ran and their results. For documentation, state which pages
you reviewed and whether the links resolve. Write "Not applicable" where appropriate. -->

- [ ] `npx markdownlint-cli2 "**/*.md"`
- [ ] `shellcheck scripts/*.sh`
- [ ] `python -m pytest` from `agent/`
- [ ] `ruff check` from `agent/`
- [ ] Manual review in a Markdown renderer

## Hardware tested

<!-- State the exact devices and software. Write "Not applicable" for docs-only changes.
Do not include phone numbers, SIM identifiers, IMEI numbers, or tokens. -->

| Device | Model | OS or build | Result |
|---|---|---|---|
| Phone A | | | |
| Linux host | | | |
| Bluetooth adapter | | | |

Call path details, if relevant: SIM slot, VoLTE state, mobile network operator, negotiated
codec (mSBC or CVSD), and whether both SCO nodes appeared. For host software, include the
Linux distribution, kernel, BlueZ, PipeWire, and WirePlumber versions.

## Checklist

- [ ] The change is focused on one topic and has a clear description.
- [ ] Documentation is updated, including verification steps and known limitations.
- [ ] Relative links resolve and no link points to a missing file.
- [ ] No secrets, phone numbers, SIM identifiers, IMEI numbers, or personal data are
      included.
- [ ] Markdown linting passes for changed documentation.
- [ ] Hardware claims are hedged as "verify on your device" unless enforced by the
      Android or Bluetooth specification.
- [ ] `CHANGELOG.md` has an entry under `Unreleased` for user-visible changes.
- [ ] I followed the Code of Conduct.
