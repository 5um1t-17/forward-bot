import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


def run_bot() -> None:
    from bot.main import main
    import asyncio
    asyncio.run(main())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()
