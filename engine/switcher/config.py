"""Loads and validates the switcher's JSON config."""
import json
import os

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json"
)


def load_config(path=None):
    path = path or os.environ.get("SWITCHER_CONFIG") or DEFAULT_CONFIG_PATH
    with open(path, "r") as f:
        config = json.load(f)

    config.setdefault("program", {}).setdefault("width", 1280)
    config["program"].setdefault("height", 720)
    config["program"].setdefault("fps", 30)

    config.setdefault("preview", {}).setdefault("port", 8080)
    config.setdefault("control", {}).setdefault("port", 8765)

    sources = config.get("sources", [])
    if not sources:
        raise ValueError("config must define at least one source")

    cut_sources = [s for s in sources if s.get("role", "cut") == "cut"]
    if not cut_sources:
        raise ValueError("config must define at least one 'cut' source")

    overlay_sources = [s for s in sources if s.get("role") == "overlay"]
    if len(overlay_sources) > 1:
        raise ValueError("only one overlay source is currently supported")

    seen_ids = set()
    for s in sources:
        if "id" not in s or "type" not in s:
            raise ValueError(f"source missing id/type: {s}")
        if s["id"] in seen_ids:
            raise ValueError(f"duplicate source id: {s['id']}")
        seen_ids.add(s["id"])
        s.setdefault("label", s["id"])
        s.setdefault("role", "cut")

    return config
