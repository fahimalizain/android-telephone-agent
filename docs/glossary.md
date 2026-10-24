# Glossary

Alphabetical. Definitions are specific to this project; vendor behavior may differ, so
verify on your device.

**AG (audio gateway).** The Bluetooth role that connects a phone's cellular call audio to
a headset. In this project Phone A is the AG and the Linux host is the HF.

**ALSA.** Advanced Linux Sound Architecture, the kernel and userspace audio framework.
PipeWire sits above it on the host; the agent uses PipeWire tools rather than addressing
an ALSA card directly.

**AMR-NB.** Adaptive Multi-Rate Narrowband, an 8 kHz speech codec used on 2G/3G and
circuit-switched fallback, with bit rates from about 4.75 to 12.2 kbit/s.

**AMR-WB.** Adaptive Multi-Rate Wideband, a 16 kHz speech codec used for HD Voice on 3G
and 4G, with bit rates from about 6.6 to 23.85 kbit/s.

**barge-in.** The callee speaking while the agent's TTS is playing. The agent must stop
playback immediately so it does not talk over the callee.

**CS fallback.** Circuit-switched fallback: an LTE phone without VoLTE drops to 2G or
3G to place or receive a voice call.

**CVSD.** Continuously Variable Slope Delta modulation, the narrowband 8 kHz codec used
by Bluetooth SCO when mSBC is not negotiated.

**Doze.** An Android idle power mode that defers background work. The dial helper should
be exempt from battery optimization.

**dual-SIM.** A phone with two SIM slots or subscriptions. Android needs a phone account
handle to choose which SIM places a call.

**E.164.** The international telephone numbering format: a leading plus, country code,
and subscriber number, at most 15 digits.

**endpointing.** Deciding that a speaker has finished an utterance. The agent uses a
300-500 ms silence window before responding.

**EVS.** Enhanced Voice Services, a 4G/5G speech codec that adapts to network conditions
and can carry wideband or super-wideband audio.

**foreground service.** An Android service with a persistent notification, used by the
dial helper so the system is less likely to kill it.

**helper.** The small Android app on Phone A that places and answers calls over the LAN
with `TelecomManager`. It is the signaling path; Bluetooth carries the audio.

**HF (hands-free unit).** The Bluetooth role that acts as the headset. The Linux host is
the HF in this project.

**HFP.** Bluetooth Hands-Free Profile, a profile for car kits and headsets that carries
call control and SCO or eSCO audio. The phone is the audio gateway (AG); the headset or
car kit is the hands-free unit (HF). HFP is the only supported audio transport here.

**inbound.** A call from a person to the agent's SIM. The daemon's opt-in inbound watcher
polls the helper, answers the call, and runs a Grok Voice session. It runs in the same
daemon as outbound calls and shares the one call slot with them, and it answers every call
with no caller-ID filter.

**journald volatile.** A systemd-journald setting that keeps logs in RAM and discards
them on reboot, reducing disk writes.

**LLM.** Large language model, the component that turns a transcript and context into
the agent's next response.

**mSBC.** Modified SBC, a 16 kHz wideband codec used by HFP for better Bluetooth call
audio than narrowband SCO.

**phone account handle.** An Android `PhoneAccountHandle` that identifies a SIM or
calling account. It is passed as `TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE` to avoid a
SIM picker.

**PipeWire.** The Linux media server that owns the host's audio graph. During an HFP call
it exposes the downlink as a SCO source and the uplink as a SCO sink; the agent captures
and plays those nodes with `pw-record` and `pw-play`.

**PSTN.** Public switched telephone network, the circuit-switched telephone system that
connects the cellular call to the callee.

**Realtime speech-to-speech.** A single full-duplex model session that recognizes speech,
decides the reply, and synthesizes audio, instead of separate STT, LLM, and TTS stages.
The Grok Voice adapter is the optional implementation in this runtime.

**SCO.** Synchronous Connection Oriented, a Bluetooth link used for call audio. It carries
narrowband CVSD or wideband mSBC; eSCO is its enhanced, retransmitting variant. SCO nodes
exist only while a call or SCO link is active.

**SIM.** Subscriber Identity Module, the chip or profile that identifies a mobile
subscription. SIM A is the agent's number.

**STT.** Speech-to-text, the streaming transcription of the callee's audio into partial
and final text.

**TelecomManager.** The Android system service for placing and managing calls. The
helper uses `placeCall()`, `acceptRingingCall()`, and optionally `endCall()`.

**TTFA.** Time to first audio, the delay from a TTS request to the first playable audio
chunk.

**TTFT.** Time to first token, the delay from an LLM request to its first streamed token.

**TTS.** Text-to-speech, the streaming synthesis of the agent's response into audio.

**VAD.** Voice activity detection, distinguishing speech from silence or noise so the
agent knows when to listen and when to stop.

**VoLTE.** Voice over LTE, speech carried as RTP over an IMS packet data network managed
by the modem rather than over a circuit.

**watchdog.** A hardware or software timer that reboots a hung host if it is not
serviced. It is separate from systemd's `Restart=always`.

**WirePlumber.** The session and policy manager for PipeWire. It loads the HFP
hands-free role, manages the Bluetooth device, and creates the SCO nodes during a call.
It runs per user, alongside PipeWire.
