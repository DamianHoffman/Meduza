"""
Meduza's backend — Python standard library only, no Flask.

Routes
------
GET  /            Storefront + chat widget (the whole demo lives here)
GET  /assets/...  The logo (assets/logo.png)
POST /api/chat     {message, history} -> {reply, tool_used}   (Claude)
POST /api/tts      {text}             -> audio/mpeg bytes     (ElevenLabs, via voice-service/)

Run it
------
    cp .env.example .env        # then fill in your API keys
    python app.py               # http://localhost:5000

No pip install needed — this file and services/ use nothing beyond the
standard library, on purpose, to match voice-service/'s "Go standard
library only" story: both halves of Meduza are dependency-free.

That does mean giving up what Flask handles for you — this file does its
own routing, JSON parsing/errors, static file serving (with a path-
traversal guard), and templating (a handful of exact-string {{ var }}
replacements; there are only five and they're all developer-controlled
constants from bot_config.py, not general-purpose templating). It also
means giving up Werkzeug's dev server for Python's http.server, which
the standard library itself documents as unfit for anything beyond local
use — see README > Limitations for what that means and doesn't mean here.

Both routes work with no API keys set — they fall back to a clearly
labeled demo response instead of crashing, so you can see the UI end to
end before wiring up real keys. See README.md for details.
"""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import bot_config
from services import claude_service, elevenlabs_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meduza")

ROOT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT_DIR / "templates" / "index.html"
ASSETS_DIR = ROOT_DIR / "assets"
MAX_BODY_BYTES = 1_000_000  # generous cap so a malformed/huge request can't hang a worker thread

ASSET_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def load_dotenv(path: str = ".env") -> None:
    """
    Tiny stand-in for python-dotenv's load_dotenv(): reads KEY=VALUE lines
    from .env into the process environment, without overriding variables
    that are already set. Mirrors voice-service/main.go's loadDotEnv, so
    both services handle .env the same (minimal) way.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def html_safe_json(value) -> str:
    """
    json.dumps() plus the same extra escaping Flask's |tojson filter does,
    so this is safe to embed inside a <script> tag — e.g. a `</script>` or
    `<!--` sequence inside the value can't break out of the tag early.
    Our actual values are all developer-controlled constants, not user
    input, so this is a belt-and-braces habit here rather than a live risk.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )


def render_index() -> bytes:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{ business_name }}": bot_config.BUSINESS_NAME,
        "{{ bot_name }}": bot_config.BOT_NAME,
        "{{ greeting|tojson }}": html_safe_json(bot_config.GREETING),
        "{{ bot_name|tojson }}": html_safe_json(bot_config.BOT_NAME),
        "{{ business_name|tojson }}": html_safe_json(bot_config.BUSINESS_NAME),
    }
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return html.encode("utf-8")


def _is_valid_history(history) -> bool:
    """A well-formed history is a list of {"role": "user"|"assistant", "content": str} dicts."""
    if not isinstance(history, list):
        return False
    return all(
        isinstance(turn, dict)
        and turn.get("role") in ("user", "assistant")
        and isinstance(turn.get("content"), str)
        for turn in history
    )


class MeduzaHandler(BaseHTTPRequestHandler):
    server_version = "Meduza/1.0"
    # No default timeout on the underlying socket otherwise — a slow or
    # malicious client could hold a worker thread open indefinitely by
    # trickling bytes. This turns that into a clean socket.timeout instead.
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 (matching BaseHTTPRequestHandler's signature)
        logger.info("%s - %s", self.address_string(), format % args)

    # ---------- response helpers ----------

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- routing ----------

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler's naming convention)
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._send_bytes(200, render_index(), "text/html; charset=utf-8")
            elif path.startswith("/assets/"):
                self._serve_asset(path[len("/assets/") :])
            else:
                self._send_json(404, {"error": "not found"})
        except Exception:
            logger.exception("GET %s failed", self.path)
            self._send_json(500, {"error": "internal error"})

    def do_POST(self):  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/api/chat":
                self._handle_chat()
            elif path == "/api/tts":
                self._handle_tts()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception:
            logger.exception("POST %s failed", self.path)
            self._send_json(500, {"error": "internal error"})

    def _serve_asset(self, relative_path: str) -> None:
        # Resolve and confirm the result is still inside ASSETS_DIR, so a
        # path like /assets/../app.py can't escape the assets/ folder.
        candidate = (ASSETS_DIR / relative_path).resolve()
        if ASSETS_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_type = ASSET_MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self._send_bytes(200, candidate.read_bytes(), content_type)

    # ---------- API handlers ----------

    def _handle_chat(self) -> None:
        data = self._read_json_body()
        message = (data.get("message") or "").strip()
        history = data.get("history") or []

        if not message:
            self._send_json(400, {"error": "message is required"})
            return
        if len(message) > 2000:
            self._send_json(400, {"error": "message is too long"})
            return
        if not _is_valid_history(history):
            self._send_json(400, {"error": "history must be a list of {role, content} turns"})
            return

        try:
            result = claude_service.get_reply(message, history)
            self._send_json(200, result)
        except RuntimeError:
            # No ANTHROPIC_API_KEY configured yet — keep the UI usable anyway.
            self._send_json(
                200,
                {
                    "reply": (
                        f"I'm in demo mode right now — no ANTHROPIC_API_KEY is set, "
                        f"so I can't call Claude for real yet. Add a key to your .env "
                        f"file to see {bot_config.BOT_NAME} answer for real (see README.md)."
                    ),
                    "tool_used": None,
                    "demo_mode": True,
                },
            )
        except Exception:
            logger.exception("chat handling failed")
            self._send_json(500, {"error": "Something went wrong generating a reply."})

    def _handle_tts(self) -> None:
        data = self._read_json_body()
        text = (data.get("text") or "").strip()

        if not text:
            self._send_json(400, {"error": "text is required"})
            return
        if len(text) > 2000:
            self._send_json(400, {"error": "text is too long"})
            return

        try:
            audio_bytes = elevenlabs_service.text_to_speech(text)
            self._send_bytes(200, audio_bytes, "audio/mpeg")
        except RuntimeError:
            # voice-service isn't reachable, or is up but has no ElevenLabs
            # key — either way, tell the frontend so it can fail quietly
            # instead of showing a scary error.
            self._send_json(503, {"error": "voice_not_configured"})
        except Exception:
            logger.exception("tts handling failed")
            self._send_json(500, {"error": "Could not generate audio."})


def main() -> None:
    load_dotenv()
    port = int(os.environ.get("PORT", 5000))
    server = ThreadingHTTPServer(("0.0.0.0", port), MeduzaHandler)
    logger.info("Meduza listening on :%d", port)
    logger.info(
        "This is Python's stdlib http.server (ThreadingHTTPServer) — fine "
        "for local dev, not hardened for production. See README > Limitations."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
