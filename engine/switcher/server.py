"""Local control + preview server.

- WebSocket "/ws"                JSON control protocol, also broadcasts status/error events.
- HTTP     "/preview/program.mjpg"  motion-JPEG stream of the on-air program output.
- HTTP     "/preview/row_a.mjpg"    motion-JPEG stream of row A (whichever role it's playing).
- HTTP     "/preview/row_b.mjpg"    motion-JPEG stream of row B.

The renderer points its PVW monitor's <img> at row_a/row_b depending on
which one `status.previewRow` currently says is the preview row - see
electron/renderer.js. All of these are consumed directly by the Electron
renderer; there is no IPC hop through Electron's main process.
"""
import asyncio
import json
import logging

from aiohttp import web, WSMsgType

logger = logging.getLogger("switcher.server")

BOUNDARY = "switcherframe"


class MjpegChannel:
    """Fans one JPEG-frame source out to any number of HTTP clients."""

    def __init__(self):
        self.queues = set()

    def broadcast_threadsafe(self, loop, data):
        if loop is None:
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(self._broadcast(data)))

    async def _broadcast(self, data):
        for queue in list(self.queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def handle(self, request):
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}"},
        )
        await response.prepare(request)

        queue = asyncio.Queue(maxsize=1)
        self.queues.add(queue)
        try:
            while True:
                data = await queue.get()
                chunk = (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n"
                ).encode("ascii") + data + b"\r\n"
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.queues.discard(queue)
        return response


class Server:
    def __init__(self, pipeline, config):
        self.pipeline = pipeline
        self.config = config
        self.loop = None
        self.ws_clients = set()

        self.channels = {
            "program": MjpegChannel(),
            "row_a": MjpegChannel(),
            "row_b": MjpegChannel(),
        }

        self.pipeline.preview_listeners.append(self._on_program_frame)
        self.pipeline.row_preview_listeners["A"].append(self._on_row_a_frame)
        self.pipeline.row_preview_listeners["B"].append(self._on_row_b_frame)
        self.pipeline.status_listeners.append(self._on_status)
        self.pipeline.error_listeners.append(self._on_error)

        self.app = web.Application()
        self.app.router.add_get("/ws", self._handle_ws)
        self.app.router.add_get("/preview/program.mjpg", self.channels["program"].handle)
        self.app.router.add_get("/preview/row_a.mjpg", self.channels["row_a"].handle)
        self.app.router.add_get("/preview/row_b.mjpg", self.channels["row_b"].handle)
        self.app.router.add_get("/health", lambda req: web.json_response({"ok": True}))

    # --- pipeline callbacks (called from GStreamer threads) --------------

    def _on_program_frame(self, data):
        self.channels["program"].broadcast_threadsafe(self.loop, data)

    def _on_row_a_frame(self, data):
        self.channels["row_a"].broadcast_threadsafe(self.loop, data)

    def _on_row_b_frame(self, data):
        self.channels["row_b"].broadcast_threadsafe(self.loop, data)

    def _on_status(self, status):
        self._call_threadsafe(self._broadcast, {"event": "status", "data": status})

    def _on_error(self, message):
        logger.error("pipeline error: %s", message)
        self._call_threadsafe(self._broadcast, {"event": "error", "data": {"message": message}})

    def _call_threadsafe(self, coro_func, *args):
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(coro_func(*args)))

    # --- websocket control -------------------------------------------------

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        try:
            await ws.send_json({"event": "status", "data": self.pipeline.status()})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_command(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("ws connection closed with exception %s", ws.exception())
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def _handle_command(self, ws, raw):
        cmd = None
        try:
            message = json.loads(raw)
            cmd = message.get("cmd")
            args = message.get("args", {})
            result = self._dispatch(cmd, args)
            await ws.send_json({"event": "result", "data": {"cmd": cmd, "ok": True, "result": result}})
        except Exception as exc:  # noqa: BLE001 - report to the caller instead of crashing the server
            logger.exception("command failed")
            await ws.send_json({"event": "result", "data": {"cmd": cmd, "ok": False, "error": str(exc)}})

    def _dispatch(self, cmd, args):
        if cmd == "load_preview":
            self.pipeline.load_preview(args["id"])
        elif cmd == "take":
            self.pipeline.take(args.get("mode", "fade"), args.get("duration_ms", 500))
        elif cmd == "set_overlay":
            self.pipeline.set_overlay(bool(args.get("enabled")))
        elif cmd == "start_stream":
            self.pipeline.start_stream(args["url"], args.get("kind", "rtmp"), args.get("bitrate", 4000))
        elif cmd == "stop_stream":
            self.pipeline.stop_stream()
        elif cmd == "get_status":
            return self.pipeline.status()
        else:
            raise ValueError(f"unknown command: {cmd}")
        return None

    async def _broadcast(self, message):
        dead = []
        for ws in self.ws_clients:
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_json(message)
            except ConnectionResetError:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    # --- lifecycle -----------------------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        runner = web.AppRunner(self.app)
        await runner.setup()
        control_port = self.config["control"]["port"]
        preview_port = self.config["preview"]["port"]
        control_site = web.TCPSite(runner, "127.0.0.1", control_port)
        await control_site.start()
        logger.info("control server listening on ws://127.0.0.1:%s/ws", control_port)

        if preview_port != control_port:
            preview_site = web.TCPSite(runner, "127.0.0.1", preview_port)
            await preview_site.start()
        logger.info("preview server listening on http://127.0.0.1:%s/preview/*.mjpg", preview_port)

        while True:
            await asyncio.sleep(3600)
