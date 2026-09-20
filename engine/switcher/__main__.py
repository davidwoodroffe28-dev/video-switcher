import argparse
import asyncio
import logging

from .config import load_config
from .pipeline import SwitcherPipeline
from .server import Server


def main():
    parser = argparse.ArgumentParser(description="video-switcher GStreamer engine")
    parser.add_argument("--config", help="path to a config JSON file", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = load_config(args.config)
    pipeline = SwitcherPipeline(config)
    server = Server(pipeline, config)

    pipeline.start()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
