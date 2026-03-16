from pathlib import Path

from src.project_bootstrap import detect_language, detect_package_manager


def test_bootstrap_detects_python_and_pip(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    lang = detect_language(tmp_path)
    pm = detect_package_manager(tmp_path, lang)
    assert lang == "python"
    assert pm in ("pip", "poetry")
