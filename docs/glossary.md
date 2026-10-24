# Glossary

Alphabetical. Definitions are specific to this project; vendor behavior may differ, so
verify on your device.

**ALSA.** Advanced Linux Sound Architecture, the kernel and userspace audio framework
that exposes PCM devices. The agent uses `aplay` and `arecord` against the gadget's ALSA
card.

**AMR-NB.** Adaptive Multi-Rate Narrowband, an 8 kHz speech codec used on 2G/3G and
circuit-switched fallback, with bit rates from about 4.75 to 12.2 kbit/s.

**AMR-WB.** Adaptive Multi-Rate Wideband, a 16 kHz speech codec used for HD Voice on 3G
and 4G, with bit rates from about 6.6 to 23.85 kbit/s.

**barge-in.** The callee speaking while the agent's TTS is playing. The agent must stop
playback immediately so it does not talk over the callee.

**configfs.** A kernel pseudo-filesystem used to create and bind USB gadget functions at
runtime. The UAC1 function is instantiated there.

**CS fallback.** Circuit-switched fallback: an LTE phone without VoLTE drops to 2G or
3G to place or receive a voice call.

**DFP and UFP.** Downstream-facing port and upstream-facing port, the USB-C data roles
for host and device. Power role is negotiated separately, so a DFP can be a power sink.

**Doze.** An Android idle power mode that defers background work. The dial helper should
be exempt from battery optimization.

**dual-SIM.** A phone with two SIM slots or subscriptions. Android needs a phone account
handle to choose which SIM places a call.

**dwc2.** The Synopsys USB 2.0 OTG controller driver used by the Pi Zero 2 W. In
peripheral mode it presents the gadget to the phone.

**E.164.** The international telephone numbering format: a leading plus, country code,
and subscriber number, at most 15 digits.

**endpointing.** Deciding that a speaker has finished an utterance. The agent uses a
300-500 ms silence window before responding.

**EVS.** Enhanced Voice Services, a 4G/5G speech codec that adapts to network conditions
and can carry wideband or super-wideband audio.

**foreground service.** An Android service with a persistent notification, used by the
dial helper so the system is less likely to kill it.

**f_uac1.** The Linux USB gadget function that implements USB Audio Class 1. It provides
the speaker and microphone interfaces used here.

**HFP.** Bluetooth Hands-Free Profile, a profile for car kits and headsets that carries
call control and SCO or eSCO audio.

**host role and device role.** The two USB data roles: the host initiates enumeration
and transfers, while the device responds. Phone A is host and the Pi is device.

**journald volatile.** A systemd-journald setting that keeps logs in RAM and discards
them on reboot, reducing SD card wear.

**libcomposite.** The Linux kernel module that lets configfs assemble USB gadget
functions into a composite device.

**LLM.** Large language model, the component that turns a transcript and context into
the agent's next response.

**mSBC.** Modified SBC, a 16 kHz wideband codec used by HFP for better Bluetooth call
audio than narrowband SCO.

**OTG.** USB On-The-Go, a dual-role USB port that can act as host or device. The Pi Zero
2 W OTG port acts as device.

**phone account handle.** An Android `PhoneAccountHandle` that identifies a SIM or
calling account. It is passed as `TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE` to avoid a
SIM picker.

**plughw.** An ALSA plugin that converts sample format, rate, and channel count to what
the hardware accepts. It is used for the fixed-format gadget.

**PSTN.** Public switched telephone network, the circuit-switched telephone system that
connects the cellular call to the callee.

**SCO.** Synchronous Connection Oriented, a Bluetooth link used for narrowband call
audio. eSCO is its enhanced, retransmitting variant.

**SIM.** Subscriber Identity Module, the chip or profile that identifies a mobile
subscription. SIM A is the agent's number.

**STT.** Speech-to-text, the streaming transcription of the callee's audio into partial
and final text.

**TelecomManager.** The Android system service for placing and managing calls. The
helper uses `placeCall()` and optionally `endCall()`.

**TTFA.** Time to first audio, the delay from a TTS request to the first playable audio
chunk.

**TTFT.** Time to first token, the delay from an LLM request to its first streamed token.

**TTS.** Text-to-speech, the streaming synthesis of the agent's response into audio.

**UAC, UAC1, UAC2.** USB Audio Class, a standard class for audio devices. UAC1 is the
full-speed class 1 subset used here, and UAC2 is the class 2 fallback.

**UPS.** Uninterruptible power supply, a battery-backed supply that covers the hub and
router during a mains outage. Phone A uses its own battery.

**USB gadget.** The Linux device-side USB stack. It lets a board appear as a peripheral,
such as a headset, to a host.

**USB-C PD.** USB-C Power Delivery, a negotiation protocol for voltage and current. The
hub uses it to pass charger power to the phone.

**VAD.** Voice activity detection, distinguishing speech from silence or noise so the
agent knows when to listen and when to stop.

**VBUS.** The 5 V power rail on a USB cable. The source of VBUS determines who powers
whom; here the hub sources it.

**VoLTE.** Voice over LTE, speech carried as RTP over an IMS packet data network managed
by the modem rather than over a circuit.

**watchdog.** A hardware or software timer that reboots a hung system if it is not
serviced. It is separate from systemd's `Restart=always`.
