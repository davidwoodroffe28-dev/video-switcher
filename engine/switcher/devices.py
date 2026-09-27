"""Best-effort camera/mic enumeration for the Settings screen's device pickers.

GStreamer's DeviceMonitor reports a display name plus a GstStructure of
properties whose exact keys are provider-specific (v4l2 vs. Media
Foundation vs. AVFoundation) and aren't consistently documented. Rather
than depend on exact property keys across all three platforms, this reads
the couple of keys that are reliably present when available (a v4l2
device path) and otherwise falls back to positional index within its
class - which is what v4l2src/mfvideosrc/avfvideosrc expect by
convention (enumeration order), but isn't guaranteed by GStreamer to stay
stable across a device plug/unplug. Treat the result as a starting point
to verify against `gst-device-monitor-1.0`, not gospel - same spirit as
this project's other best-effort importers (see the EasyWorship song
importer in the Sanctuary app).
"""
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def _device_path(props):
    if props is None:
        return None
    for key in ("device.path", "api.v4l2.path"):
        if props.has_field(key):
            return props.get_string(key)
    return None


def list_devices():
    Gst.init(None)
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", None)
    monitor.add_filter("Audio/Source", None)
    monitor.start()
    try:
        devices = monitor.get_devices()
    finally:
        monitor.stop()

    result = {"video": [], "audio": []}
    counters = {"video": 0, "audio": 0}
    for dev in devices:
        klass = dev.get_device_class()
        bucket = "video" if klass.startswith("Video") else "audio" if klass.startswith("Audio") else None
        if bucket is None:
            continue
        props = dev.get_properties()
        entry = {
            "name": dev.get_display_name(),
            "deviceIndex": counters[bucket],
            "path": _device_path(props),
        }
        counters[bucket] += 1
        result[bucket].append(entry)
    return result
