# ADR 0002: Power the phone and gadget through a USB-C PD hub

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

Phone A and the Pi need both a USB data path and electrical power for 24/7 operation.
The phone is the USB data host in this design: it enumerates the Pi as a headset. It is
also a power sink. The Pi is the USB device and must be powered. These roles are
independent in USB-C, but they are easy to wire incorrectly.

If the phone sources VBUS through an OTG cable, the Pi can run, but the phone
discharges continuously and cannot charge during a call. The Pi's current draw is
roughly 150-300 mA typical with peaks near 400 mA, which is close enough to a phone's
OTG budget to cause brownouts when the cellular radio transmits. A powered hub avoids
that conflict: it sources VBUS to the phone and the Pi while the phone remains the
data host.

The canonical charger is a 25 W USB-C PD unit. The hub must accept charger power on its
PD input, pass data between the phone and the Pi, and provide at least one USB-A port
for the Pi. A USB 2.0 USB-A port is limited to 500 mA, so the Pi's peak draw must stay
within that budget or the installation must use the Pi's separate PWR port.

## Decision

The charger feeds the hub's USB-C PD input. The hub supplies PD power to Phone A over
the USB-C data link and 5 V to the Pi over a USB-A to micro-USB cable. Phone A is the
USB data host and a power sink; the Pi is the USB device and is bus-powered. The phone
never sources VBUS to the gadget.

If the hub's downstream port proves unstable, the Pi is powered through its separate
PWR port from a 5 V/2 A supply, while the USB-A link carries data only. That is a
fallback, not the canonical topology. The mini UPS covers the hub and the Wi-Fi router;
Phone A remains on its own battery. The exact UPS-to-hub interconnection must be
verified for the chosen hardware, because a 5 V router UPS and a 25 W PD hub do not
necessarily share the same input arrangement.

## Consequences

### Positive

- One cable carries power and data between Phone A and the hub.
- Phone A charges while the agent is running, which matters for a 24/7 installation.
- The phone does not have to source VBUS, avoiding OTG current limits, brownouts, and
  the charging conflict.
- The Pi is bus-powered from the hub, so it needs no separate supply in the common case.
- The topology matches standard USB-C dock behavior: a data host can be a power sink.
- The phone's Protect battery setting can cap charge at 80-85% to reduce swelling.

### Negative

- The hub is a single point of failure for both power and USB data.
- Not every USB-C hub passes data reliably while accepting PD power; the hub must be
  tested with this exact phone and Pi.
- The USB-A port's current limit may be marginal at the Pi's peak; the separate PWR
  port may be needed.
- The hub adds heat and another cable, and it occupies space near the phone.
- PD negotiation can fail silently, leaving the phone charging slowly or not at all.
- A hub that backfeeds power or floats VBUS can confuse the phone's role detection.

### Risks

- **Hub brownout during radio transmission.** LTE transmit bursts draw current; a weak
  hub or long cable can drop the Pi. Use a short, quality cable and check
  `vcgencmd get_throttled` after test calls.
- **Phone data role switch.** A hub or cable that presents the wrong role can make the
  phone stop enumerating the gadget. Verify the USB audio device appears after every
  reconnect.
- **PD negotiation failure.** Test with the canonical 25 W charger and confirm the phone
  reports charging while the gadget is active.
- **Hub thermal limit.** Passive hubs can throttle or drop ports when warm; keep the
  installation ventilated.
- **UPS mismatch.** A router-class 5 V UPS may not be able to feed a PD hub input.
  Verify the power path and measure runtime under a live call.

## Alternatives considered

### Phone-VBUS-only prototype

A direct USB-C OTG connection from Phone A to the Pi is useful for a phase 0 proof:
it removes the hub and proves that the phone enumerates the gadget. It is rejected for
the canonical build because the phone powers the Pi, drains its own battery, cannot
charge during the call, and risks brownouts at the Pi's peak current. It remains a
valid bench test before the hub arrives.

### Self-powered hub with Qi charging

A self-powered hub plus a Qi charging pad would separate data and power cleanly and
avoid PD passthrough entirely. The Galaxy A23 4G has no wireless charging, so this
would require a different phone or an add-on receiver. It also adds a second power
supply and a charging pad. It is a reasonable option for other phones, not for the
canonical hardware.

### Separate Pi supply and data-only cable

Powering the Pi from its own 5 V supply while using a data-only USB link to the hub is
a valid fallback and removes the Pi's load from the hub. It adds a second supply,
another cable, and a second failure domain. It is retained as the instability
workaround rather than the default.

## References

- [Design brief](../design-brief.md), section 6.
- [Architecture](../architecture.md), sections 5 and 9.
- [Hardware](../hardware.md).
- [Power and thermal](../power-and-thermal.md).
- [ADR 0003: Raspberry Pi Zero 2 W](./0003-run-on-pi-zero-2w.md).
