import importlib


def test_budget_persistence(tmp_path, monkeypatch):
    db = tmp_path / "prefs.db"
    monkeypatch.setenv("SQLITE_DB_PATH", str(db))

    import src.chat_preferences as cp
    importlib.reload(cp)

    cp.set_budget(123, 40, 120)
    pref = cp.get_preference(123)
    assert pref["max_llm_calls"] == 40
    assert pref["max_tool_calls"] == 120

    cp.set_budget(123, None, None)
    pref2 = cp.get_preference(123)
    assert pref2["max_llm_calls"] is None
    assert pref2["max_tool_calls"] is None
