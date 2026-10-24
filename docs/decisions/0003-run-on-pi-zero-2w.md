# ADR 0003: Run on a Raspberry Pi Zero 2 W

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

The runtime must run continuously, present a UAC1 gadget over USB, reach cloud
providers over Wi-Fi, and fit a small, ventilated enclosure near the phone. It must
also be inexpensive enough that the total build stays near the project's indicative
budget. The canonical hardware is the Raspberry Pi Zero 2 W: a quad-core Cortex-A53,
512 MB of RAM, 2.4 GHz Wi-Fi, a micro-USB OTG port, and a separate PWR port.

The separate PWR port matters. It allows the board to be powered without backfeeding
5 V into the GPIO header, which bypasses input protection and risks damage or brownouts.
The OTG port can then carry the gadget data link and, in the canonical topology, bus
power as well.

The Pi 4 Model B 2 GB is a supported alternative at roughly ₹3,000 more. It adds
Ethernet and more RAM and CPU headroom for local speech models. It must be powered
through the GPIO header so its USB-C port remains available for gadget mode. The Pi 5
is rejected: its USB-C port is power-only and cannot operate in USB device mode, so it
cannot present the UAC gadget at all.

## Decision

The Raspberry Pi Zero 2 W is the canonical runtime. It runs Raspberry Pi OS Lite
64-bit (Bookworm or later) from a 32 GB A1/A2 microSD card, with the gadget built from
configfs and `f_uac1`. The Pi 4 Model B 2 GB is supported as an alternative for wired
Ethernet or local-model experiments, powered through GPIO. The Pi 5 is not supported
as the gadget host.

The design assumes the Zero 2 W is network-bound, not CPU-bound: STT, LLM, and TTS run
in the cloud. Audio I/O, resampling, VAD, and buffering fit comfortably in one
primarily single-threaded pipeline. The control loop is effectively a single-core
workload; the quad-core CPU provides headroom for system tasks, not for local
inference. The 512 MB of RAM is the hard constraint: local Whisper or Piper models are
a stretch goal for a Pi 4 or Pi 5, not for this board.

## Consequences

### Positive

- The board is small, low-power, and passively cooled, which suits a 24/7 installation.
- The separate PWR port avoids GPIO power hacks and keeps the OTG port for data.
- The micro-USB OTG port and `dwc2` support USB device mode, which the design requires.
- The board is widely available, well documented, and inexpensive.
- 2.4 GHz Wi-Fi is adequate for audio streaming and control traffic.
- The same software stack runs on a Pi 4 with only power and device-tree differences.
- A 32 GB A1/A2 card is enough for the OS, runtime, and logs when journald is volatile.

### Negative

- 512 MB of RAM rules out comfortable local speech models and large buffers.
- The CPU is modest; a single slow stage can delay the whole pipeline.
- 2.4 GHz Wi-Fi is susceptible to congestion and shares spectrum with Bluetooth.
- There is no wired Ethernet on the Zero 2 W; the Pi 4 is needed for that.
- microSD cards wear out and can corrupt after power loss.
- USB 2.0 and the single OTG port limit expansion and debugging options.
- The board can throttle under sustained load in a closed enclosure.

### Risks

- **SD card corruption.** Use a quality card, volatile journald, a hardware watchdog,
  and a UPS. Keep a known-good image for recovery.
- **Wi-Fi instability.** Prefer a 2.4 GHz access point with a strong signal; monitor
  reconnect events and provider timeouts.
- **Undervoltage.** Check `vcgencmd get_throttled` after test calls and power the Pi
  from the PWR port if the hub port is marginal.
- **Thermal throttling.** Ventilate the enclosure and avoid stacking the Pi against the
  phone or hub.
- **Supply shortages.** The Pi 4 alternative uses the same software but a different
  power arrangement; document both paths.

## Alternatives considered

### Raspberry Pi 4 Model B 2 GB

The Pi 4 is the supported alternative. It offers Ethernet, more RAM, and enough CPU to
experiment with local Whisper.cpp and Piper. It costs more, draws more power, and needs
GPIO power to keep the USB-C port free for gadget mode. It is not the default because
the Zero 2 W is sufficient for the cloud-bound pipeline and fits the budget.

### Raspberry Pi 5

The Pi 5 is rejected because its USB-C port is power-only and cannot act as a USB
device. Without device mode there is no UAC gadget and therefore no headset path. A Pi
5 could run the agent against an external USB gadget board, but that adds hardware and
complexity for no benefit in this design.

### Raspberry Pi Zero W

The original Zero W has a single-core CPU and less headroom. It can run a simple audio
bridge, but it is a poorer fit for a 24/7 pipeline with resampling, VAD, and logging.
The Zero 2 W is preferred for its quad-core CPU and better availability.

### Mini PC or the phone itself

A mini PC would provide ample CPU and RAM but at higher cost, size, and power draw. The
phone cannot host the agent and be the called endpoint at the same time; its audio would
go to the local speaker, not to a remote party. Neither is a better fit for this build.

## References

- [Design brief](../design-brief.md), sections 5 and 6.
- [Architecture](../architecture.md), sections 5 and 11.
- [Raspberry Pi setup](../raspberry-pi-setup.md).
- [Power and thermal](../power-and-thermal.md).
- [ADR 0002: PD hub power topology](./0002-power-phone-and-gadget-through-pd-hub.md).
