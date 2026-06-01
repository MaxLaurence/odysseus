#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MACOS_DIR="${ROOT}/macos"
APP_DIR="${MACOS_DIR}/build/Odysseus.app"
CONTENTS="${APP_DIR}/Contents"
MACOS_CONTENTS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"

cd "$ROOT"
swift build --package-path macos -c release

rm -rf "$APP_DIR"
mkdir -p "$MACOS_CONTENTS" "$RESOURCES"

cp "${MACOS_DIR}/.build/release/Odysseus" "${MACOS_CONTENTS}/Odysseus"
chmod +x "${MACOS_CONTENTS}/Odysseus"

if [ -d "${ROOT}/dist/odysseus_backend" ]; then
  cp -R "${ROOT}/dist/odysseus_backend" "${RESOURCES}/server"
else
  cat > "${RESOURCES}/README-backend.txt" <<'EOF'
No bundled backend was copied into this app.

Run macos/Scripts/build_backend.sh first to create dist/odysseus_backend,
then rerun macos/Scripts/package_app.sh. For development, run the Swift app
from the repository or set ODYSSEUS_BACKEND_ROOT to the Odysseus repo path.
EOF
fi

cat > "${CONTENTS}/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>Odysseus</string>
  <key>CFBundleIdentifier</key>
  <string>com.odysseus.desktop</string>
  <key>CFBundleName</key>
  <string>Odysseus</string>
  <key>CFBundleDisplayName</key>
  <string>Odysseus</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
    <key>NSExceptionDomains</key>
    <dict>
      <key>127.0.0.1</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key>
        <true/>
      </dict>
      <key>localhost</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key>
        <true/>
      </dict>
    </dict>
  </dict>
</dict>
</plist>
EOF

printf "APPL????" > "${CONTENTS}/PkgInfo"
codesign --force --deep --sign - "$APP_DIR" >/dev/null

echo "Packaged ${APP_DIR}"
