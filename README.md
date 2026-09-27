# video-switcher

A lightweight live video switcher: cut/fade between camera and capture-card
sources on a two-stage program/preview bus like a hardware switcher, overlay
lower-thirds pulled in over NDI (e.g. from ProPresenter/EasyWorship), mux in
program audio, monitor program + preview, and push the result out over RTMP
or SRT. The media pipeline is GStreamer; the control surface is a small
Electron app.

## Architecture

```
Electron (electron/)                     Python engine (engine/)
┌─────────────────────────┐   WS :8765   ┌───────────────────────────────────────────┐
│ index.html/renderer.js  │◀────────────▶│ per-source tee ─▶ sel_A ─▶ comp row A  \    │
│  - source buttons       │              │               └─▶ sel_B ─▶ comp row B  >comp│
│    (load into preview)  │              │  (take() crossfades/cuts between rows) /    │
│  - CUT / AUTO(fade)     │   MJPEG x3   │  NDI overlay ───────────▶ comp (top layer)  │
│  - overlay toggle       │◀────:8080────│                                │           │
│  - stream start/stop    │              │                                ├─▶ tee ─▶..│
│  <img> PGM / PVW        │              │  audio src ─▶ tee ─────────────┘   │       │
└─────────────────────────┘              │                       ┌────────────┴─┐     │
                                          │                  preview (jpeg)  RTMP/SRT   │
                                          └───────────────────────────────────────────┘
```

`electron/main.js` spawns the Python engine (`python3 -m switcher`) as a
child process and opens the control window. The renderer talks to the
engine directly:

- a WebSocket (`/ws`) for control commands and status/error events
- three MJPEG `<img>` feeds - the on-air program monitor
  (`/preview/program.mjpg`) and one for whichever row is currently the
  preview bus (`/preview/row_a.mjpg` / `/preview/row_b.mjpg`, the renderer
  switches between them based on `status.previewRow`)

No frame data is round-tripped through Electron's main process.

The engine (`engine/switcher/`) builds one GStreamer pipeline, modeled on a
standard two-bus (program/preview) hardware switcher:

- every **cut** source (capture card, test pattern, ...) feeds its own
  `tee`, which fans into **both** row selectors (`sel_A`/`sel_B`), so either
  row can be pointed at any source
- exactly one row is on air (opaque, `compositor` zorder 0) and the other is
  the hidden preview row (zorder 1, alpha 0) - clicking a source button
  calls `load_preview`, which only ever repoints the *hidden* row, so it
  never touches what's live
- `take()` (CUT or AUTO) animates the hidden row's alpha 0→1 over the
  program row - instantly for a cut, or stepped over a configurable
  duration for a fade - then swaps which row is "on air" vs "preview" so
  the operator can load the next source without disturbing the program
- the **overlay** source (NDI lower-thirds) is a third compositor layer,
  always on top, whose opacity is toggled on/off independently of whichever
  camera is live
- an optional **audio** source feeds its own `tee`, muxed straight into the
  stream branch - audio does not "follow" the video cuts, the sound desk's
  mix just plays through continuously
- the composited program feeds a `tee`: one branch always runs to the
  program JPEG preview, a second (video + audio) branch is created/torn
  down on demand when you start/stop an RTMP or SRT stream
- each row also taps its own small JPEG preview branch for the PVW monitor

## Requirements

You need GStreamer with its Python (PyGObject/`gi`) bindings installed
system-wide - `pip install PyGObject` does **not** pull in GStreamer, so it's
deliberately left out of `engine/requirements.txt`.

### Linux (Debian/Ubuntu)

```sh
sudo apt install python3-gi gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
```

Capture cards that show up as UVC devices work out of the box via
`v4l2src` - check `v4l2-ctl --list-devices` for the device path.

### macOS

```sh
brew install pygobject3 gtk4 gstreamer gst-plugins-base gst-plugins-good \
  gst-plugins-bad gst-plugins-ugly gst-libav
```

Capture is via `avfvideosrc`; run `gst-device-monitor-1.0 Video/Source` to
find `deviceIndex` values for `config.json`.

### Windows

Easiest: `pip install gstreamer-bundle` (official, on PyPI since GStreamer
1.28) into the same Python environment you run the engine with - it bundles
the native runtime, plugins (including the GPL-licensed ones this project
needs, like `x264enc` for streaming), *and* the Python/`gi` bindings in one
step, no separate installer. Verified working end-to-end (all of
`mfvideosrc`, `compositor`, `input-selector`, `x264enc`, `flvmux`/
`rtmpsink`, `mpegtsmux`/`srtsink`, `voaacenc` present and functional) against
GStreamer 1.28.7 this way.

Alternatively, install the official unified installer from
gstreamer.freedesktop.org (Inno Setup-based as of 1.28) - as of that
version its `/TYPE=` silent-install switches (`runtime`, `devel`, `debug`)
don't appear to cover the Python bindings specifically, so if you go this
route, verify `python -c "import gi; gi.require_version('Gst','1.0'); from
gi.repository import Gst"` actually works afterward and adjust your
component selection if not (untested against the current installer version;
the pip route above is the one this project has actually confirmed).
Capture is via `mfvideosrc` either way.

### NDI (lower-thirds input)

NDI is not part of stock GStreamer. Install the NDI SDK/runtime for your
OS, then build/install a GStreamer NDI plugin that provides the `ndisrc`
element - e.g. the one in
[gst-plugins-rs](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs)
(`ndi` plugin) or a prebuilt `gst-plugin-ndi` package for your platform.
Once installed, `gst-inspect-1.0 ndisrc` should print the element's docs.
Set the overlay source's `ndiName` in `engine/config.json` to the NDI
source name as it appears in your NDI network (e.g. what your worship
software advertises).

### Node/Electron

```sh
npm install
```

## Configuring sources

The easiest way is the **Settings** tab in the Electron window: add/remove
cameras and the overlay, pick a device from **Detect cameras / mics** (a
best-effort scan using GStreamer's device monitor - it can't always tell
what device string a mic needs, in which case it says so rather than
guessing), and hit **Save config.json**. The Settings screen only edits the
file; the engine reads it once at startup, so changes need **Restart Engine
to apply** (the button next to Save) before they take effect.

To edit by hand instead, copy `engine/config.example.json` to
`engine/config.json` and edit it (or point `SWITCHER_CONFIG` at any path -
both the engine and the Settings screen respect it). Each entry in
`sources` is either:

- `"role": "cut"` - a switchable camera/capture/test source. Add as many as
  you like (the example ships two cameras, a color-bars test pattern, and a
  `black` test source - see **Transitions** below for what that's for)
- `"role": "overlay"` - the one NDI lower-thirds layer, composited on top

```json
{ "id": "cam1", "label": "Main Camera", "type": "capture", "role": "cut", "device": "/dev/video0" }
{ "id": "cam2", "label": "Stage Wide", "type": "capture", "role": "cut", "device": "/dev/video1" }
{ "id": "lower3rd", "label": "Lower Thirds (NDI)", "type": "ndi", "role": "overlay", "ndiName": "WORSHIP-PC (ProPresenter)" }
```

`type` is one of `capture`, `ndi`, `test`. Use `v4l2-ctl --list-devices` (or
your platform's equivalent) to find each capture card's device path -
multiple capture cards typically show up as `/dev/video0`, `/dev/video1`, etc.

### Audio

```json
"audio": { "enabled": true, "source": { "type": "alsa", "device": "hw:1,0" } }
```

`type` is `alsa` or `pulse` (point it at your sound desk's USB/audio
interface output) or `test` (a tone, for testing without hardware). This is
a single fixed audio feed muxed straight into the outgoing stream - it does
not play out of the machine running the switcher (you're in the room; you
don't want the desk mix coming out of a laptop speaker too), and it is not
included in the on-screen preview, only in what you stream out.

## Transitions

For a church service, two transition types cover almost everything:

- **CUT** - instant, no animation. Use this for most switches: it reads as
  clean and intentional, and it's what people expect during normal
  multi-camera coverage (speaker → wide shot → worship team, etc).
- **AUTO (fade/dissolve)** - a soft crossfade, default 500ms (adjustable in
  the UI, 150ms-2000ms). Use it for the moments you want to feel deliberate
  rather than reactive: start/end of service, moving into a moment of
  prayer or reflection, or bridging between a video/slide source and a
  camera. Keep it short (300-800ms) - a slow fade reads as "trying to be
  cinematic" and gets distracting fast in a live service.

I'd deliberately recommend **against** wipes, stingers, or other flashy
transitions for a church context - they draw attention to the switching
itself, which is the opposite of what you want during worship or preaching.
Cut and fade are what every serious church AV team actually uses.

"Fade to black" doesn't need special support: the example config includes a
`black` cut source, so loading it into preview and hitting AUTO gives you a
fade-to-black exactly like any other transition (handy for the very start/
end of a stream, or covering a dead moment).

## Running

```sh
npm start
```

This launches the Electron window, which spawns the Python engine for you.
To run the engine standalone while iterating on it (handy for reading its
logs, or testing the pipeline without opening a window):

```sh
cd engine
python3 -m switcher --config config.json
```

then, separately: `SWITCHER_SKIP_ENGINE=1 npm start` to point the Electron
UI at that already-running engine instead of spawning its own.

## Known limitations (MVP)

- **Single fixed audio feed** - one audio source is muxed straight into the
  stream; there's no per-camera "audio follows video" and no in-app mixing
  or level metering. For church use this is usually exactly what you want
  (one clean feed from the sound desk), but it means the engine doesn't
  touch audio routing at all beyond that one feed.
- **One overlay layer** - only a single NDI lower-third graphic is
  composited at a time; multiple simultaneous overlays aren't supported
  yet.
- **No recording** - only RTMP/SRT streaming out is wired up; a
  `filesink`/`splitmuxsink` branch off the `tee` would add local recording.
- Frame-accurate/keyed (fill+key) NDI compositing isn't implemented -
  overlay sources are expected to carry their own alpha (RGBA); worship
  software that only outputs separate key/fill NDI streams will need an
  `alphacombine` stage added to the overlay branch.
- Transition mixing (the alpha crossfade during AUTO) is stepped from a
  Python thread rather than driven by `GstController`, which is simple and
  fine at 25 steps/sec but not frame-accurate to the pipeline clock.
- The Settings screen edits `config.json` on disk only - there's no live
  pipeline reload, so adding/removing/rewiring a source needs a full engine
  restart (the **Restart Engine to apply** button does this for you). Its
  device detection (`engine/switcher/devices.py`) is best-effort: GStreamer's
  per-platform device properties aren't consistently documented, so it reads
  what's reliably there (e.g. a v4l2 device path) and falls back to
  enumeration order otherwise - verify against `gst-device-monitor-1.0` if a
  picked device doesn't behave.
- **Known open issue - intermittent stall with 2+ simultaneous synthetic
  test sources on Windows.** Tested on Windows with GStreamer 1.28.7 (the
  official `gstreamer-bundle` PyPI wheels, `pip install gstreamer-bundle` -
  no separate MSI needed for the engine's Python/`gi` bindings, native libs,
  or plugins; confirmed present: `mfvideosrc`, `compositor`, `input-selector`,
  `x264enc`, `flvmux`/`rtmpsink`, `mpegtsmux`/`srtsink`, `voaacenc`, and
  friends). With that setup, a config using two or more `"type": "test"`
  sources (`is-live=true` `videotestsrc`) occasionally has one of them
  produce a single buffer and then never produce again - confirmed via pad
  probes to be non-deterministic (identical repeated runs sometimes work,
  sometimes don't) and tied specifically to live clock pacing: forcing
  `is-live=false` makes it disappear every time (not a real fix - it also
  removes real-time pacing, so those sources would push frames as fast as
  possible instead of at the configured fps). This looks like a genuine
  GStreamer/Windows clock-contention race among multiple simultaneously
  "live" elements rather than a bug in this repo's pipeline construction
  (order-of-construction, `sync-streams`, and explicit clock/base-time
  assignment were all ruled out as the cause). **Real capture sources
  (`mfvideosrc`) are hardware/driver-timestamped, not software-clock-paced
  like `videotestsrc`, so this likely does not affect an actual camera
  setup** - it was only reproduced with 2-3 simultaneous synthetic test
  sources (e.g. the `bars`/`black` fallbacks in `config.example.json`
  running alongside each other or alongside a `test`-type stand-in for a
  camera). Worth re-testing against the official Inno-installer GStreamer
  build (see the Windows section above) rather than the pip bundle if it
  turns out to matter with real hardware.
