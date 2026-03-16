from src.planner import normalize_spec


def test_normalize_spec_supports_hierarchical_subtasks():
    spec = {
        "title": "x",
        "subtasks": [
            {"phase": "analysis", "tasks": ["leer módulos", "mapear riesgos"]},
            "implementar fix crítico",
            {"name": "coding", "subtasks": ["editar archivo A", "editar archivo B"]},
        ],
    }
    out = normalize_spec(spec)
    assert "subtasks_hierarchical" in out
    assert any(s.startswith("[analysis]") for s in out["subtasks"])
    assert "implementar fix crítico" in out["subtasks"]
    assert any(s.startswith("[coding]") for s in out["subtasks"])
