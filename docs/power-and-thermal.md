# Power and Thermal

Status: design complete, pre-implementation
Last updated: 2026-10-24

This guide covers USB-C power roles, measurement, Pi power, battery care, UPS sizing, monsoon recovery, and thermal limits for Kerala. Read [hardware.md](hardware.md) first; the [design brief](design-brief.md) is authoritative.

Prerequisites: the assembled build from [hardware.md](hardware.md), a phone with Protect battery,
a router, a mini UPS, and, for measurements, an inline USB power meter.

## 1. USB-C power roles

USB-C separates data role from power role. Data role is DFP (downstream-facing port, host) or
UFP (upstream-facing port, device); power role is source (provides VBUS) or sink (consumes it).
The roles are independent, and USB Power Delivery (PD) can swap power without changing data.

VBUS is the power conductor. USB 2.0 uses 5 V; PD negotiates 5 V, 9 V, 15 V, or 20 V over the
Configuration Channel (CC) pins, with optional PPS and a current limit. The hub's USB-A port is
fixed 5 V; the phone's PD contract is separate.

| Port | Data role | Power role | Role in this build |
|---|---|---|---|
| Phone USB-C | DFP (host) | Sink | Enumerates the hub and Pi; charges from the hub |
| Hub upstream | UFP (device) | Source | Presents a dock to the phone; passes charger power |
| Hub USB-A downstream | DFP (host) | Source (5 V) | Enumerates the Pi; supplies its VBUS |
| Pi OTG | UFP (device) | Sink | UAC1 gadget; bus-powered |

The phone being DFP and sink at once is normal for a dock. A proper hub negotiates a PD
power-role swap so the phone sinks power while remaining the data host; the hub becomes the
source. This is why the phone neither sources VBUS to the gadget nor loses fast charging in a
proper dock. The hub and phone firmware must implement it; verify on your device.

If the Pi were connected directly, the phone would source its VBUS, run in OTG host-power mode, drain its battery, and may current-limit the Pi. A charge-only splitter may parallel VBUS or skip the role swap; it is excluded.

"Fast charging preserved" means the hub passes the charger's PD contract instead of forcing
5 V. The phone still requests its own profile; a 25 W charger does not give 25 W to a phone
that asks for less. If it shows "Charging", the hub may have fallen back to 5 V. Measure.

## 2. Measuring power

An inline USB power meter is the only practical way to confirm the budget. Use a USB-C
PD-capable meter for the charger and phone paths and a USB-A meter for the Pi. Planning values:

| Point | Meter | Expected | Notes |
|---|---|---|---|
| Charger to hub | USB-C PD | 10–30 W charging; 2–6 W idle | Total input; meter must pass PD |
| Hub to phone | USB-C | 10–25 W charging; tapers near the cap | May interrupt data; confirm enumeration |
| Hub to Pi | USB-A | 0.75–1.5 W typical; about 2 W peak | 150–300 mA / 400 mA at 5 V, per brief |

Procedure: measure the charger input at baseline (phone 50%, screen off, Wi-Fi on, no call, Pi idle), during a ten-minute active call, and at the 80–85% cap. Measure the Pi branch separately and remove the meters afterward.

Meters add resistance and can cap current; a cheap USB-C meter may not pass PD, and a USB-A
meter in the Pi path can break enumeration. If the gadget disappears, remove the meter. Treat
readings as ±10–20%.

## 3. Pi Zero 2 W power

Power the Pi from the hub's USB-A port first, with a short data-rated cable and the OTG port,
not PWR. The brief's figures are 150–300 mA typical and about 400 mA peak at 5 V. The hub must
have enough total budget for the Pi and its own overhead.

Fallback: use the PWR port with a 5 V/2 A supply. If the OTG port stays connected to a powered
hub, use a data-only cable with VBUS disconnected, or verify that the hub does not back-feed;
otherwise two 5 V sources are tied. Verify on your device.

Brownout symptoms: the phone drops the UAC device; `arecord -l` loses the gadget and ALSA logs
xruns; `dmesg` shows `Under-voltage detected!`; `get_throttled` is non-zero (bit 0 now, bit 16
since boot); Wi-Fi drops or the Pi reboots.

```sh
vcgencmd get_throttled; vcgencmd measure_temp
dmesg | grep -iE 'voltage|throttl|usb|gadget'; arecord -l; cat /sys/class/udc/*/state
```

Fixes, in order: a shorter or thicker cable; a hub port rated for at least 500 mA; a hub with
more power budget; the PWR fallback. Do not power the Pi from the phone directly.

## 4. Phone battery care

- **Protect battery ON.** One UI caps charging at 80% or 85% depending on the build; verify on
  your device. This is the most useful setting for a phone powered most of the day.
- **Heat is worse than a slightly higher charge voltage.** Keep the phone out of direct sun and
  closed boxes. Charging at 25 W, a long call, and 30 °C ambient all add heat; improve airflow.
- **Monthly swelling check.** With the case off, place the phone on a flat table and look for a
  screen lift, a bulging back, or a rock. A swollen battery is a fire risk: stop charging and
  have it replaced at a service center.
- **Do not keep it at 100% in a drawer.** A battery at 100% and 30 °C or more ages faster than
  one at 50–80%. Store a spare at about 50% and power it off; for this build, use the cap.
- **Avoid deep discharge.** Let the phone run on its battery during an outage, but recharge when
  mains returns. Do not disable the cap for outage insurance; 80% is hours of talk time.

## 5. UPS sizing

The UPS keeps the hub (and therefore the Pi) and the router alive during a cut; the phone runs on its own battery. It is a ride-through, not a generator; figures below are assumptions:

```
runtime_h = (battery_Wh × depth_of_discharge × conversion_efficiency) / load_W
```

Worked example: a 5 V load of about 1 A (Pi plus hub) is 5 W; a router assumed at 10 W makes
15 W total (check the label; 12 V/1 A is 12 W). A 10 Wh battery at 80% depth of discharge
gives 8 Wh usable; at 80% inverter or converter efficiency, runtime is 8 × 0.8 / 15 = 0.427 h,
about 26 minutes. Without the depth-of-discharge derate: 10 × 0.8 / 15 = 0.533 h, about
32 minutes. Treat 26–32 minutes as the planning range.

| Load | Runtime at 10 Wh, 80% DoD, 80% efficiency |
|---|---:|
| 5 W (Pi and hub only) | about 77 minutes |
| 15 W (Pi, hub, router) | about 26 minutes |
| 40 W (adds 25 W phone charging) | about 10 minutes |

The phone dominates the load if it is below its cap; below 80–85% it may draw up to 25 W and
cut runtime. For a long outage, keep the phone connected (agent available) or let it run on its
own battery (agent offline, phone lasts longer). A 10 Wh unit is marginal; 20–30 Wh is better.

Verify Wh, output voltages, continuous rating, and transfer behavior with a real mains pull;
derate for age and test annually. If it has only 5 V and the router needs 12 V, choose the
correct output. An AC outlet lets the Samsung charger keep the hub's PD contract; 5 V-only will not.

## 6. Kerala monsoon and outages

Monsoon brings outages, humidity, and water risk. The UPS covers the hub and router; the phone
has its own battery, though it may draw charging current from the hub. Without a UPS, a cut
stops the Pi and router and the agent goes offline.

- Keep the Pi, hub, charger, and UPS above floor level, away from window spray, with drip
  loops. Do not put the UPS on a wet floor.
- Use a surge protector; a UPS is not lightning protection. Inspect connectors monthly for
  corrosion and keep the build ventilated rather than sealed.
- If a call is in progress when the UPS dies, the phone may keep the cellular call alive but
  the agent is mute. Set a hard maximum call duration and a heartbeat.

Recovery checklist:

1. Confirm mains is restored and the UPS is charging; check the router LEDs and internet.
2. Check the hub LED and phone charging; confirm the Pi boots.
3. SSH in; `systemctl status uac-gadget` and `cat /sys/class/udc/*/state` should show `configured`.
4. Confirm the phone's USB audio device and helper app; unlock the phone if it rebooted.
5. Place a test call, confirm both audio directions, and hang up.
6. Check `vcgencmd get_throttled`, `vcgencmd measure_temp`, and `dmesg`; check battery and
   connector heat; review logs and record the outage and UPS runtime.

## 7. Thermal

- **Pi.** The SoC soft-throttles around 80 °C and hard-throttles around 85 °C; verify on your
  board. A Zero 2 W with Wi-Fi and audio should run well below that at 30 °C ambient with
  airflow. Use `vcgencmd measure_temp` and `vcgencmd get_throttled`; a heatsink helps, but airflow matters more.
- **Phone.** 25 W charging plus a call plus 30 °C ambient produces heat; One UI may reduce the
  rate when the battery is warm. Remove the case, stand the phone upright, keep it away from the
  charger and router, and aim below 40 °C; charging is typically limited near 45 °C. Verify on your device.
- **Hub and charger.** Warm is normal; hot, a burning smell, or discolored plastic is a
  failure. Replace the unit and do not cover it.
- **No closed boxes.** Use an open frame or vented enclosure with at least 5 cm clearance, and
  keep the phone outside it. If you add a fan, power it from a separate 5 V supply, not the Pi's
  USB port, to avoid a brownout. Keep everything out of direct sun and below about 35 °C.

## 8. Assumptions and limits

- Mini UPS specifications vary. Verify Wh, output voltage, continuous rating, and efficiency;
  the 80% efficiency and 80% depth of discharge are planning assumptions.
- Phone charging is device- and firmware-specific. Verify on your device after a One UI update.
- Meter readings include conversion losses and meter error; treat them as ±10–20%.
- The Pi current figures come from the design brief and are typical, not guaranteed for every
  workload.
- A UPS is not lightning or flood protection. The phone battery is the only phone backup; a
  degraded or swollen battery reduces outage resilience.

## 9. Verification steps

1. Measure the charger input at baseline, during an active call, and at the 80–85% cap; confirm
   the Pi draws 150–300 mA typical and about 400 mA peak at 5 V.
2. Run a 10-minute call; confirm the UAC device stays enumerated, `arecord -l` lists the
   gadget, and there are no ALSA xruns or USB disconnects.
3. Pull mains and measure UPS runtime with the router and hub connected. Confirm the phone stays
   up and the Pi either stays up for the measured runtime or fails cleanly.
4. Rehearse the recovery checklist; record the time to a successful test call.
5. After 24 hours, confirm `vcgencmd get_throttled` is 0x0, `dmesg` has no undervoltage lines,
   temperature is stable, and the phone is at the cap, not hot, with no swelling.
6. Record all measurements and assumptions; update the power budget when values differ.
