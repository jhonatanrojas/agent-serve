from __future__ import annotations
import subprocess
from pathlib import Path

from src.repomap import refresh_repo_map


def _run(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        out = subprocess.check_output(cmd, cwd=str(cwd), stderr=subprocess.STDOUT, text=True, timeout=240)
        return True, out[:2000]
    except Exception as e:
        return False, str(e)[:2000]


def detect_language(repo_path: Path) -> str:
    if (repo_path / "pyproject.toml").exists() or (repo_path / "requirements.txt").exists():
        return "python"
    if (repo_path / "package.json").exists():
        return "javascript"
    if (repo_path / "Cargo.toml").exists():
        return "rust"
    return "unknown"


def detect_package_manager(repo_path: Path, language: str) -> str:
    if language == "python":
        if (repo_path / "poetry.lock").exists():
            return "poetry"
        return "pip"
    if language == "javascript":
        if (repo_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (repo_path / "yarn.lock").exists():
            return "yarn"
        return "npm"
    if language == "rust":
        return "cargo"
    return "none"


def bootstrap_project(repo_path: str) -> dict:
    repo = Path(repo_path).resolve()
    language = detect_language(repo)
    pm = detect_package_manager(repo, language)

    install_ok, install_out = True, "no-op"
    test_ok, test_out = True, "no tests executed"

    if language == "python":
        if pm == "poetry":
            install_ok, install_out = _run(["poetry", "install"], repo)
            test_ok, test_out = _run(["poetry", "run", "pytest", "-q"], repo)
        else:
            req = repo / "requirements.txt"
            if req.exists():
                install_ok, install_out = _run(["python", "-m", "pip", "install", "-r", "requirements.txt"], repo)
            test_ok, test_out = _run(["pytest", "-q"], repo)
    elif language == "javascript":
        if pm == "pnpm":
            install_ok, install_out = _run(["pnpm", "install"], repo)
            test_ok, test_out = _run(["pnpm", "test", "--", "--runInBand"], repo)
        elif pm == "yarn":
            install_ok, install_out = _run(["yarn", "install"], repo)
            test_ok, test_out = _run(["yarn", "test"], repo)
        else:
            install_ok, install_out = _run(["npm", "install"], repo)
            test_ok, test_out = _run(["npm", "test", "--", "--runInBand"], repo)
    elif language == "rust":
        install_ok, install_out = _run(["cargo", "fetch"], repo)
        test_ok, test_out = _run(["cargo", "test", "-q"], repo)

    repo_map = refresh_repo_map(repo_path=repo)
    return {
        "language": language,
        "package_manager": pm,
        "install_ok": install_ok,
        "install_output": install_out,
        "tests_ok": test_ok,
        "tests_output": test_out,
        "modules": len(repo_map.get("modules", [])),
        "dependencies": len(repo_map.get("dependencies", [])),
    }
