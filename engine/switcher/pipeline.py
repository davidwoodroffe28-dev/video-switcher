"""Builds and controls the GStreamer program pipeline.

Topology:

  cut sources -> input-selector (sel) -> compositor sink_0 \\
  overlay source (optional)     -> compositor sink_1 (alpha toggled)  -> compositor (comp)
                                                                       -> tee (program_tee)
                                                                            |-> preview branch (jpeg -> appsink)
                                                                            `-> stream branch (x264 -> rtmp/srt), added/removed at runtime
"""
import platform
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

QUEUE_LEAK_DOWNSTREAM = 2


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


def _leaky_queue(name):
    queue = _make("queue", name)
    queue.set_property("leaky", QUEUE_LEAK_DOWNSTREAM)
    queue.set_property("max-size-buffers", 2)
    return queue


class SwitcherPipeline:
    def __init__(self, config):
        Gst.init(None)
        self.config = config
        self.pipeline = Gst.Pipeline.new("switcher")
        self.sources = {}  # id -> {"cfg": dict, "sel_pad": Gst.Pad|None}
        self.overlay_pad = None
        self.overlay_enabled = False
        self.stream_branch = None
        self.preview_listeners = []
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
        self.sel = _make("input-selector", "sel")
        self.comp = _make("compositor", "comp")
        post_convert = _make("videoconvert", "post_convert")
        post_caps = _make("capsfilter", "post_caps")
        prog = self.config["program"]
        post_caps.set_property(
            "caps", Gst.Caps.from_string(f"video/x-raw,width={prog['width']},height={prog['height']}")
        )
        self.tee = _make("tee", "program_tee")

        for el in (self.sel, self.comp, post_convert, post_caps, self.tee):
            self.pipeline.add(el)

        sel_out_convert = _make("videoconvert", "sel_out_convert")
        sel_out_scale = _make("videoscale", "sel_out_scale")
        sel_out_caps = _make("capsfilter", "sel_out_caps")
        sel_out_caps.set_property("caps", self.program_caps)
        for el in (sel_out_convert, sel_out_scale, sel_out_caps):
            self.pipeline.add(el)
        self.sel.link(sel_out_convert)
        sel_out_convert.link(sel_out_scale)
        sel_out_scale.link(sel_out_caps)

        comp_pad0 = _request_pad(self.comp, "sink_%u")
        sel_out_caps.get_static_pad("src").link(comp_pad0)
        comp_pad0.set_property("zorder", 0)

        self.comp.link(post_convert)
        post_convert.link(post_caps)
        post_caps.link(self.tee)

        first_pad = None
        for src_cfg in self.config["sources"]:
            if src_cfg["role"] == "cut":
                pad = self._add_cut_source(src_cfg)
                first_pad = first_pad or pad
        if first_pad is not None:
            self.sel.set_property("active-pad", first_pad)

        overlay_cfg = next((s for s in self.config["sources"] if s["role"] == "overlay"), None)
        if overlay_cfg:
            self._add_overlay_source(overlay_cfg)

        self._add_preview_branch()

        self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True)

    def _add_cut_source(self, cfg):
        src_el = _make_source_element(cfg)
        convert = _make("videoconvert", f"{cfg['id']}_convert")
        scale = _make("videoscale", f"{cfg['id']}_scale")
        rate = _make("videorate", f"{cfg['id']}_rate")
        caps = _make("capsfilter", f"{cfg['id']}_caps")
        caps.set_property("caps", self.program_caps)
        queue = _leaky_queue(f"{cfg['id']}_queue")

        for el in (src_el, convert, scale, rate, caps, queue):
            self.pipeline.add(el)
        src_el.link(convert)
        convert.link(scale)
        scale.link(rate)
        rate.link(caps)
        caps.link(queue)

        sel_pad = _request_pad(self.sel, "sink_%u")
        queue.get_static_pad("src").link(sel_pad)

        self.sources[cfg["id"]] = {"cfg": cfg, "sel_pad": sel_pad}
        return sel_pad

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
        queue = _leaky_queue("overlay_queue")

        for el in (src_el, convert, scale, caps, queue):
            self.pipeline.add(el)
        src_el.link(convert)
        convert.link(scale)
        scale.link(caps)
        caps.link(queue)

        pad = _request_pad(self.comp, "sink_%u")
        queue.get_static_pad("src").link(pad)
        pad.set_property("zorder", 1)
        pad.set_property("alpha", 0.0)

        self.overlay_pad = pad
        self.sources[cfg["id"]] = {"cfg": cfg, "sel_pad": None}

    def _add_preview_branch(self):
        prog = self.config["program"]
        preview_cfg = self.config.get("preview", {})
        pw = preview_cfg.get("width", min(640, prog["width"]))
        ph = preview_cfg.get("height", min(360, prog["height"]))

        queue = _leaky_queue("preview_queue")
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
        self.preview_sink.connect("new-sample", self._on_preview_sample)

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

    # --- control --------------------------------------------------------

    def list_sources(self):
        return [
            {"id": sid, "label": e["cfg"]["label"], "role": e["cfg"]["role"], "type": e["cfg"]["type"]}
            for sid, e in self.sources.items()
        ]

    def cut(self, source_id):
        entry = self.sources.get(source_id)
        if entry is None or entry["sel_pad"] is None:
            raise ValueError(f"unknown cut source: {source_id}")
        self.sel.set_property("active-pad", entry["sel_pad"])
        self._notify_status()

    def set_overlay(self, enabled):
        if self.overlay_pad is None:
            raise ValueError("no overlay source configured")
        self.overlay_pad.set_property("alpha", 1.0 if enabled else 0.0)
        self.overlay_enabled = bool(enabled)
        self._notify_status()

    def get_active_source(self):
        active_pad = self.sel.get_property("active-pad")
        for sid, entry in self.sources.items():
            if entry["sel_pad"] is active_pad:
                return sid
        return None

    def status(self):
        return {
            "sources": self.list_sources(),
            "activeSource": self.get_active_source(),
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

    # --- streaming (RTMP/SRT) -------------------------------------------

    def start_stream(self, url, kind="rtmp", bitrate=4000):
        if self.stream_branch is not None:
            raise RuntimeError("already streaming - stop the current stream first")

        queue = _leaky_queue("stream_queue")
        convert = _make("videoconvert", "stream_convert")
        enc = _make("x264enc", "stream_enc")
        enc.set_property("tune", "zerolatency")
        enc.set_property("bitrate", bitrate)
        enc.set_property("speed-preset", "veryfast")
        enc.set_property("key-int-max", self.config["program"]["fps"] * 2)
        parse = _make("h264parse", "stream_parse")

        if kind == "rtmp":
            mux = _make("flvmux", "stream_mux")
            mux.set_property("streamable", True)
            sink = _make("rtmpsink", "stream_sink")
            sink.set_property("location", url)
        elif kind == "srt":
            mux = _make("mpegtsmux", "stream_mux")
            sink = _make("srtsink", "stream_sink")
            sink.set_property("uri", url)
        else:
            raise ValueError(f"unknown stream kind: {kind}")

        elements = [queue, convert, enc, parse, mux, sink]
        for el in elements:
            self.pipeline.add(el)

        queue.link(convert)
        convert.link(enc)
        enc.link(parse)
        parse.link(mux)
        mux.link(sink)

        for el in elements:
            el.sync_state_with_parent()

        tee_pad = _request_pad(self.tee, "src_%u")
        tee_pad.link(queue.get_static_pad("sink"))

        self.stream_branch = {"elements": elements, "tee_pad": tee_pad, "queue": queue}
        self._notify_status()

    def stop_stream(self):
        branch = self.stream_branch
        if branch is None:
            return
        self.stream_branch = None
        tee_pad = branch["tee_pad"]

        def _on_blocked(pad, info):
            queue_sink = branch["queue"].get_static_pad("sink")
            tee_pad.unlink(queue_sink)
            self.tee.release_request_pad(tee_pad)
            for el in branch["elements"]:
                el.set_state(Gst.State.NULL)
                self.pipeline.remove(el)
            return Gst.PadProbeReturn.REMOVE

        tee_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, _on_blocked)
        self._notify_status()

    # --- preview / bus ----------------------------------------------------

    def _on_preview_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            for listener in list(self.preview_listeners):
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
