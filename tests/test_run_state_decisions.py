import importlib


def test_append_and_list_decisions(tmp_path, monkeypatch):
    db = tmp_path / "agent.db"
    monkeypatch.setenv("RUNSTATE_DB_PATH", str(db))

    import src.run_state as rs
    importlib.reload(rs)

    run_id = rs.create_run_state(source_message="test", task_id="TASK-1")
    ok = rs.append_decision(
        run_id,
        phase="coding",
        decision_type="circuit_breaker_triggered",
        actor="supervisor",
        details={"reason": "repeat"},
        cost_estimate=1.5,
        risk_level="high",
    )
    assert ok is True

    decisions = rs.list_run_decisions(run_id)
    assert len(decisions) >= 1
    latest = decisions[0]
    assert latest["decision_type"] == "circuit_breaker_triggered"
    assert latest["actor"] == "supervisor"
    assert latest["risk_level"] == "high"
