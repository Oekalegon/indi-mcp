# Sensor analysis: theory and use (INDIMCP-104)

What the sensor calibration frames captured by this server are *for*: the measurements a
photon-transfer-curve (PTC) analysis extracts from them, the math behind each one, and how the
resulting sensor profile is used to choose camera settings for real imaging. This document is
theory and usage; [SensorCalibration.md](SensorCalibration.md) is the companion design document
for the capture scripts and sweep tools themselves (INDIMCP-81/101), and deliberately says
nothing about *why* the analysis works — that's this document's job.

## What a sensor profile is

A camera sensor at one gain/offset setting is characterized by a handful of numbers:

| Quantity | Unit | What it tells you |
|---|---|---|
| **Read noise** | e⁻ RMS | The noise floor added by reading out a pixel, independent of exposure. Determines how faint a signal can be before it drowns in readout noise. |
| **Gain** (conversion factor) | e⁻/ADU | How many photo-electrons one output count (ADU) represents. Converts everything the camera reports from arbitrary counts into physical electrons. |
| **Dark current** | e⁻/s (per pixel) | Thermally generated signal that accumulates with exposure time even in total darkness. Depends strongly on sensor temperature. |
| **Full-well capacity** | e⁻ | How many electrons a pixel can hold before it saturates. Sets the bright end of the usable range. |
| **Dynamic range** | stops, or dB | Full well ÷ read noise — the ratio between the brightest and faintest usable signal in a single exposure. |
| **Bias level (offset)** | ADU | The constant pedestal added to every readout so noise never clips below zero. |

None of these are single numbers for "the sensor" — read noise, gain, and full well all change
with the camera's **gain setting** (and, weakly, its offset setting). That's why the capture
side sweeps gain/offset combinations: the end product is a profile *per setting*, from which the
best setting for a given kind of imaging can be chosen (see "Using the profile" below).

## The sensor's noise model

Every raw pixel value is the sum of independent random contributions. Because the contributions
are independent, their **variances add** — this single fact is what the whole analysis leans on:

$$\sigma^2_{\text{total}} = \sigma^2_{\text{read}} + \sigma^2_{\text{dark}} + \sigma^2_{\text{shot}} \qquad \text{(all in the same units, i.e. ADU}^2\text{)}$$

* **Read noise** $\sigma_{\text{read}}$ — fixed per readout, independent of exposure time and signal level.
* **Dark-current shot noise** $\sigma_{\text{dark}}$ — dark current is a Poisson process, so its noise
  grows with exposure time: $\sigma^2_{\text{dark}} = D \cdot t$ (in electrons, for dark current $D$
  and exposure $t$).
* **Photon shot noise** $\sigma_{\text{shot}}$ — photon arrival is also Poisson, so the variance of the
  detected signal *equals its mean* in electrons: $\sigma^2_{\text{shot}} = S$ (for mean signal $S$
  in e⁻).

The Poisson property of photon shot noise is the key to measuring gain. In **electrons**,
variance = mean exactly. In **ADU**, with gain $g$ (e⁻/ADU), signal converts as
$S_{\text{ADU}} = S_{e^-}/g$ and variance as $\sigma^2_{\text{ADU}} = \sigma^2_{e^-}/g^2$, so:

$$\sigma^2_{\text{shot,ADU}} = \frac{S_{\text{ADU}}}{g}$$

Plot shot-noise variance against mean signal, both in ADU, and the slope is 1/g. That plot is
the **photon transfer curve**, and everything the frame types below exist for is isolating the
terms of the noise model well enough to draw it.

## What each frame type isolates

* **Bias** — zero-length (or shortest possible) exposure, no light. Contains *only* the bias
  pedestal and read noise: t ≈ 0 kills the dark term, no light kills the shot term.
* **Dark** — real exposure, no light. Pedestal + read noise + dark current and its shot noise.
  Compared against bias (or fit across several exposure lengths), it isolates dark current.
* **Flat** — real exposure, illuminated. All three noise terms plus real photon signal. The only
  frame type containing photon shot noise, hence the only route to measuring gain and full well.
* **Flat-dark** — a dark at *exactly the flat's exposure length*. Everything the flat contains
  except the light. Subtracting it from a flat leaves pure photonic signal, so the shot-noise
  term can be isolated without assuming anything about dark current.

## The pair-differencing trick

A naïve variance measurement across a flat frame is corrupted by **fixed pattern**: vignetting,
dust shadows, and pixel-to-pixel sensitivity differences (PRNU) imprinted by the optical train
and the sensor itself. These are *spatial* structure, not noise — identical in every frame —
but they inflate a naïvely computed spatial variance enormously.

The standard cure is to difference **two frames captured back-to-back under identical
conditions**:

$$\text{diff} = \text{frame}_A - \text{frame}_B$$

Everything deterministic — pedestal, dark-current mean, vignetting, dust, PRNU — is identical
in both frames and cancels exactly. What survives is only the *random* part, present
independently in each frame, so the difference has twice the single-frame variance:

$$\sigma^2_{\text{frame}} = \frac{\text{Var}(\text{diff})}{2} \qquad \text{equivalently} \qquad \sigma_{\text{frame}} = \frac{\text{StdDev}(\text{diff})}{\sqrt{2}}$$

This is why sensor analysis works with **pairs** (or sequences treated pairwise), and why the
flats used here do *not* need to be optically uniform: the imaging train's spatial imprint
cancels in the difference, as long as nothing (illumination level, exposure, gain/offset,
temperature) changes between the two frames of a pair. The same trick applied to two bias frames
measures read noise directly.

## The measurements, step by step

All formulas below operate per gain/offset setting, on frames from that setting only. An
overbar (e.g. $\bar{x}$) denotes a mean over pixels; Var/StdDev are computed over pixels of a
difference image, ideally over a central region avoiding sensor edges.

### 1. Read noise (from bias pairs)

$$\sigma_{\text{read,ADU}} = \frac{\text{StdDev}(\text{bias}_A - \text{bias}_B)}{\sqrt{2}}$$

$$\sigma_{\text{read},e^-} = \sigma_{\text{read,ADU}} \times g \qquad \text{(once } g \text{ is known from step 3)}$$

With more than two bias frames, average the estimate over successive pairs. The bias frames'
mean also gives the **bias level** (the offset pedestal in ADU) — worth recording per offset
setting to verify the offset is high enough that read noise never clips at zero (see "Choosing
an offset" below).

### 2. Dark current (from darks at one or more exposures)

Mean dark signal above bias, converted to a rate:

$$D_{\text{ADU/s}} = \frac{\bar{\text{dark}} - \bar{\text{bias}}}{t}$$

$$D_{e^-/\text{s}} = D_{\text{ADU/s}} \times g$$

A single exposure length gives a one-point estimate; darks at several exposure lengths allow a
linear fit of mean-signal-vs-time whose slope is D and whose intercept should recover the bias
level (a useful consistency check). Dark current roughly doubles every 5–7 °C, so the sensor
temperature at capture is an essential part of the result — a dark-current figure without its
temperature is meaningless.

### 3. Gain (from flat pairs — the PTC proper)

For each flat exposure level, using a back-to-back pair of flats and the matching flat-dark:

$$S_{\text{ADU}} = \text{mean}(\text{flat}_A - \text{flatdark}) \qquad \text{(mean photonic signal)}$$

$$\sigma^2_{\text{ADU}} = \frac{\text{Var}(\text{flat}_A - \text{flat}_B)}{2} - \sigma^2_{\text{read,ADU}} \qquad \text{(shot-noise variance, read noise removed)}$$

(The flat-dark subtraction removes the dark and pedestal contribution from the *mean*; the
pair difference removes fixed pattern from the *variance*; subtracting the independently
measured read-noise variance leaves shot noise alone. Dark shot noise is negligible at flat
exposures and cancels to first order in the difference's statistics.)

Poisson statistics then give, at every exposure level:

$$\sigma^2_{\text{ADU}} = \frac{S_{\text{ADU}}}{g} \quad\Longrightarrow\quad g = \frac{S_{\text{ADU}}}{\sigma^2_{\text{ADU}}} \qquad (e^-\!/\text{ADU})$$

A single flat level yields a single gain estimate; **several exposure levels spanning from low
signal up toward saturation** yield a variance-vs-signal line whose slope is 1/g — a far more
robust fit, and the only way to *see* the two things a single point can't show: whether the
sensor is linear over its range, and where the curve breaks (step 4). This is why the capture
design sweeps flat exposure times, not just gain/offset
(see [SensorCalibration.md](SensorCalibration.md)).

### 4. Full well and dynamic range (from the top of the PTC)

As signal approaches saturation, variance stops growing with the Poisson slope and rolls over
(pixels clip, so their spread collapses). The signal level at which the measured PTC departs
from the fitted line marks the usable **full-well capacity**:

$$FW_{e^-} = S_{\text{ADU,rolloff}} \times g$$

$$DR = \frac{FW_{e^-}}{\sigma_{\text{read},e^-}} \qquad \text{(express as } 20\log_{10}(DR) \text{ dB, or } \log_2(DR) \text{ stops)}$$

### 5. Defect maps (a by-product, not a PTC quantity)

The same dark and bias frames also yield per-pixel maps: hot pixels (outliers in the darks),
unstable/warm pixels (high variance across the bias stack), dead or low-response pixels
(outliers in the flats). These are not part of the sensor profile proper but fall out of the
same data for free.

## Using the profile

The point of measuring all this per gain/offset setting is to make imaging decisions with
numbers instead of folklore:

* **Choosing a gain setting.** Raising the camera's gain setting typically lowers read noise
  (good for faint, narrowband, or short-exposure work) but shrinks full well and dynamic range
  (bad for bright targets and star colors). The measured read-noise and full-well curves versus
  gain setting show exactly where the trade sits for *this* sensor — including any step change
  (many CMOS sensors switch conversion mode at a specific gain setting, visible as a sudden
  read-noise drop worth sitting just above).
* **Unity gain** — the setting where $g = 1\ e^-\!/\text{ADU}$ — falls straight out of the gain curve, for
  operators who use it as a reference point.
* **Choosing an offset.** The offset must be high enough that the pedestal minus a few σ of read
  noise stays above zero (clipped noise biases every later calibration subtraction), and no
  higher than needed (wasted headroom). The measured bias level and read noise per offset
  setting answer this directly.
* **Sub-exposure length.** Knowing read noise in electrons lets you compute when sky-background
  shot noise swamps read noise — the classic criterion for "long enough" sub-exposures — rather
  than guessing.
* **Converting anything to electrons.** Once g is known, every ADU measurement the camera ever
  produces (sky background rates, star fluxes, calibration statistics) can be expressed in
  physical units and compared across settings, sessions, and cameras.
* **Monitoring sensor health.** Re-running the analysis periodically shows drift: growing dark
  current, spreading hot-pixel populations, changing read noise — early warnings of a cooling
  problem or an aging sensor.

## How a capture session maps to the analysis

Practically, gathering the inputs for one sensor profile looks like this (mechanics in
[SensorCalibration.md](SensorCalibration.md); repeated per gain/offset setting of interest):

1. **Unattended part — no light source, panel *not* staged:** capture the bias stack and the
   flat-dark frames (one flat-dark set per flat exposure length planned in step 2). Order
   matters: flat-darks must be captured *before* the flat panel is ever staged, since a
   panel that's on — or merely leaking light nearby — contaminates them. Optionally include
   longer darks at this point if a dark-current fit is wanted.
2. **Attended part — flat panel staged:** capture flat *pairs* at a ladder of exposure levels,
   from a low signal level up to past saturation (e.g. roughly doubling exposure each rung).
   Illumination and settings must not change within a pair.
3. **Analysis (offline, outside this server):** apply steps 1–4 above to produce the profile
   for that setting; repeat over settings; tabulate read noise, gain, full well, and dynamic
   range versus gain setting.

Sensor temperature should be held constant (cooled to a fixed set point) across the entire
session — dark current's strong temperature dependence otherwise contaminates every comparison
between frames.

The analysis itself is deliberately **not** implemented in this server: the server captures and
stores the frames (they're ordinary FITS files, retrievable via `list_frames` and each frame's
`downloadUrl` — see [Design.md § Retrieving frames](Design.md#retrieving-frames)); the
statistics above are a client-side/offline computation over those files.
