# Rig YAML Schema

An imaging **rig** describes the physical equipment mounted for a session — mount, telescope(s),
camera(s), focuser, filter wheel, rotator, and anything else — that INDI itself has no protocol
representation for. See [Design.md § Imaging rig metadata](Design.md#imaging-rig-metadata) for
the background and rationale; this document is the field-by-field reference for the YAML format.

Each rig is one YAML file under the rigs directory (`$INDI_MCP_RIGS_DIR`, default `./rigs`),
named freely — the file's `id` field, not its filename, is what scripts and MCP tools use to
reference it. Files are loaded at server startup with `yaml.safe_load` and validated against
the schema below; a file that fails to parse or validate is logged and skipped rather than
aborting the whole load, and unknown fields are rejected.

A rig is a **flat list of components**, not a nested structure of imaging/guiding trains,
optical tube assemblies, or mounts — see
[Design.md](Design.md#imaging-rig-metadata) for why that's deferred. Each component has a
`role` (free-form, not a fixed enum) plus whichever of the fields below are meaningful for it.

This document still talks about the **imaging train** (`telescope`, `focuser`, `filterWheel`,
`rotator`, `camera`) and **guiding train** (`guideTelescope`, `guideCamera`) as a way to group
and discuss related roles — that's just descriptive language, not YAML structure. There is no
`imagingTrain`/`guidingTrain` key; every component, regardless of which train it conceptually
belongs to, is just another entry in the one flat `components` list.

## Example

```yaml
id: newtonian-8in
name: 8" Newtonian imaging rig
components:
  - role: mount
    device: "Telescope Simulator"
  - role: telescope
    apertureMm: 203
    focalLengthMm: 1000
  - role: focuser
    device: "Focuser Simulator"
    minPosition: 0
    maxPosition: 50000
  - role: filterWheel
    device: "Filter Wheel Simulator"
    slots:
      1: Luminance
      2: Red
      3: Green
      4: Blue
      5: Ha
      6: OIII
      7: SII
  - role: rotator
    device: "Rotator Simulator"
  - role: camera
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
  - role: guideTelescope
    apertureMm: 60
    focalLengthMm: 240
  - role: guideCamera
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
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

## Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Stable identifier for this rig. Used by scripts and MCP tools (`get_rig`, and eventually rig-aware script parameters) to reference it. Must be unique across all rig files; if two files declare the same `id`, the one loaded first (files are read in sorted filename order) wins and the other is skipped. |
| `name` | string | yes | Human-readable display name. |
| `components` | list of objects | yes (may be empty) | The rig's equipment. See below. |

## Component fields

Every component has a `role`; the rest of its fields depend on what that role needs. The
schema doesn't enforce which fields go with which role (e.g. it won't reject a `telescope` that
also has a `device`) — this is deliberately loose, matching the "flat list" simplicity above.
The tables below group roles by imaging train / guiding train / other purely to make this
reference easier to read — again, not a grouping that exists in the YAML itself.

| Field | Type | Description |
|---|---|---|
| `role` | string | What this component is. One of the known roles — `"mount"`, `"telescope"`, `"guideTelescope"`, `"camera"`, `"guideCamera"`, `"focuser"`, `"filterWheel"`, `"rotator"`, `"powerHub"`, `"observatoryControl"`, `"flatScreen"`, `"dewHeater"` — or any other string, for a component type this schema's authors haven't thought of yet. Not required to be unique within a rig: a rig commonly has more than one component sharing a role (e.g. several independently-controlled dew heater channels). |

#### Mount

| Field | Type | Description |
|---|---|---|
| `device` | string | The INDI device name for the mount driver. |

#### Imaging train (`telescope`, `focuser`, `filterWheel`, `rotator`, `camera`)

| Field | Type | Applies to | Description |
|---|---|---|---|
| `device` | string | `focuser`, `filterWheel`, `rotator`, `camera` | The INDI device name for that component's driver. Omitted for `telescope` (no driver of its own). |
| `apertureMm` | number | `telescope` | Aperture, in millimeters. |
| `focalLengthMm` | number | `telescope` | Focal length, in millimeters. |
| `minPosition` / `maxPosition` | integer | `focuser` | The focuser's travel range, in its native position units. |
| `slots` | map of integer → string | `filterWheel` | Filter name per slot position (1-indexed, matching the filter wheel's own numbering). Omit or leave incomplete for slots that aren't in use or aren't yet decided. |
| `cooled` | boolean | `camera` | Whether the camera has active sensor cooling. |
| `pixelsX` / `pixelsY` | integer | `camera` | Sensor resolution. |
| `pixelSizeMicron` | number | `camera` | Pixel pitch, in microns. |
| `bitDepth` | integer | `camera` | ADC bit depth (e.g. `16`). |

#### Guiding train (`guideTelescope`, `guideCamera`)

Same fields as their imaging-train counterparts (`telescope`/`camera` above) — `guideTelescope`
takes `apertureMm`/`focalLengthMm`, `guideCamera` takes `device`/`cooled`/`pixelsX`/`pixelsY`/
`pixelSizeMicron`/`bitDepth`. A guide setup is usually just these two roles, with no
`focuser`/`filterWheel`/`rotator` counterpart, but nothing stops a rig from declaring one if it
has a motorized guide focuser or similar.

#### Other equipment (`powerHub`, `observatoryControl`, `flatScreen`, `dewHeater`, ...)

| Field | Type | Description |
|---|---|---|
| `device` | string | The INDI device name for that component's driver. |

## Design notes

* **The YAML definition is authoritative; live INDI properties are advisory.** Where a field
  overlaps with something INDI reports at runtime (a camera's pixel size/count/bit depth via
  its `CCD_INFO`-family properties), the server can cross-check the connected device against
  the configured rig and flag a mismatch — but it never overrides the declared config. INDI
  has no way to confirm `apertureMm`/`focalLengthMm`, or which camera is the imaging vs.
  guiding one, so those parts of the rig can only come from the YAML.
* **No silent auto-selection.** The server never guesses which rig is physically mounted.
  `suggest_rig` proposes a likely match by cross-referencing connected device names against
  configured rigs, but the operator (or client) explicitly selects the active rig; scripts and
  tool calls reference a rig by `id`.
* **Unknown top-level fields are rejected**, not ignored, so a typo'd or outdated field name
  fails loudly (as a skipped file, logged) instead of silently having no effect. Component
  entries are similarly strict about field *names* (no typo'd `pixelX` slipping through), even
  though which fields are meaningful for a given `role` isn't enforced.
