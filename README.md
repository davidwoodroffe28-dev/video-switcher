# video-switcher

A lightweight live video switcher: cut between camera/capture-card sources,
overlay lower-thirds pulled in over NDI (e.g. from ProPresenter/EasyWorship),
preview the program output, and push it out over RTMP or SRT. The media
pipeline is GStreamer; the control surface is a small Electron app.

## Architecture

```
Electron (electron/)                 Python engine (engine/)
┌───────────────────────┐            ┌─────────────────────────────────────┐
│ index.html/renderer.js│  WS :8765  │ input-selector ──▶ compositor ──▶ tee│
│  - source buttons     │◀──────────▶│   (cut sources)  (+ NDI overlay)     │
│  - overlay toggle     │            │                          │          │
│  - stream start/stop  │            │                          ├─▶ preview│
│                        │  MJPEG     │                          │  (jpeg)  │
│  <img src=".../mjpg"> │◀───:8080───│                          └─▶ RTMP/  │
└───────────────────────┘            │                             SRT     │
                                      └─────────────────────────────────────┘
```

`electron/main.js` spawns the Python engine (`python3 -m switcher`) as a
child process and opens the control window. The renderer talks to the
engine directly:

- a WebSocket (`/ws`) for control commands and status/error events
- a plain `<img>` tag pointed at an MJPEG endpoint (`/preview.mjpg`) for the
  live preview - no frame data is round-tripped through Electron's main
  process

The engine (`engine/switcher/`) builds one GStreamer pipeline:

- every **cut** source (capture card, test pattern, ...) feeds an
  `input-selector`, so switching is an instant, glitch-free pad change
- the selector's output is the base layer of a `compositor`
- the **overlay** source (NDI lower-thirds) is a second compositor layer
  whose opacity is toggled on/off at runtime - it stays composited over
  whichever cut source is currently live
- the composited program feeds a `tee`: one branch always runs to a JPEG
  preview (`appsink`), a second branch is created/torn down on demand when
  you start/stop an RTMP or SRT stream

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

Install the official GStreamer runtime **and** development MSIs from
gstreamer.freedesktop.org (pick the "complete" component set, which
includes the Python/GI bindings), then run the app with the GStreamer
Python from that install. Capture is via `mfvideosrc`.

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

Copy `engine/config.example.json` to `engine/config.json` and edit it (or
point `SWITCHER_CONFIG` at any path). Each entry in `sources` is either:

- `"role": "cut"` - a switchable camera/capture/test source
- `"role": "overlay"` - the one NDI lower-thirds layer, composited on top

```json
{ "id": "cam1", "label": "Main Camera", "type": "capture", "role": "cut", "device": "/dev/video0" }
{ "id": "lower3rd", "label": "Lower Thirds (NDI)", "type": "ndi", "role": "overlay", "ndiName": "WORSHIP-PC (ProPresenter)" }
```

`type` is one of `capture`, `ndi`, `test`.

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

- **Video only** - no audio path yet. Adding an `audiomixer` branch
  alongside the video `tee`/`compositor` is the natural next step.
- **One overlay layer** - only a single NDI lower-third graphic is
  composited at a time; multiple simultaneous overlays aren't supported
  yet.
- **No recording** - only RTMP/SRT streaming out is wired up; a
  `filesink`/`splitmuxsink` branch off the `tee` would add local recording.
- Frame-accurate/keyed (fill+key) NDI compositing isn't implemented -
  overlay sources are expected to carry their own alpha (RGBA); worship
  software that only outputs separate key/fill NDI streams will need an
  `alphacombine` stage added to the overlay branch.
