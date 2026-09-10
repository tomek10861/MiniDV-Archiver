# FireWire & camera notes

## ⚠ Cabling and the 6-pin ↔ 4-pin adapter

The 6-pin (alpha) FireWire connector carries **bus power, ~8–30 V**. The 4-pin
i.LINK connector on camcorders carries **only data**. A cheap or miswired 6→4
adapter cable — or hot-plugging one — can route that power into a connector that
was never meant to receive it and physically burn out the port on the camera or
the PC. It has happened here to an old camcorder.

Safe procedure:

1. **PC powered off** — connect the **6-pin** end to the PC card.
2. **Camera powered off** — connect the **4-pin** end to the camera.
3. Power the PC on, then the camera (PLAYER / VCR mode).
4. **Never** hot-plug either end while anything is powered. To change cables, power
   both down first.

Prefer a straight **4-pin ↔ 4-pin** cable if the PC card exposes a 4-pin port, or a
reputable powered adapter. Avoid unbranded bargain adapters.

## The stack

MiniDV Archiver uses the mainline `firewire_ohci` / `firewire_core` kernel stack and
`/dev/fw*`. It never uses the legacy `raw1394` stack. The camera is found by its
GUID (`MINIDV_CAMERA_GUID`) or, if that is empty, as the first firewire device that
exposes an **AV/C tape recorder/player** unit (specifier `0x00a02d`).

`dvgrab -noavc` receives the isochronous DV channel. Transport control (PLAY / STOP /
REW) is a separate concern:

- `MINIDV_ALLOW_FCP=1` (default) — the app sends AV/C Function Control Protocol
  frames via `firewire-request` from a closed allow-list:
  PLAY `00 20 c3 75`, STOP `00 20 c4 60`, PAUSE `00 20 c3 7d`, REW `00 20 c4 65`,
  FF `00 20 c4 75`. Status is read with `01 20 d0 7f`, timecode with
  `01 20 51 71 ff ff ff ff`. **No RECORD opcode exists anywhere in the code.**
- `MINIDV_ALLOW_FCP=0` — manual mode: `/api/tape/*` returns 409, `/api/status` never
  touches AV/C, and you press PLAY/STOP on the camera yourself. The capture still
  detects start (first DV bytes) and end (blank-tail / no-signal timeouts).

`dvcont` is not used — its auto-detection is incompatible with several cameras.

## When a camera misbehaves

Symptoms: the camera node appears and disappears on the bus, `dmesg` shows
`giving up on node ... reading config rom failed`, `PHY ID mismatch in self ID`,
`topology build failed`, or the bus resets right after an FCP transaction.

1. **Switch to manual mode**: `MINIDV_ALLOW_FCP=0`. Many old i.LINK PHYs survive
   passive isochronous streaming but reset the bus on FCP.
2. **Power-cycle the camera** — unplug its AC adapter for ~30–60 s, then back on,
   PLAYER mode. A wedged PHY cannot be recovered in software.
3. **Disable PCI runtime power management** for the FireWire controller and any
   PCIe-to-PCI bridge in front of it — autosuspend into D3 glitches the 1394 bus.
   `systemd/99-minidv-firewire.rules` has an example (`ATTR{power/control}="on"`)
   keyed to VIA VT6306 (`1106:3044`) + ASMedia ASM1083 (`1b21:1080`); adjust the
   IDs for your controller (`lspci -nn | grep -i 1394`).
4. Try a different cable / a Texas Instruments OHCI card without a PCI bridge.
5. Do **not** run `testlibraw` while capturing — it rewrites the local config ROM
   and resets `/dev/fw*` numbering.

The capture pipeline itself does not care about `/dev/fwN` numbering — the camera is
always re-resolved by GUID / AV/C unit before each transaction.

---

## Case study: Sony DCR-PC2E

A worked example of every failure mode above, and why the defaults for a *known*
flaky camera differ from the shipped defaults.

- Its AV/C stack is partly broken: a single FCP frame usually returns `timeout` /
  `no ack`, after which the PHY drops off the bus and a reset storm starts
  (`PHY ID mismatch in self ID: 0 != 1` → `topology build failed`). With **zero
  FCP** it streamed for 25+ minutes straight and captured a full 60-minute tape
  with one damaged frame.
- With PCI runtime PM at the default `auto`, reloading `firewire_ohci` would not
  even create the local node. Forcing `power/control=on` on the VIA controller and
  the ASMedia bridge fixed enumeration.
- Recovery from "disappeared from bus" is **only** a physical power-cycle.
- Its real-time clock reads year 2067 — recording dates from VAUX are wrong, so
  proxy `creation_time` is only stamped when the year looks sane (1990–2025).

Recommended settings for this camera: `MINIDV_ALLOW_FCP=0`, install the udev PM
rule, keep `MINIDV_BLANK_TAIL_TIMEOUT` at its default so the ~5-minute blank tail at
end of tape does not stall the pipeline.
