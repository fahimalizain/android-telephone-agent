# Security Policy

This project is at alpha, design stage. It places real telephone calls and handles call
audio, so security reports are taken seriously even though no production support is
promised.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes, security fixes are applied to the latest 0.1.x release |
| Earlier or unreleased snapshots | No |

There is no long-term support branch. If you are running an unreleased commit, reproduce
the issue against the latest `main` before reporting it.

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Email [fahimalizain@gmail.com](mailto:fahimalizain@gmail.com) with:

- A description of the issue and its potential impact.
- Steps to reproduce, including the affected file, commit, or release.
- The hardware and software involved, if the issue depends on a device or OS build.
- Any proof-of-concept material, with secrets and personal data removed.
- Whether you intend to disclose the issue publicly, and on what timeline.

You should receive an acknowledgement within 72 hours. If you do not, send a follow-up.
Valid reports will be investigated, fixed on the supported branch, and credited in the
release notes unless you ask to remain anonymous. This project does not operate a bug
bounty program and cannot offer payment.

Please give the maintainer a reasonable opportunity to release a fix before public
disclosure. Coordinated disclosure timelines are agreed case by case.

## Threat model

The relevant assets are the agent SIM's phone number, call audio and transcripts, helper
and provider credentials, and control of the phone's dialer. The main threats are:

- **Helper HTTP endpoint on the LAN.** The Android helper accepts call requests over the
  local network. It binds to a private interface and requires a bearer token. It must
  never be port-forwarded or exposed to the internet. Anyone on the same LAN who obtains
  the token can place calls, so the token is a secret and should be rotated if leaked.
  Treat the LAN as only as trustworthy as its weakest device.
- **Secrets in the repository.** Phone numbers, SIM identifiers, API keys, helper
  tokens, and Wi-Fi credentials must never be committed. `config/config.example.toml`
  and `.env.example` contain placeholders only. If you commit a secret by accident,
  rotate it immediately; removing it from a later commit does not remove it from history.
- **Call audio leaves the LAN.** Audio and transcripts are sent to third-party
  speech-to-text and text-to-speech providers, and the LLM provider sees transcripts and
  call context. Those providers can observe the conversation subject to their own
  retention policies. Choose providers and regions deliberately, keep audio persistence
  disabled by default, and avoid saying anything on a call that you would not send to
  those providers.
- **Personal data in logs and reports.** Call metadata, phone numbers, and transcripts
  can identify the owner or the callee. Redact identifiers before sharing logs in an
  issue. Volatile journald storage reduces disk writes; that is not a substitute for
  redaction.
- **Physical and host access.** Anyone with physical access to the phone or the Linux host
  can read configuration and place calls. Use screen lock on the phone, keep the host in a
  controlled space, and do not reuse the helper token elsewhere.
- **Regulatory and abuse risk.** The project is for personal use. Using it for spam,
  impersonation, or caller-ID evasion is misuse, not an accepted feature.

## Out of scope

The following are generally out of scope for this policy:

- Vulnerabilities in Android, One UI, the Linux distribution, BlueZ, PipeWire,
  WirePlumber, the cellular network, or third-party STT, LLM, and TTS providers. Report
  those to the respective vendor.
- Attacks that require an unlocked bootloader, root, a malicious system image, or
  prolonged physical access to the phone or host.
- Denial of service against your own LAN, and misconfiguration of a router or firewall
  that exposes the helper endpoint.
- Regulatory compliance, consent for recorded conversations, and lawful use of
  outbound calling. These are operator responsibilities described in the README and the
  design brief.
- Issues that only affect an unsupported version or a local modification that is not in
  this repository.

## Operator guidance

- Keep the helper endpoint on a private interface and require its bearer token.
- Rotate the helper token and provider keys after any suspected exposure.
- Keep the phone and host patched, and review the helper app's permissions.
- Monitor heartbeat alerts for unexpected calls or failed calls.
- Do not record calls unless you have the consent required in your jurisdiction.
