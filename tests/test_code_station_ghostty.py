from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODE_STATION_JS = ROOT / "static" / "js" / "codeStation.js"


def test_code_station_loads_vendored_ghostty_wasm_explicitly():
    source = CODE_STATION_JS.read_text(encoding="utf-8")

    assert "/static/lib/ghostty/ghostty-vt.wasm" in source
    assert ".Ghostty.load(GHOSTTY_WASM_PATH)" in source
    assert ".GhosttyWeb.init()" not in source
