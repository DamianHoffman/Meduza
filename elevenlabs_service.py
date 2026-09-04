"""
Client for the Go voice-service (see voice-service/main.go), which wraps
ElevenLabs text-to-speech behind a small standalone HTTP endpoint.

Standard library only (urllib), not requests — matching app.py's choice
to keep the whole Python side dependency-free. This used to call
ElevenLabs directly from Python; it's split out as a separate Go service
now so the app is backed by two languages doing what each is good at:
Python for the app itself and the Claude integration, Go for a small
stateless HTTP service wrapping an external API. The public function
below still just returns MP3 bytes — nothing above this module needed
to change when this moved services or dropped its HTTP library.

Run the voice service separately: `cd voice-service && go run .`
"""

import json
import os
import urllib.error
import urllib.request

VOICE_SERVICE_URL = os.environ.get("VOICE_SERVICE_URL", "http://localhost:8081")
REQUEST_TIMEOUT_SECONDS = 30


def text_to_speech(text: str, voice_id: str | None = None) -> bytes:
    """Returns MP3 audio bytes for the given text, via the Go voice-service."""
    payload = {"text": text}
    if voice_id:
        payload["voice_id"] = voice_id

    request = urllib.request.Request(
        f"{VOICE_SERVICE_URL}/tts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            # The Go service's own "ELEVENLABS_API_KEY is not set" response.
            raise RuntimeError("voice-service is up but ELEVENLABS_API_KEY is not set on it") from exc
        raise  # any other non-2xx becomes a generic 500 in app.py, as before
    except urllib.error.URLError as exc:
        # Same user-facing outcome as a missing API key (voice just isn't
        # available right now) — but a distinct message, since a developer
        # reading the logs needs to know "start the Go service" is the fix,
        # not "check your ElevenLabs key".
        raise RuntimeError(
            f"Could not reach voice-service at {VOICE_SERVICE_URL} — is it "
            f"running? (cd voice-service && go run .)"
        ) from exc
