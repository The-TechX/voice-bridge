# voice-bridge

Minimal, Dockerized voice bridge for experimenting with browser voice interfaces and pluggable agent backends.

## Increment 1 — Web STT

Current flow:

```text
Microphone -> Browser Web Speech API -> transcript in the web app
```

This increment is intentionally small. It includes:

- Mobile-first web UI
- Microphone permission request
- Push-to-talk interaction
- Interim and final transcription
- Docker deployment
- Tailnet-only HTTPS access through Tailscale Serve

It does not include TTS, an agent runtime, an LLM, or an external STT provider yet.

## Run

```bash
cp .env.example .env
# Set the desired local bind address if needed.
docker compose up -d --build
```

The container listens on port `8000` internally. By default, Compose publishes the app on `127.0.0.1:8765`.

For this experiment, Tailscale Serve proxies HTTPS traffic from the node hostname to the local container endpoint.

## Browser support

The first increment uses `SpeechRecognition` / `webkitSpeechRecognition`, so Chromium-based browsers such as Chrome or Edge are the intended clients.

## Endpoints

- `GET /` — web app
- `GET /health` — service health

## Next increments

A future server-side voice pipeline can replace browser STT without changing the overall shape of the application:

```text
Browser microphone -> audio stream -> STT adapter -> agent runtime -> TTS adapter -> browser audio
```
