"""Builds and controls the GStreamer program pipeline.

Topology (two-stage PGM/PVW, like a hardware M/E switcher):

  each cut source -> per-source tee -> sel_A (row A selector)
                                     `-> sel_B (row B selector)

  sel_A -> compositor sink (row A layer, zorder/alpha swap on take())  \\
  sel_B -> compositor sink (row B layer, zorder/alpha swap on take())   >- compositor (comp)
  overlay source (optional, NDI)  -> compositor sink (always on top)  /
                                                                       -> tee (program_tee)
                                                                            |-> preview branch (jpeg -> appsink)
                                                                            `-> stream branch (x264/aac -> rtmp/srt),
                                                                                added/removed at runtime

  Row A and row B each also tap their own small JPEG preview branch, so the
  UI can show a live PVW monitor for "whichever row is currently not on
  air" without any pipeline rewiring on every take().

  audio source (optional) -> tee (audio_tee) -> muxed into the stream branch
  when one is active. Audio is NOT part of the video crossfade - the church
  sound desk's mix plays straight through regardless of which camera is on
  air.

Exactly one row is "on air" (opaque, zorder 0) and the other is "preview"
(zorder 1, alpha animated 0->1 by take()). Sources are loaded into the
*preview* row only - loading a source never touches the program output.
take() animates the preview row over the program row, then swaps which row
is which so the row that was just previewed becomes the new opaque base
and the old on-air row becomes the next, currently-hidden preview row.
"""
import platform
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

QUEUE_LEAK_DOWNSTREAM = 2
ROWS = ("A", "B")


def _request_pad(element, template):
    if hasattr(element, "request_pad_simple"):
        return element.request_pad_simple(template)
    return element.get_request_pad(template)


def _make(factory, name):
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(
            f"failed to create GStreamer element '{factory}' - is the plugin providing it installed?"
        )
    return el


def _capture_source_element(cfg):
    system = platform.system()
    if system == "Linux":
        el = _make("v4l2src", f"{cfg['id']}_src")
        el.set_property("device", cfg.get("device", "/dev/video0"))
    elif system == "Darwin":
        el = _make("avfvideosrc", f"{cfg['id']}_src")
        if "deviceIndex" in cfg:
            el.set_property("device-index", cfg["deviceIndex"])
    elif system == "Windows":
        el = _make("mfvideosrc", f"{cfg['id']}_src")
        if "deviceIndex" in cfg:
            el.set_property("device-index", cfg["deviceIndex"])
    else:
        raise RuntimeError(f"unsupported platform for a capture source: {system}")
    return el


def _ndi_source_element(cfg):
    el = _make("ndisrc", f"{cfg['id']}_src")
    el.set_property("ndi-name", cfg["ndiName"])
    return el


_TEST_PATTERNS = {"smpte": 0, "black": 2, "white": 3, "ball": 18}


def _test_source_element(cfg):
    el = _make("videotestsrc", f"{cfg['id']}_src")
    el.set_property("is-live", True)
    el.set_property("pattern", _TEST_PATTERNS.get(cfg.get("pattern", "smpte"), 0))
    return el


def _make_source_element(cfg):
    source_type = cfg["type"]
    if source_type == "capture":
        return _capture_source_element(cfg)
    if source_type == "ndi":
        return _ndi_source_element(cfg)
    if source_type == "test":
        return _test_source_element(cfg)
    raise ValueError(f"unknown source type: {source_type}")


def _leaky_video_queue(name):
    queue = _make("queue", name)
    queue.set_property("leaky", QUEUE_LEAK_DOWNSTREAM)
    queue.set_property("max-size-buffers", 2)
    return queue


def _audio_queue(name):
    # Audio must not drop data the way the leaky video queues do (that
    # would click/glitch), so this just uses a modest time-based bound.
    queue = _make("queue", name)
    queue.set_property("max-size-time", Gst.SECOND)
    queue.set_property("max-size-buffers", 0)
    queue.set_property("max-size-bytes", 0)
    return queue


def _make_audio_source_element(cfg):
    source_type = cfg["type"]
    if source_type == "alsa":
        el = _make("alsasrc", "audio_src")
        el.set_property("device", cfg.get("device", "default"))
    elif source_type == "pulse":
        el = _make("pulsesrc", "audio_src")
        if "device" in cfg:
            el.set_property("device", cfg["device"])
    elif source_type == "test":
        el = _make("audiotestsrc", "audio_src")
        el.set_property("is-live", True)
    else:
        raise ValueError(f"unknown audio source type: {source_type}")
    return el


class SwitcherPipeline:
    def __init__(self, config):
        Gst.init(None)
        self.config = config
        self.pipeline = Gst.Pipeline.new("switcher")

        self.sources = {}  # cut source id -> {"cfg": dict, "pads": {"A": Gst.Pad, "B": Gst.Pad}}
        self.row_selectors = {}  # "A"/"B" -> input-selector element
        self.row_comp_pads = {}  # "A"/"B" -> compositor sink pad
        self.row_preview_listeners = {"A": [], "B": []}
        self.on_air_row = "A"
        self.preview_row = "B"
        self._transitioning = False

        self.overlay_pad = None
        self.overlay_enabled = False
        self.audio_tee = None
        self.stream_branch = None

        self.preview_listeners = []  # program (on-air) preview
        self.status_listeners = []
        self.error_listeners = []
        self._bus_thread = None

        prog = config["program"]
        self.program_caps = Gst.Caps.from_string(
            f"video/x-raw,width={prog['width']},height={prog['height']},framerate={prog['fps']}/1"
        )

        self._build()

    # --- construction -----------------------------------------------------

    def _build(self):
        self.comp = _make("compositor", "comp")
        self.pipeline.add(self.comp)

        for row in ROWS:
            self._build_row(row)

        self.row_comp_pads["A"].set_property("zorder", 0)
        self.row_comp_pads["A"].set_property("alpha", 1.0)
        self.row_comp_pads["B"].set_property("zorder", 1)
        self.row_comp_pads["B"].set_property("alpha", 0.0)

        post_convert = _make("videoconvert", "post_convert")
        post_caps = _make("capsfilter", "post_caps")
        prog = self.config["program"]
        post_caps.set_property(
            "caps", Gst.Caps.from_string(f"video/x-raw,width={prog['width']},height={prog['height']}")
        )
        self.tee = _make("tee", "program_tee")
        for el in (post_convert, post_caps, self.tee):
            self.pipeline.add(el)
        self.comp.link(post_convert)
        post_convert.link(post_caps)
        post_caps.link(self.tee)

        cut_sources = [s for s in self.config["sources"] if s["role"] == "cut"]
        for src_cfg in cut_sources:
            self._add_cut_source(src_cfg)

        first = cut_sources[0]["id"]
        second = cut_sources[1]["id"] if len(cut_sources) > 1 else first
        self.row_selectors["A"].set_property("active-pad", self.sources[first]["pads"]["A"])
        self.row_selectors["B"].set_property("active-pad", self.sources[second]["pads"]["B"])

        overlay_cfg = next((s for s in self.config["sources"] if s["role"] == "overlay"), None)
        if overlay_cfg:
            self._add_overlay_source(overlay_cfg)

        if self.config.get("audio", {}).get("enabled"):
            self._build_audio(self.config["audio"])

        self._add_program_preview_branch()

        self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True)

    def _build_row(self, row):
        selector = _make("input-selector", f"sel_{row}")
        convert = _make("videoconvert", f"row_{row}_convert")
        scale = _make("videoscale", f"row_{row}_scale")
        caps = _make("capsfilter", f"row_{row}_caps")
        caps.set_property("caps", self.program_caps)
        tap_tee = _make("tee", f"row_{row}_tap_tee")
        to_comp_queue = _leaky_video_queue(f"row_{row}_to_comp_queue")

        for el in (selector, convert, scale, caps, tap_tee, to_comp_queue):
            self.pipeline.add(el)
        selector.link(convert)
        convert.link(scale)
        scale.link(caps)
        caps.link(tap_tee)

        tap_to_comp = _request_pad(tap_tee, "src_%u")
        tap_to_comp.link(to_comp_queue.get_static_pad("sink"))
        comp_pad = _request_pad(self.comp, "sink_%u")
        to_comp_queue.get_static_pad("src").link(comp_pad)

        self.row_selectors[row] = selector
        self.row_comp_pads[row] = comp_pad
        self._add_row_preview_branch(row, tap_tee)

    def _add_row_preview_branch(self, row, tap_tee):
        prog = self.config["program"]
        preview_cfg = self.config.get("preview", {})
        pw = preview_cfg.get("rowWidth", min(480, prog["width"]))
        ph = preview_cfg.get("rowHeight", min(270, prog["height"]))

        pad = _request_pad(tap_tee, "src_%u")
        queue = _leaky_video_queue(f"row_{row}_preview_queue")
        convert = _make("videoconvert", f"row_{row}_preview_convert")
        scale = _make("videoscale", f"row_{row}_preview_scale")
        caps = _make("capsfilter", f"row_{row}_preview_caps")
        caps.set_property("caps", Gst.Caps.from_string(f"video/x-raw,width={pw},height={ph}"))
        enc = _make("jpegenc", f"row_{row}_preview_enc")
        enc.set_property("quality", 70)
        sink = _make("appsink", f"row_{row}_preview_sink")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("sync", False)
        sink.connect("new-sample", self._on_row_preview_sample, row)

        for el in (queue, convert, scale, caps, enc, sink):
            self.pipeline.add(el)
        pad.link(queue.get_static_pad("sink"))
        queue.link(convert)
        convert.link(scale)
        scale.link(caps)
        caps.link(enc)
        enc.link(sink)

    def _add_cut_source(self, cfg):
        src_el = _make_source_element(cfg)
        convert = _make("videoconvert", f"{cfg['id']}_convert")
        scale = _make("videoscale", f"{cfg['id']}_scale")
        rate = _make("videorate", f"{cfg['id']}_rate")
        caps = _make("capsfilter", f"{cfg['id']}_caps")
        caps.set_property("caps", self.program_caps)
        source_tee = _make("tee", f"{cfg['id']}_tee")

        for el in (src_el, convert, scale, rate, caps, source_tee):
            self.pipeline.add(el)
        src_el.link(convert)
        convert.link(scale)
        scale.link(rate)
        rate.link(caps)
        caps.link(source_tee)

        pads = {}
        for row in ROWS:
            queue = _leaky_video_queue(f"{cfg['id']}_to_{row}_queue")
            self.pipeline.add(queue)
            tee_pad = _request_pad(source_tee, "src_%u")
            tee_pad.link(queue.get_static_pad("sink"))
            sel_pad = _request_pad(self.row_selectors[row], "sink_%u")
            queue.get_static_pad("src").link(sel_pad)
            pads[row] = sel_pad

        self.sources[cfg["id"]] = {"cfg": cfg, "pads": pads}

    def _add_overlay_source(self, cfg):
        src_el = _make_source_element(cfg)
        convert = _make("videoconvert", "overlay_convert")
        scale = _make("videoscale", "overlay_scale")
        caps = _make("capsfilter", "overlay_caps")
        prog = self.config["program"]
        caps.set_property(
            "caps",
            Gst.Caps.from_string(f"video/x-raw,format=RGBA,width={prog['width']},height={prog['height']}"),
        )
        queue = _leaky_video_queue("overlay_queue")

        for el in (src_el, convert, scale, caps, queue):
            self.pipeline.add(el)
        src_el.link(convert)
        convert.link(scale)
        scale.link(caps)
        caps.link(queue)

        pad = _request_pad(self.comp, "sink_%u")
        queue.get_static_pad("src").link(pad)
        pad.set_property("zorder", 2)  # always above both program rows
        pad.set_property("alpha", 0.0)

        self.overlay_pad = pad
        self.sources[cfg["id"]] = {"cfg": cfg, "pads": {"A": None, "B": None}}

    def _build_audio(self, audio_cfg):
        src_el = _make_audio_source_element(audio_cfg["source"])
        convert = _make("audioconvert", "audio_convert")
        resample = _make("audioresample", "audio_resample")
        queue = _audio_queue("audio_queue")
        tee = _make("tee", "audio_tee")

        for el in (src_el, convert, resample, queue, tee):
            self.pipeline.add(el)
        src_el.link(convert)
        convert.link(resample)
        resample.link(queue)
        queue.link(tee)

        self.audio_tee = tee

    def _add_program_preview_branch(self):
        prog = self.config["program"]
        preview_cfg = self.config.get("preview", {})
        pw = preview_cfg.get("width", min(640, prog["width"]))
        ph = preview_cfg.get("height", min(360, prog["height"]))

        queue = _leaky_video_queue("preview_queue")
        convert = _make("videoconvert", "preview_convert")
        scale = _make("videoscale", "preview_scale")
        caps = _make("capsfilter", "preview_caps")
        caps.set_property("caps", Gst.Caps.from_string(f"video/x-raw,width={pw},height={ph}"))
        enc = _make("jpegenc", "preview_enc")
        enc.set_property("quality", 80)
        self.preview_sink = _make("appsink", "preview_sink")
        self.preview_sink.set_property("emit-signals", True)
        self.preview_sink.set_property("max-buffers", 1)
        self.preview_sink.set_property("drop", True)
        self.preview_sink.set_property("sync", False)
        self.preview_sink.connect("new-sample", self._on_program_preview_sample)

        for el in (queue, convert, scale, caps, enc, self.preview_sink):
            self.pipeline.add(el)

        tee_pad = _request_pad(self.tee, "src_%u")
        tee_pad.link(queue.get_static_pad("sink"))
        queue.link(convert)
        convert.link(scale)
        scale.link(caps)
        caps.link(enc)
        enc.link(self.preview_sink)

    # --- lifecycle ----------------------------------------------------

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        self._bus_thread.start()

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    # --- control: source selection ----------------------------------------

    def list_sources(self):
        return [
            {"id": sid, "label": e["cfg"]["label"], "role": e["cfg"]["role"], "type": e["cfg"]["type"]}
            for sid, e in self.sources.items()
        ]

    def load_preview(self, source_id):
        """Point the (currently hidden) preview row at a cut source. Does not touch program."""
        entry = self.sources.get(source_id)
        if entry is None or entry["pads"][self.preview_row] is None:
            raise ValueError(f"unknown cut source: {source_id}")
        self.row_selectors[self.preview_row].set_property("active-pad", entry["pads"][self.preview_row])
        self._notify_status()

    def set_overlay(self, enabled):
        if self.overlay_pad is None:
            raise ValueError("no overlay source configured")
        self.overlay_pad.set_property("alpha", 1.0 if enabled else 0.0)
        self.overlay_enabled = bool(enabled)
        self._notify_status()

    def _source_for_pad(self, row, pad):
        for sid, entry in self.sources.items():
            if entry["pads"][row] is pad:
                return sid
        return None

    def get_program_source(self):
        pad = self.row_selectors[self.on_air_row].get_property("active-pad")
        return self._source_for_pad(self.on_air_row, pad)

    def get_preview_source(self):
        pad = self.row_selectors[self.preview_row].get_property("active-pad")
        return self._source_for_pad(self.preview_row, pad)

    def status(self):
        return {
            "sources": self.list_sources(),
            "programSource": self.get_program_source(),
            "previewSource": self.get_preview_source(),
            "previewRow": self.preview_row,
            "transitioning": self._transitioning,
            "overlayEnabled": self.overlay_enabled,
            "streaming": self.stream_branch is not None,
        }

    def _notify_status(self):
        s = self.status()
        for listener in list(self.status_listeners):
            listener(s)

    def _notify_error(self, message):
        for listener in list(self.error_listeners):
            listener(message)

    # --- control: transitions ----------------------------------------------

    def take(self, mode="fade", duration_ms=500):
        """Take the preview row to air: "cut" (instant) or "fade" (crossfade)."""
        if self._transitioning:
            raise RuntimeError("a transition is already in progress")
        if mode not in ("cut", "fade"):
            raise ValueError(f"unknown transition mode: {mode}")

        self._transitioning = True
        self._notify_status()

        top_row = self.preview_row
        bottom_row = self.on_air_row
        top_pad = self.row_comp_pads[top_row]

        def _run():
            if mode == "cut" or duration_ms <= 0:
                top_pad.set_property("alpha", 1.0)
            else:
                steps = max(1, int(duration_ms / 40))
                step_sleep = (duration_ms / 1000.0) / steps
                for i in range(1, steps + 1):
                    top_pad.set_property("alpha", i / steps)
                    time.sleep(step_sleep)

            bottom_pad = self.row_comp_pads[bottom_row]
            bottom_pad.set_property("alpha", 0.0)
            bottom_pad.set_property("zorder", 1)
            top_pad.set_property("zorder", 0)
            top_pad.set_property("alpha", 1.0)

            self.on_air_row, self.preview_row = top_row, bottom_row
            self._transitioning = False
            self._notify_status()

        threading.Thread(target=_run, daemon=True).start()

    # --- streaming (RTMP/SRT, video + optional audio) -----------------------

    def start_stream(self, url, kind="rtmp", bitrate=4000):
        if self.stream_branch is not None:
            raise RuntimeError("already streaming - stop the current stream first")
        if kind not in ("rtmp", "srt"):
            raise ValueError(f"unknown stream kind: {kind}")

        video_queue = _leaky_video_queue("stream_video_queue")
        video_convert = _make("videoconvert", "stream_video_convert")
        video_enc = _make("x264enc", "stream_video_enc")
        video_enc.set_property("tune", "zerolatency")
        video_enc.set_property("bitrate", bitrate)
        video_enc.set_property("speed-preset", "veryfast")
        video_enc.set_property("key-int-max", self.config["program"]["fps"] * 2)
        video_parse = _make("h264parse", "stream_video_parse")
        video_elements = [video_queue, video_convert, video_enc, video_parse]

        if kind == "rtmp":
            mux = _make("flvmux", "stream_mux")
            mux.set_property("streamable", True)
            sink = _make("rtmpsink", "stream_sink")
            sink.set_property("location", url)
        else:
            mux = _make("mpegtsmux", "stream_mux")
            sink = _make("srtsink", "stream_sink")
            sink.set_property("uri", url)

        all_elements = list(video_elements) + [mux, sink]

        audio_elements = []
        audio_tee_pad = None
        if self.audio_tee is not None:
            audio_queue = _audio_queue("stream_audio_queue")
            audio_convert = _make("audioconvert", "stream_audio_convert")
            audio_enc = _make("voaacenc", "stream_audio_enc")
            audio_enc.set_property("bitrate", 128000)
            audio_parse = _make("aacparse", "stream_audio_parse")
            audio_elements = [audio_queue, audio_convert, audio_enc, audio_parse]
            all_elements += audio_elements

        for el in all_elements:
            self.pipeline.add(el)

        video_queue.link(video_convert)
        video_convert.link(video_enc)
        video_enc.link(video_parse)
        video_mux_pad = _request_pad(mux, "video") if kind == "rtmp" else _request_pad(mux, "sink_%d")
        video_parse.get_static_pad("src").link(video_mux_pad)
        mux.link(sink)

        if audio_elements:
            audio_queue, audio_convert, audio_enc, audio_parse = audio_elements
            audio_queue.link(audio_convert)
            audio_convert.link(audio_enc)
            audio_enc.link(audio_parse)
            audio_mux_pad = _request_pad(mux, "audio") if kind == "rtmp" else _request_pad(mux, "sink_%d")
            audio_parse.get_static_pad("src").link(audio_mux_pad)

        for el in all_elements:
            el.sync_state_with_parent()

        video_tee_pad = _request_pad(self.tee, "src_%u")
        video_tee_pad.link(video_queue.get_static_pad("sink"))

        if audio_elements:
            audio_tee_pad = _request_pad(self.audio_tee, "src_%u")
            audio_tee_pad.link(audio_elements[0].get_static_pad("sink"))

        self.stream_branch = {
            "elements": all_elements,
            "video_tee_pad": video_tee_pad,
            "video_queue": video_queue,
            "audio_tee_pad": audio_tee_pad,
            "audio_queue": audio_elements[0] if audio_elements else None,
        }
        self._notify_status()

    def stop_stream(self):
        branch = self.stream_branch
        if branch is None:
            return
        self.stream_branch = None

        taps = [(branch["video_tee_pad"], branch["video_queue"], self.tee)]
        if branch["audio_tee_pad"] is not None:
            taps.append((branch["audio_tee_pad"], branch["audio_queue"], self.audio_tee))

        remaining = {"count": len(taps)}
        lock = threading.Lock()

        def _teardown():
            for el in branch["elements"]:
                el.set_state(Gst.State.NULL)
                self.pipeline.remove(el)

        def _make_probe(tee_pad, queue_el, source_tee):
            def _on_blocked(pad, info):
                tee_pad.unlink(queue_el.get_static_pad("sink"))
                source_tee.release_request_pad(tee_pad)
                with lock:
                    remaining["count"] -= 1
                    done = remaining["count"] == 0
                if done:
                    _teardown()
                return Gst.PadProbeReturn.REMOVE

            return _on_blocked

        for tee_pad, queue_el, source_tee in taps:
            tee_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, _make_probe(tee_pad, queue_el, source_tee))

        self._notify_status()

    # --- preview / bus ----------------------------------------------------

    def _on_program_preview_sample(self, sink):
        return self._emit_preview_sample(sink, self.preview_listeners)

    def _on_row_preview_sample(self, sink, row):
        return self._emit_preview_sample(sink, self.row_preview_listeners[row])

    @staticmethod
    def _emit_preview_sample(sink, listeners):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            for listener in list(listeners):
                listener(data)
        return Gst.FlowReturn.OK

    def _bus_loop(self):
        bus = self.pipeline.get_bus()
        while True:
            msg = bus.timed_pop_filtered(
                Gst.CLOCK_TIME_NONE,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self._notify_error(f"{err}: {debug}")
            elif msg.type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                print(f"[gst warning] {warn}: {debug}")
            elif msg.type == Gst.MessageType.EOS:
                print("[gst] end of stream")
