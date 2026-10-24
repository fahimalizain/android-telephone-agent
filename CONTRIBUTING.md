# Contributing to android-telephone-agent

This project is at alpha, design stage. Documentation corrections, hardware test
reports, and reference-code improvements are all useful. Read the
[design brief](docs/design-brief.md) before proposing changes to architecture or scope;
it is the source of truth.

By contributing, you agree that your contribution is licensed under the MIT License
(see [LICENSE](LICENSE)) and that you will follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Prerequisites

- Python 3.11 or later for the reference runtime. The runtime uses the standard library
  only; development dependencies are `ruff` and `pytest`.
- `shellcheck` for changes to scripts under `scripts/`.
- `markdownlint-cli2` for documentation. It is optional locally; CI runs it on every
  pull request. Run it with `npx markdownlint-cli2 "**/*.md"`.
- Hardware is not required for documentation or Python changes. For hardware testing,
  see the notes below.

## Repository layout

| Path | Contents |
|---|---|
| `README.md` | Project overview, scope, hardware summary, roadmap |
| `docs/` | Canonical design, build, operation, and reference documentation |
| `docs/decisions/` | Architecture decision records (ADRs) |
| `agent/` | Python reference runtime and tests |
| `android-helper/` | Kotlin reference helper app for placing and ending calls |
| `scripts/` | Bluetooth setup, check, audio-test, dial, and Android helper scripts plus systemd user units |
| `config/` | Example configuration with placeholders only |
| `.github/` | Issue templates, pull request template, and CI workflows |

Every path in the [repository map](README.md#repository-map) is part of the intended
layout. Some paths are placeholders until the corresponding milestone lands.

## Proposing changes

1. Open an issue first for anything larger than a typo or a broken link. Use the bug
   report or feature request form.
2. Fork the repository and create a branch from `main`. Use a short descriptive name,
   for example `docs/operations-clarify` or `fix/sco-reconnect`.
3. Keep the change focused. One topic per pull request makes review and rollback easier.
4. Update documentation in the same pull request as the behavior change. If the change
   alters a hardware assumption, update the design brief first.
5. Run the checks that apply:
   - `npx markdownlint-cli2 "**/*.md"` for documentation.
   - `shellcheck scripts/*.sh` for shell changes.
   - `python -m pytest` and `ruff check` from `agent/` for Python changes.
6. Open a pull request and complete the template, including the testing you performed
   and the hardware you used.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>(<scope>): <imperative summary>

<body: why the change is needed, and anything a reviewer should know>
```

Common types are `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, and `chore`. Use `docs` for documentation-only changes. Keep the subject line
under 72 characters, in the imperative mood, without a trailing period. Reference an
issue in the body when one exists.

## Pull request checklist

- [ ] The change is linked to an issue, or the pull request explains why no issue is
      needed.
- [ ] Documentation is updated, including verification steps and known limitations.
- [ ] Relative links resolve, and no link points to a file that does not exist.
- [ ] No phone numbers, SIM identifiers, API keys, tokens, IMEI numbers, or personal data
      are included.
- [ ] `markdownlint-cli2` passes for changed Markdown.
- [ ] `shellcheck` passes for changed shell scripts.
- [ ] Python tests pass on Python 3.11, and new behavior has a test where practical.
- [ ] Hardware claims are hedged as "verify on your device" unless the Android or
      Bluetooth specification enforces the behavior.
- [ ] `CHANGELOG.md` has an entry under `Unreleased` for user-visible changes.

## Documentation style

- Write in English, in a neutral technical tone. No emojis, no marketing language, and
  no claims that the project is production-ready.
- Use GitHub-flavored Markdown. Use Mermaid for diagrams and tables for parameters,
  pinouts, and bills of materials.
- Start every guide with its purpose and prerequisites. End it with verification steps
  and known limitations where relevant.
- Use relative links for files in this repository, so the docs work in forks and in
  offline clones. Do not use absolute `github.com` links to files in this repository.
- Express rupee amounts as ranges and mark them as indicative. Tell readers to verify
  current prices; hardware prices change.
- Do not invent hardware details. If a detail is not in the design brief or verified on
  a device, mark it as an open question or a placeholder.
- Wrap prose at roughly 80 to 100 columns. Line length is not enforced by the linter,
  but long unbroken paragraphs are hard to review.
- Use one level-one heading per file and do not skip heading levels.
- Code fences must have a language identifier (`bash`, `python`, `text`, `yaml`,
  `mermaid`). Use `text` for trees and console transcripts.

## Hardware testing notes

Hardware behavior varies across vendors, Android builds, Bluetooth adapters, and Linux
distributions. A report is only useful if it identifies the setup:

- Phone model and region, Android version, One UI or vendor build number.
- Linux distribution and version, kernel, BlueZ, PipeWire, and WirePlumber versions.
- Bluetooth adapter model, whether it is onboard or USB, and the negotiated codec
  (mSBC or CVSD).
- RSSI and whether the phone was paired, trusted, and set to **Calls and audio**.
- SIM slot used for the call, whether VoLTE is enabled, and the mobile network operator.
- Whether the SCO source and sink appeared, and the output of `bluetoothctl info`,
  `pw-dump`, and `scripts/bluetooth-check.sh` where relevant.
- Which stage failed: pairing, HFP link, SCO audio, helper, dial, media, inbound answer,
  or hangup.

For helper or inbound failures, include the helper log (with numbers and tokens removed)
and the relevant output of
`journalctl --user -u telephone-agent-daemon.service`. Redact phone numbers, IMEI and SIM
identifiers, tokens, and Wi-Fi credentials from logs before attaching them. State clearly
whether you verified a behavior yourself or are reporting what a vendor documents.

## Reporting security issues

Do not open a public issue for a security problem. Email
[fahimalizain@gmail.com](mailto:fahimalizain@gmail.com) with the details described in
[SECURITY.md](SECURITY.md). You should receive an acknowledgement within 72 hours.
