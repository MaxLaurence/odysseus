from src.model_discovery import DEFAULT_MODEL_PORTS, parse_model_ports


def test_parse_model_ports_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_PORTS", raising=False)

    assert parse_model_ports() == list(DEFAULT_MODEL_PORTS)


def test_parse_model_ports_accepts_ports_ranges_and_dedupes():
    assert parse_model_ports("1337, 8000-8002, 1337,11434") == [
        1337,
        8000,
        8001,
        8002,
        11434,
    ]


def test_parse_model_ports_ignores_invalid_entries():
    assert parse_model_ports("bad,0,65536,9000-8999,8000") == [8000]


def test_parse_model_ports_falls_back_when_all_entries_invalid():
    assert parse_model_ports("bad,0,65536") == list(DEFAULT_MODEL_PORTS)
