# Observatory Location YAML Schema

An **observatory location** describes where the equipment is physically set up — latitude,
longitude, and elevation — plus a `name`/`id`. INDI has no protocol representation for this at
all (unlike a camera's pixel geometry, which a device can at least partially report over
`CCD_INFO`): it is pure operator knowledge, needed for astronomical calculations that depend on
where on Earth the observer is, such as computing an object's altitude/visibility over a timespan
(INDIMCP-29) and, later, meridian-flip and multi-target scheduling logic (INDIMCP-32/33).

## Where this lives

Observatory locations are **YAML documents, not SQLite rows, and not part of the rig store** —
this mirrors the reasoning already laid out for rigs (see
[Design.md § Imaging rig metadata](Design.md#imaging-rig-metadata)):

* **Not SQLite** — like rigs, this is low-volume, human-curated configuration that changes
  rarely, not write-heavy operational data.
* **Not part of the rig store** — a rig (mount, camera, focuser, ...) and a location are
  orthogonal: the same rig can be used from more than one site (a backyard setup that's
  occasionally taken to a dark-sky site), and the same site can host different rigs over time.
  Folding location fields into `Rig`/`Component` would force every rig definition to repeat (or
  omit) the same site data, and would conflate two independent axes of "what's the setup" and
  "where is it." A separate store keeps them composable: a script or tool call names a rig and a
  location independently.

Each location is one YAML file under an observatories directory (`$INDI_MCP_OBSERVATORIES_DIR`,
default `./observatories`), named freely — the file's `id` field, not its filename, is what
scripts and MCP tools reference. Files are loaded at server startup with `yaml.safe_load` and
validated against the schema below, following the same loading discipline as rigs: a file that
fails to parse or validate is logged and skipped rather than aborting the whole load (since this
config may be hand-edited or uploaded by a client), and unknown fields are rejected rather than
ignored. A duplicate `id` across files keeps whichever file was loaded first (files are loaded in
sorted filename order) — again matching `load_rigs`.

**Multiple locations, explicitly selected — no auto-detection.** A user may run the same server
from more than one site (home observatory vs. a travel/remote setup), so the store holds any
number of location definitions, not just one. There is nothing for the server to auto-detect a
location *from* (no INDI device reports GPS coordinates), so this goes further than a rig's
"no silent auto-selection" rule: there is no `suggest_location` at all. A script or tool call that
needs a location takes an explicit `locationId` parameter (the same shape as `run_script`'s
`rigId`, see [ScriptSchema.md § Resolving roles to devices](ScriptSchema.md#resolving-roles-to-devices)),
resolved against this store the same way a rig id is resolved against the rig store.

## Example

```yaml
id: home-backyard
name: Home backyard observatory
latitudeDeg: 52.3676
longitudeDeg: 4.9041
elevationMeters: 4
```

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Stable identifier for this location. Used by scripts and MCP tools (a `locationId` parameter, analogous to `run_script`'s `rigId`) to reference it. Must be unique across all location files; if two files declare the same `id`, the one loaded first (files read in sorted filename order) wins and the other is skipped. |
| `name` | string | yes | Human-readable display name (e.g. `"Home backyard observatory"`). |
| `latitudeDeg` | number | yes | Geodetic latitude, in decimal degrees, WGS84. Positive north, negative south. Must be in `[-90, 90]`. |
| `longitudeDeg` | number | yes | Geodetic longitude, in decimal degrees, WGS84. Positive east, negative west (astropy's `EarthLocation.from_geodetic` convention). Must be in `[-180, 180]`. |
| `elevationMeters` | number | no, default `0` | Height above the WGS84 ellipsoid, in meters. May be negative (a site below the ellipsoid is valid). |

`latitudeDeg`/`longitudeDeg`/`elevationMeters` map directly onto astropy's
`EarthLocation.from_geodetic(lon, lat, height)`, which is what INDIMCP-29's horizon check (and
any later sun/moon/twilight or meridian-flip calculation) constructs the observer frame from —
this schema exists to hold exactly the three numbers that call needs, plus the `name`/`id` used to
select which location a script run applies to. No timezone field is included: astropy's
time/coordinate calculations run in UTC regardless of the site's local timezone, and adding a
`timezone` field now for display purposes with no current consumer would be speculative — it can
be added later if a concrete use (e.g. showing local sunset time in a client) needs it.

## Design notes

* **The YAML definition is authoritative.** There is no live signal to cross-check it against
  (unlike a rig's `device` fields, which the messaging layer can at least confirm are connected)
  — a location is trusted operator input, full stop.
* **Unknown top-level fields are rejected**, not ignored, matching rigs and scripts — a typo'd
  field name fails loudly (as a skipped file, logged) instead of silently having no effect.
* **Latitude/longitude bounds are validated**, not just typed as numbers, since a value outside
  `[-90, 90]`/`[-180, 180]` is unambiguously a mistake (e.g. degrees/minutes/seconds pasted in
  without conversion) rather than a legitimate location — failing to load loudly here is cheaper
  than a silently wrong horizon calculation later.
