import ast
import json
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
GUARD_PATH = BACKEND_DIR / "scripts" / "check_python_version.py"
ENTRYPOINT_PATH = BACKEND_DIR / "scripts" / "guarded_entrypoint.py"
BOOTSTRAP_PATH = BACKEND_DIR / "scripts" / "bootstrap_backend.py"
PYPROJECT_PATH = BACKEND_DIR / "pyproject.toml"
UNSUPPORTED_PYTHON_VERSION = "UNSUPPORTED_PYTHON_VERSION"
REQUIRED_PYTHON_RANGE = ">=3.11,<3.13"


def _run_entrypoint(*args, env=None):
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_guard(*args):
    return subprocess.run(
        [sys.executable, "-c", _GUARD_RUNNER.format(guard=str(GUARD_PATH)), *args],
        capture_output=True,
        text=True,
    )


_GUARD_RUNNER = (
    "import runpy, sys;"
    "sys.argv = ['check_python_version', *sys.argv[1:]];"
    "runpy.run_path({guard!r}, run_name='__main__')"
)


def _load_guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_python_version", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e0_py_red_contract():
    missing = []
    if not GUARD_PATH.exists():
        missing.append("scripts/check_python_version.py")
    if not PYPROJECT_PATH.exists():
        missing.append("pyproject.toml")
    if missing:
        pytest.fail("RED_E0_PY: Python floor contract missing: " + ", ".join(missing))
    metadata = tomllib.loads(PYPROJECT_PATH.read_text())
    if metadata.get("project", {}).get("requires-python") != REQUIRED_PYTHON_RANGE:
        pytest.fail(
            f"RED_E0_PY: pyproject.toml must declare requires-python = "
            f"{REQUIRED_PYTHON_RANGE!r}"
        )
    proc = _run_guard()
    if proc.returncode != 0:
        pytest.fail(f"RED_E0_PY: guard failed under supported Python: {proc.stderr}")


def test_e0_py_entrypoint_red_contract():
    if not ENTRYPOINT_PATH.exists():
        pytest.fail("RED_E0_PY_ENTRYPOINT: guarded launcher is missing")

    proc = _run_entrypoint(sys.executable, "-c", "print('guarded-target')")
    if proc.returncode != 0 or proc.stdout.strip() != "guarded-target":
        pytest.fail(
            "RED_E0_PY_ENTRYPOINT: guarded launcher did not exec the target: "
            + (proc.stderr or proc.stdout)
        )


def test_e0_py_bootstrap_red_contract():
    if not BOOTSTRAP_PATH.exists():
        pytest.fail("RED_E0_PY_BOOTSTRAP: guarded dependency bootstrap is missing")


def test_entrypoint_rejects_python_3_10_before_target_side_effect(tmp_path):
    marker = tmp_path / "target-ran"
    runner = (
        "import runpy, sys;"
        "sys.version_info = (3, 10, 20, 'final', 0);"
        f"sys.path.insert(0, {str(ENTRYPOINT_PATH.parent)!r});"
        f"sys.argv = ['guarded_entrypoint.py', {sys.executable!r}, '-c', "
        f"\"from pathlib import Path; Path({str(marker)!r}).write_text('ran')\"];"
        f"runpy.run_path({str(ENTRYPOINT_PATH)!r}, run_name='__main__')"
    )
    proc = subprocess.run([sys.executable, "-c", runner], capture_output=True, text=True)

    assert proc.returncode != 0
    assert UNSUPPORTED_PYTHON_VERSION in proc.stderr + proc.stdout
    assert not marker.exists()


def test_entrypoint_preserves_argv_environment_exit_and_process(tmp_path):
    target = tmp_path / "target.py"
    target.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv, 'env': os.environ['E0_PY_ENV'], "
        "'pid': os.getpid()}))\n"
        "raise SystemExit(23)\n"
    )
    env = dict(os.environ, E0_PY_ENV="preserved")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ENTRYPOINT_PATH),
            "--",
            sys.executable,
            str(target),
            "literal;not-a-shell",
            "$(not-expanded)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    launcher_pid = proc.pid
    stdout, stderr = proc.communicate()

    assert proc.returncode == 23, stderr
    result = json.loads(stdout)
    assert result == {
        "argv": [str(target), "literal;not-a-shell", "$(not-expanded)"],
        "env": "preserved",
        "pid": launcher_pid,
    }


def test_entrypoint_empty_command_has_stable_safe_usage():
    secret = "must-not-leak"
    proc = _run_entrypoint(env=dict(os.environ, E0_PY_SECRET=secret))

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr == "GUARDED_ENTRYPOINT_USAGE: command required\n"
    assert secret not in proc.stderr


def test_entrypoint_imports_stdlib_only():
    tree = ast.parse(ENTRYPOINT_PATH.read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module.split(".")[0])
    unknown = sorted(set(imported) - set(sys.stdlib_module_names) - {"check_python_version"})
    assert not unknown, f"entrypoint imports non-stdlib modules: {unknown}"


def test_bootstrap_guards_before_exec_and_uses_absolute_requirements(monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(BOOTSTRAP_PATH.parent))
    spec = importlib.util.spec_from_file_location("bootstrap_backend", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "require_supported_python", lambda: calls.append("guard"))

    def capture_exec(file, argv, env):
        calls.append((file, argv, env))

    monkeypatch.setattr(module.os, "execvpe", capture_exec)
    module.main()

    assert calls[0] == "guard"
    file, argv, env = calls[1]
    assert file == sys.executable
    assert argv == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        str(BACKEND_DIR / "requirements.txt"),
    ]
    assert env is os.environ


def test_bootstrap_imports_stdlib_only():
    tree = ast.parse(BOOTSTRAP_PATH.read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module.split(".")[0])
    unknown = sorted(set(imported) - set(sys.stdlib_module_names) - {"check_python_version"})
    assert not unknown, f"bootstrap imports non-stdlib modules: {unknown}"


def test_require_supported_python_accepts_current_runtime():
    assert _load_guard().require_supported_python() is True


def test_require_supported_python_rejects_versions_outside_supported_range():
    for major_minor in [(3, 10), (3, 9), (3, 8), (3, 13)]:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import runpy, sys;"
                f"sys.version_info = {major_minor + (0, 'final', 0)};"
                f"runpy.run_path({str(GUARD_PATH)!r}, run_name='__main__')",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        message = proc.stderr + proc.stdout
        assert UNSUPPORTED_PYTHON_VERSION in message
        detected = ".".join(str(part) for part in major_minor)
        assert f"detected Python {detected}" in message
        assert "required >= 3.11, < 3.13" in message


def test_guard_runs_without_site_packages():
    proc = subprocess.run([sys.executable, "-S", str(GUARD_PATH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_guard_imports_stdlib_only():
    tree = ast.parse(GUARD_PATH.read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module.split(".")[0])
    assert imported, "guard must import at least one standard-library module"
    unknown = sorted(set(imported) - set(sys.stdlib_module_names))
    assert not unknown, f"guard imports non-stdlib modules: {unknown}"


def test_pyproject_declares_python_floor():
    metadata = tomllib.loads(PYPROJECT_PATH.read_text())
    assert metadata["project"]["requires-python"] == REQUIRED_PYTHON_RANGE


def test_packaging_build_evaluates_and_emits_python_floor(tmp_path):
    report_path = tmp_path / "pip-report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--no-deps",
            "--report",
            str(report_path),
            ".",
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "RED_E0_PY: packaging build is broken (setuptools flat-layout "
            "discovers app/uploads/alembic):\n" + (proc.stderr or proc.stdout)
        )
    installed = json.loads(report_path.read_text())["install"]
    assert len(installed) == 1
    metadata = installed[0]["metadata"]
    assert metadata["name"] == "ontexus-backend"
    assert set(metadata["requires_python"].split(",")) == set(REQUIRED_PYTHON_RANGE.split(","))
