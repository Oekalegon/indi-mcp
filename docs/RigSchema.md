# Rig YAML Schema

An imaging **rig** describes the physical equipment mounted for a session — an imaging train
(telescope optics, focuser, filter wheel, rotator, camera), an optional separate guiding train,
and a mount — that INDI itself has no protocol representation for. See
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
imagingTrain:
  telescope:
    apertureMm: 203
    focalLengthMm: 1000
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
  rotator:
    device: "Rotator Simulator"
  camera:
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
guidingTrain:
  telescope:
    apertureMm: 60
    focalLengthMm: 240
  camera:
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
devices:
  - role: powerHub
    device: "Pegasus PPBA"
  - role: observatoryControl
    device: "Dome Simulator"
  - role: flatScreen
    device: "Flat Panel Simulator"
  - role: dewHeater
    device: "Pegasus PPBA:Dew A"
  - role: dewHeater
    device: "Pegasus PPBA:Dew B"
```

An off-axis-guided rig looks like this instead of having a `guidingTrain` (see
[Off-axis guiding](#off-axis-guiding) below):

```yaml
imagingTrain:
  telescope:
    apertureMm: 200
    focalLengthMm: 800
  camera:
    device: "ZWO CCD ASI2600MM Pro"
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
  offAxisGuider:
    camera:
      device: "ZWO CCD ASI120MM Mini"
      pixelsX: 1280
      pixelsY: 960
      pixelSizeMicron: 3.75
      bitDepth: 12
```

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Stable identifier for this rig. Used by scripts and MCP tools (`get_rig`, and eventually rig-aware script parameters) to reference it. Must be unique across all rig files; if two files declare the same `id`, the one loaded first (files are read in sorted filename order) wins and the other is skipped. |
| `name` | string | yes | Human-readable display name. |
| `mount` | object | yes | The telescope mount. Sits outside both trains — it's the shared physical platform, not part of either optical path. |
| `mount.device` | string | yes | The INDI device name for the mount driver (e.g. `"Telescope Simulator"`). |
| `imagingTrain` | object | yes | The main imaging optical train: telescope optics through to the imaging camera. |
| `guidingTrain` | object | no | A separate guiding optical train, if the rig uses one. Same shape as `imagingTrain`, minus `offAxisGuider` (see [Off-axis guiding](#off-axis-guiding)). Omit if guiding shares the imaging train via an off-axis guider, or if the rig isn't guided. |

### Optical train fields (`imagingTrain` / `guidingTrain`)

Both trains share the same shape: a `telescope` and `camera` are required; `focuser`,
`filterWheel`, and `rotator` are optional on **either** train, since in principle any of them
could apply to a guiding setup even though in practice a guide scope is almost always just a
telescope + camera with none of the three.

| Field | Type | Required | Description |
|---|---|---|---|
| `telescope` | object | yes | Optical data for this train. Has no `device` of its own — it isn't a driver, it's data associated with what's attached to the mount (or, for an off-axis guider, the imaging train's own telescope). |
| `telescope.apertureMm` | number | yes | Aperture, in millimeters. |
| `telescope.focalLengthMm` | number | yes | Focal length, in millimeters. |
| `camera` | object | yes | The camera for this train. |
| `camera.device` | string | yes | The INDI device name for the camera driver. |
| `camera.cooled` | boolean | no (default: `false`) | Whether the camera has active sensor cooling. |
| `camera.pixelsX` / `camera.pixelsY` | integer | yes | Sensor resolution. |
| `camera.pixelSizeMicron` | number | yes | Pixel pitch, in microns. |
| `camera.bitDepth` | integer | yes | ADC bit depth (e.g. `16`). |
| `focuser` | object | no | The focuser for this train, if it has one. |
| `focuser.device` | string | yes (if `focuser` is present) | The INDI device name for the focuser driver. |
| `focuser.minPosition` / `focuser.maxPosition` | integer | yes (if `focuser` is present) | The focuser's travel range, in its native position units. |
| `filterWheel` | object | no | The filter wheel for this train, if it has one. |
| `filterWheel.device` | string | yes (if `filterWheel` is present) | The INDI device name for the filter wheel driver. |
| `filterWheel.slots` | map of integer → string | no (default: empty) | Filter name per slot position (1-indexed, matching the filter wheel's own numbering). Omit or leave incomplete for slots that aren't in use or aren't yet decided. |
| `rotator` | object | no | A camera-field rotator, if this train has one. |
| `rotator.device` | string | yes (if `rotator` is present) | The INDI device name for the rotator driver. |

### Off-axis guiding

`imagingTrain.offAxisGuider` is an alternative to `guidingTrain`, not an addition to it — the
schema rejects a rig that declares both. An off-axis guider (OAG) picks off guide-camera light
from the imaging train's own optical path via a prism, so it needs no telescope optics of its
own, just a camera.

| Field | Type | Required | Description |
|---|---|---|---|
| `imagingTrain.offAxisGuider` | object | no | An off-axis guider on the imaging train. Mutually exclusive with `guidingTrain`. |
| `imagingTrain.offAxisGuider.camera` | object | yes (if `offAxisGuider` is present) | The guide camera. Same shape as any other `camera` field above. |

### Other devices

| Field | Type | Required | Description |
|---|---|---|---|
| `devices` | list of objects | no (default: empty) | Any other equipment that doesn't need config beyond a device name and a role — power hubs, observatory/dome control, flat-field panels, dew heaters, and anything else this schema doesn't have a dedicated field for. |
| `devices[].role` | string | yes | A free-form label for what this device is (e.g. `"powerHub"`, `"observatoryControl"`, `"flatScreen"`, `"dewHeater"`). Not a fixed enum — a rig can use a role this schema's authors never anticipated, so a new device type never requires a schema change. `role` values aren't required to be unique: a rig commonly has more than one device sharing a role (e.g. several independently-controlled dew heater channels). |
| `devices[].device` | string | yes | The INDI device name for that device's driver. |

A component only needs its own typed field, instead of an entry in `devices`, once it needs
config beyond a device name — the way `focuser` needs a position range and `filterWheel` needs
slot names. That's also why `rotator` is a typed field on the optical trains rather than living
in `devices`: it's a core part of the imaging train, the same way `focuser`/`filterWheel`/`camera`
are. Everything else — including device types not yet anticipated — belongs in `devices`.

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
