# Odysseus macOS Wrapper

This is a native SwiftUI wrapper around the existing Odysseus web app. It starts
the FastAPI backend on `127.0.0.1`, opens the UI in a `WKWebView`, and keeps
mutable state under:

```text
~/Library/Application Support/Odysseus
```

## Development Run

From the repository root:

```bash
macos/Scripts/compile_and_run.sh
```

The wrapper will run the source backend with the repo's `.venv` or `venv` if
available. You can override paths:

```bash
ODYSSEUS_BACKEND_ROOT=/path/to/odysseus ODYSSEUS_PYTHON=/path/to/python swift run --package-path macos Odysseus
```

## Tailscale Sharing

Sharing defaults to local-only. In the app toolbar or Settings, switch to
`Tailnet via Tailscale Serve` to run:

```bash
tailscale serve <backend-port>
```

This keeps the backend bound to localhost and lets Tailscale proxy it to your
tailnet. The app does not enable public Funnel sharing.

## Build a Self-Contained Backend

```bash
macos/Scripts/build_backend.sh
macos/Scripts/package_app.sh
```

`build_backend.sh` creates `dist/odysseus_backend` with PyInstaller.
`package_app.sh` copies that backend into:

```text
macos/build/Odysseus.app/Contents/Resources/server
```

If `dist/odysseus_backend` does not exist, the packaged app is a development
wrapper and needs `ODYSSEUS_BACKEND_ROOT` to point at the source tree.
