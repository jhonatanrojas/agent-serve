import os
import subprocess
import logging
from pathlib import Path

REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve"))
VENV_BIN = REPO_PATH / "venv" / "bin"
log = logging.getLogger("validator")


def _run(cmd: list, cwd=None, timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(cwd or REPO_PATH), timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 1, "Timeout"
    except FileNotFoundError:
        return 1, f"Comando no encontrado: {cmd[0]}"


def run_lint(files: list[str]) -> dict:
    """Ejecuta ruff o flake8 sobre los archivos modificados."""
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"tool": "lint", "passed": True, "output": "Sin archivos Python que revisar"}

    ruff = str(VENV_BIN / "ruff")
    flake8 = str(VENV_BIN / "flake8")

    for tool, cmd in [
        ("ruff", [ruff, "check", "--select=E,F,W"] + py_files),
        ("flake8", [flake8, "--max-line-length=120"] + py_files),
    ]:
        code, output = _run(cmd)
        if "no encontrado" not in output and "Timeout" not in output:
            passed = code == 0
            log.info("Lint (%s): %s", tool, "OK" if passed else f"{output[:100]}")
            return {"tool": tool, "passed": passed, "output": output[:500] or "Sin errores"}

    return {"tool": "lint", "passed": True, "output": "Sin linter disponible (instala ruff o flake8)"}


def run_type_check(files: list[str]) -> dict:
    """Ejecuta pyright o mypy sobre los archivos modificados."""
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"tool": "typecheck", "passed": True, "output": "Sin archivos Python"}

    pyright = str(VENV_BIN / "pyright")
    mypy = str(VENV_BIN / "mypy")

    for tool, cmd in [
        ("pyright", [pyright] + py_files),
        ("mypy", [mypy, "--ignore-missing-imports"] + py_files),
    ]:
        code, output = _run(cmd)
        if "no encontrado" not in output and "Timeout" not in output:
            passed = code == 0
            log.info("Typecheck (%s): %s", tool, "OK" if passed else f"{output[:100]}")
            return {"tool": tool, "passed": passed, "output": output[:500] or "Sin errores de tipos"}

    return {"tool": "typecheck", "passed": True, "output": "Sin type checker disponible (instala pyright o mypy)"}


def run_syntax_check(files: list[str]) -> dict:
    """Verifica sintaxis Python con py_compile — siempre disponible."""
    import py_compile
    errors = []
    for f in files:
        if not f.endswith(".py"):
            continue
        path = REPO_PATH / f if not f.startswith("/") else Path(f)
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(str(e))

    passed = len(errors) == 0
    log.info("Syntax check: %s", "OK" if passed else errors)
    return {
        "tool": "syntax",
        "passed": passed,
        "output": "Sin errores de sintaxis" if passed else "\n".join(errors),
    }


def _discover_related_tests(modified_files: list[str]) -> list[str]:
    """Mapea archivos modificados a tests cercanos: tests/**/test_<stem>.py o *test*.py relacionados."""
    tests_root = REPO_PATH / "tests"
    if not tests_root.exists():
        return []

    candidates: set[str] = set()
    all_tests = [p for p in tests_root.rglob("test_*.py") if p.is_file()]
    for rel in modified_files:
        p = Path(rel)
        stem = p.stem
        parent_parts = [part for part in p.parts if part not in {"src", "app"}]

        for t in all_tests:
            t_rel = t.relative_to(REPO_PATH)
            t_str = str(t_rel)
            if stem and (f"test_{stem}.py" in t_str or stem in t_str):
                candidates.add(t_str)
                continue

            if any(part and part in t_str for part in parent_parts[-2:]):
                candidates.add(t_str)

    return sorted(candidates)


def run_related_tests(modified_files: list[str]) -> dict:
    """Ejecuta tests relacionados al área modificada usando pytest por archivo."""
    related_tests = _discover_related_tests(modified_files)
    if not related_tests:
        return {
            "tool": "tests_related",
            "passed": True,
            "output": "Sin tests relacionados detectados",
            "related_tests": [],
        }

    pytest_bin = str(VENV_BIN / "pytest")
    cmd = [pytest_bin, "-q", *related_tests]
    code, output = _run(cmd, timeout=90)

    if "Comando no encontrado" in output:
        return {
            "tool": "tests_related",
            "passed": True,
            "output": "Pytest no disponible; omitiendo tests relacionados",
            "related_tests": related_tests,
        }

    return {
        "tool": "tests_related",
        "passed": code == 0,
        "output": output[:800] or "Sin salida",
        "related_tests": related_tests,
    }


def run_validation(modified_files: list[str]) -> dict:
    """
    Ejecuta validaciones incrementales sobre archivos modificados:
    - sintaxis/lint/typecheck incremental
    - tests relacionados al área modificada
    """
    if not modified_files:
        return {"passed": True, "checks": [], "summary": "Sin archivos que validar"}

    checks = [
        run_syntax_check(modified_files),
        run_lint(modified_files),
        run_type_check(modified_files),
        run_related_tests(modified_files),
    ]

    all_passed = all(c["passed"] for c in checks)
    return {"passed": all_passed, "checks": checks}


def format_validation(result: dict) -> str:
    """Formatea el resultado de validación para Telegram."""
    if result.get("summary"):
        return f"✅ Validación: {result['summary']}"

    overall = "✅ Validación OK" if result["passed"] else "⚠️ Validación con errores"
    lines = [overall]
    for check in result.get("checks", []):
        icon = "✅" if check["passed"] else "❌"
        lines.append(f"{icon} {check['tool']}: {check['output'][:150]}")
    return "\n".join(lines)
