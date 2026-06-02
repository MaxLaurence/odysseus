# Vendored binaries

## dtach (0.9, GPLv2)
`dtach` is the session-persistence layer for Code Station terminals (see
src/coding_pty_bridge.py). It is bundled so the packaged app does not depend on a
Homebrew install. License: licenses/dtach-GPLv2-COPYING.txt — source:
https://github.com/crigler/dtach (GPLv2 source-availability obligation).

Current binary: macOS **arm64** only. To refresh / make universal:
    cp "$(brew --prefix)/bin/dtach" vendor/dtach && chmod +x vendor/dtach
    # universal (needs an x86_64 dtach too):
    # lipo -create dtach.arm64 dtach.x86_64 -output vendor/dtach
The runtime (coding_pty_bridge.dtach_bin) falls back to PATH/Homebrew if the
bundled binary is missing or fails (e.g. wrong arch).
