# Rig YAML Schema

An imaging **rig** describes the physical equipment mounted for a session — telescope
optics, focuser, filter wheel, imaging camera, and (optionally) a separate guiding train —
that INDI itself has no protocol representation for. See
[Design.md § Imaging rig metadata](Design.md#imaging-rig-metadata) for the background and
rationale; this document is the field-by-field reference for the YAML format.

Each rig is one YAML file under the rigs directory (`$INDI_MCP_RIGS_DIR`, default `./rigs`),
named freely — the file's `id` field, not its filename, is what scripts and MCP tools use to
reference it. Files are loaded at server startup with `yaml.safe_load` and validated against
the schema below; a file that fails to parse or validate is logged and skipped rather than
aborting the whole load, and unknown fields are rejected.

## Example

```yaml
id: newtonian-8in
name: 8" Newtonian imaging rig
mount:
  device: "Telescope Simulator"
telescope:
  imaging:
    apertureMm: 203
    focalLengthMm: 1000
  guiding:
    apertureMm: 60
    focalLengthMm: 240
focuser:
  device: "Focuser Simulator"
  minPosition: 0
  maxPosition: 50000
filterWheel:
  device: "Filter Wheel Simulator"
  slots:
    1: Luminance
    2: Red
    3: Green
    4: Blue
    5: Ha
    6: OIII
    7: SII
camera:
  imaging:
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
  guiding:
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
rotator:
  device: "Rotator Simulator"
powerHub:
  device: "Pegasus PPBA"
observatoryControl:
  device: "Dome Simulator"
flatScreen:
  device: "Flat Panel Simulator"
dewHeaters:
  - device: "Pegasus PPBA:Dew A"
  - device: "Pegasus PPBA:Dew B"
```

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Stable identifier for this rig. Used by scripts and MCP tools (`get_rig`, and eventually rig-aware script parameters) to reference it. Must be unique across all rig files; if two files declare the same `id`, the one loaded first (files are read in sorted filename order) wins and the other is skipped. |
| `name` | string | yes | Human-readable display name. |
| `mount` | object | yes | The telescope mount. |
| `mount.device` | string | yes | The INDI device name for the mount driver (e.g. `"Telescope Simulator"`). |
| `telescope` | object | yes | Optical data for the telescope. Has no `device` of its own — it isn't a driver, it's data associated with what's attached to the mount. |
| `telescope.imaging` | object | yes | Optics of the main imaging telescope. |
| `telescope.imaging.apertureMm` | number | yes | Aperture, in millimeters. |
| `telescope.imaging.focalLengthMm` | number | yes | Focal length, in millimeters. |
| `telescope.guiding` | object | no | Optics of a separate guide scope, if one is used. Same shape as `telescope.imaging`. Omit if guiding shares the imaging optical train, or if the rig isn't guided. |
| `focuser` | object | yes | The focuser. |
| `focuser.device` | string | yes | The INDI device name for the focuser driver. |
| `focuser.minPosition` / `focuser.maxPosition` | integer | yes | The focuser's travel range, in its native position units. |
| `filterWheel` | object | yes | The filter wheel. |
| `filterWheel.device` | string | yes | The INDI device name for the filter wheel driver. |
| `filterWheel.slots` | map of integer → string | no (default: empty) | Filter name per slot position (1-indexed, matching the filter wheel's own numbering). Omit or leave incomplete for slots that aren't in use or aren't yet decided. |
| `camera` | object | yes | The camera(s) used for imaging and (optionally) guiding. |
| `camera.imaging` | object | yes | The main imaging camera. |
| `camera.guiding` | object | no | The guide camera, if a separate one is used. Same shape as `camera.imaging`. Omit if there is no separate guide camera. |
| `camera.imaging.device` / `camera.guiding.device` | string | yes | The INDI device name for the camera driver. |
| `camera.imaging.cooled` / `camera.guiding.cooled` | boolean | no (default: `false`) | Whether the camera has active sensor cooling. |
| `camera.imaging.pixelsX` / `pixelsY` | integer | yes | Sensor resolution. |
| `camera.imaging.pixelSizeMicron` | number | yes | Pixel pitch, in microns. |
| `camera.imaging.bitDepth` | integer | yes | ADC bit depth (e.g. `16`). |
| `rotator` | object | no | A camera-field rotator, if one is used. |
| `powerHub` | object | no | A powered USB/power-distribution hub (e.g. Pegasus PPBA), if one is used. |
| `observatoryControl` | object | no | A dome or roll-off-roof controller, if the rig is housed in a controllable observatory. |
| `flatScreen` | object | no | A flat-field panel, if one is used for calibration frames. |
| `rotator.device` / `powerHub.device` / `observatoryControl.device` / `flatScreen.device` | string | yes (if the section is present) | The INDI device name for that component's driver. |
| `dewHeaters` | list of objects | no (default: empty) | Dew heater channels/straps, if any are used. A list rather than a single device, since rigs commonly have more than one independently-controlled channel. |
| `dewHeaters[].device` | string | yes | The INDI device name for that dew heater channel. |

(`camera.guiding` fields mirror `camera.imaging`'s, shown once above.)

`rotator`, `powerHub`, `observatoryControl`, `flatScreen`, and each entry of `dewHeaters` are, for
now, just a `device` name with no further config — unlike e.g. `focuser`'s position range. Extra
fields can be added later if a concrete use case needs them (e.g. a rotator's position range).

## Design notes

* **The YAML definition is authoritative; live INDI properties are advisory.** Where a field
  overlaps with something INDI reports at runtime (a camera's pixel size/count/bit depth via
  its `CCD_INFO`-family properties), the server can cross-check the connected device against
  the configured rig and flag a mismatch — but it never overrides the declared config. INDI
  has no way to confirm `apertureMm`/`focalLengthMm`, or which camera is the imaging vs.
  guiding train, so those parts of the rig can only come from the YAML.
* **No silent auto-selection.** The server never guesses which rig is physically mounted.
  `suggest_rig` proposes a likely match by cross-referencing connected device names against
  configured rigs, but the operator (or client) explicitly selects the active rig; scripts and
  tool calls reference a rig by `id`.
* **Unknown fields are rejected**, not ignored, so a typo'd or outdated field name fails loudly
  (as a skipped file, logged) instead of silently having no effect.
