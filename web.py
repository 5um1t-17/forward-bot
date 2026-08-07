import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

_bot_thread: threading.Thread | None = None


def run_bot() -> None:
    from bot.main import main
    import asyncio

    asyncio.run(main())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # The bot main loop self-heals (reconnect loop + watchdog), so while
        # the thread is alive the process is healthy even during a brief
        # reconnect. Only when the thread has actually died do we report 503,
        # so Render / the platform restarts a zombie process. This avoids
        # needless restarts mid-transfer while still guaranteeing the process
        # is never left wedged forever.
        alive = _bot_thread is not None and _bot_thread.is_alive()
        if alive:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"bot not ready")

    def do_HEAD(self):
    self.do_GET()
 
    def log_message(self, format, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    _bot_thread = threading.Thread(target=run_bot, daemon=True)
    _bot_thread.start()
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()
