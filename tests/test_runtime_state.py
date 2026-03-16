import importlib


def test_runtime_pending_pr_persistence(tmp_path, monkeypatch):
    db = tmp_path / "runtime.db"
    monkeypatch.setenv("RUNSTATE_DB_PATH", str(db))

    import src.runtime_state as rs
    importlib.reload(rs)

    rs.set_session(42, current_run_id="run-1", current_task_id="TASK-1")
    rs.set_pending_pr(42, {"branch": "task/TASK-1", "base": "main"})
    pending = rs.get_pending_pr(42)
    assert pending and pending["branch"] == "task/TASK-1"

    rs.clear_pending_pr(42)
    assert rs.get_pending_pr(42) is None
