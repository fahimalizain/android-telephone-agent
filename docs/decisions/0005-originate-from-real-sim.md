# ADR 0005: Originate from a real SIM

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

The call must reach the owner's ordinary mobile phone with a recognizable caller ID.
The project could originate through a SIP trunk, a cloud telephony API, a GSM gateway,
or a real SIM in an Android phone. Each option produces a different caller-ID
experience, cost structure, and operational burden.

A SIP trunk or a Twilio-style API is inexpensive per minute, scales to many concurrent
calls, and needs no phone hardware. Its caller ID is a VoIP number. Some callees screen
such numbers, some carriers treat them differently, and the callee needs a working data
or voice-over-IP path for some endpoints. The project's premise is a call from a real
mobile number to an ordinary phone, so the caller-ID mismatch is material.

A real SIM in Phone A provides a genuine mobile number. The callee answers on any
phone, with no app and no data connection. The costs move to hardware and operations:
a spare phone, a Linux host, a charger, and a SIM plan. The design supports one
call at a time per phone and host pair, which is acceptable for a personal agent.

## Decision

The reference system originates calls from SIM A in Phone A. The Linux host asks a helper app
on the phone to place the call through `TelecomManager.placeCall()` with the agent
SIM's phone account handle. The call leg is a genuine cellular call over the PSTN. No
SIP trunk, cloud telephony number, or GSM gateway is part of the primary path.

The system is for personal, consenting use. It must not be used for spam,
impersonation, or evasion of caller-ID rules. The operator is responsible for
complying with local rules, including TRAI's TCCCPR in India and the TCPA and FCC
rules on AI-generated voices in the United States.

## Consequences

### Positive

- Caller ID is a real mobile number that the callee can recognize and call back.
- The callee needs no app, no data connection, and no VoIP service.
- There is no telephony provider account, number rental, or per-number fee.
- The call works when the callee cannot or will not receive VoIP calls.
- The phone's own dialer, SIM, and carrier handle network compatibility.
- The design does not depend on a provider's API terms or number reputation.
- The same SIM can be used for ordinary calls when the agent is idle.

### Negative

- One phone and one SIM support one call at a time.
- The build needs a spare Android phone, a Linux host, a charger, and a SIM plan.
- Phone A must be maintained: updates, battery health, reboots, and SIM credit.
- Cellular coverage and carrier VoLTE behavior become part of the reliability model.
- Call charges and SIM plan limits apply, even if they are modest.
- Programmatic hangup is not guaranteed; `endCall()` may require privileged access.
- Caller ID cannot be chosen or spoofed, which is a feature but limits flexibility.
- Outbound calling at scale is regulated and may require consent and record-keeping.

### Risks

- **Regulatory breach.** Automated or unsolicited calls may violate TRAI TCCCPR, the
  TCPA, or other rules. Keep the use personal and consent-based; document the operator's
  responsibility.
- **Carrier terms.** A SIM plan may prohibit automated calling or have fair-use limits.
  Review the plan and monitor usage.
- **Phone failure.** A reboot, update, dead battery, or killed helper app prevents
  dialing. Use a heartbeat, battery protection, and a watchdog.
- **SIM credit or registration loss.** The call fails before dialing; preflight should
  detect it where possible.
- **Battery swelling.** A phone left charging 24/7 can swell. Use Protect battery, keep
  the case off, ventilate, and inspect monthly.
- **Caller-ID reputation.** A number used for unwanted calls can be blocked. Use the
  system only for calls the recipient expects.

## Alternatives considered

### SIP trunk or Twilio-style API

A SIP trunk or cloud API is cheaper at low volume, scales horizontally, and needs no
phone. Its caller ID is a VoIP number, it requires a provider account and data path, and
it may be screened or blocked. It was rejected because the project's value is a call
from a real mobile number to an ordinary phone.

### GSM gateway or SIM box

A GSM gateway holds SIMs and exposes them to a SIP PBX. It is designed for continuous
operation but is expensive, bulky, and subject to licensing and interconnect rules that
vary by country; in India, GSM gateways and SIM boxes are regulated. It was rejected on
cost, regulatory risk, and complexity.

### VoIP app or messaging call

A VoIP app or a messaging service's voice call would avoid the PSTN entirely, but the
callee would need the same app and a data connection. It does not satisfy the premise
of reaching an ordinary phone number. It was rejected for the primary path.

### Landline or fixed-line adapter

A landline gives a real number and reliable voice, but adds a subscription, a location
constraint, and an analog adapter; it does not travel with the owner. It was rejected
for the reference build.

## References

- [Design brief](../design-brief.md), sections 1, 8, and 12.
- [Architecture](../architecture.md), sections 6 and 9.
- [Android setup](../android-setup.md).
- [Dial helper](../dial-helper.md).
- [Operations](../operations.md).
