from pathlib import Path


def test_cookbook_state_sync_only_sends_hf_token_when_dirty():
    repo = Path(__file__).resolve().parent.parent
    running = (repo / "static/js/cookbookRunning.js").read_text(encoding="utf-8")
    cookbook = (repo / "static/js/cookbook.js").read_text(encoding="utf-8")
    routes = (repo / "routes/cookbook_routes.py").read_text(encoding="utf-8")

    assert "if (hfToken) env.hfToken = hfToken" not in running
    assert "if (_hfTokenDirty && hfToken) env.hfToken = hfToken" in running
    assert "_envState._hfTokenDirty = false" in running
    assert "const { hfToken, _hfTokenDirty, ...safeState } = _envState" in cookbook

    assert "export HF_TOKEN=' + _shellQuote(_envState.hfToken)" not in cookbook
    assert "$env:HF_TOKEN=' + _psQuote(_envState.hfToken)" not in cookbook
    assert "export HF_TOKEN='{_bash_squote(req.hf_token)}'" not in routes
    assert "$env:HF_TOKEN = '{_ps_squote(req.hf_token)}'" not in routes
    assert "_write_bash_hf_env_file" in routes
    assert "_write_ps_hf_env_file" in routes
