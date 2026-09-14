"""CI Python selection, shell startup, and writable task environment recovery."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_ci.executor import DockerExecutor
from prepare import container_fs
from prepare.python_environment import ci_python


def test_prepared_interpreter_selection_never_falls_back_to_system_python():
    assert ci_python() == "/opt/venv/bin/python"
    assert ci_python(environment={"PYTHON_VENV_ACTIVATE": "/opt/ci/bin/activate"}) == "/opt/ci/bin/python"
    assert ci_python(environment={"SEED_PYTHON": "/opt/seed/bin/python"}) == "/opt/seed/bin/python"
    for path in ("python3", "/usr/bin/python3", "/usr/local/bin/python", "/task/candidate/venv/bin/python"):
        with pytest.raises(ValueError, match="prepared CI virtual environment"):
            ci_python({"container_python": path})


def test_executor_selects_task_venv_and_prepared_management_python(tmp_path):
    executor = DockerExecutor(
        {"runtime": {"kind": "docker-rootless", "endpoint": "unix:///run/user/1001/docker.sock"},
         "codex_bin": "/usr/local/bin/codex", "profiles": {"ci": {}}},
        tmp_path,
        {"artifacts_host": str(tmp_path / "artifacts"), "execution_uid": 11001,
         "execution_gid": 11001, "container_id": "fixture", "profile_branch": "ci",
         "profile": "ci", "environment_fingerprint": "fixture", "backend_enabled": False,
         "env": {"PYTHONHOME": "/usr", "SEED_PYTHON": "/opt/ci/bin/python"}},
        {"task_id": "fixture", "tested_sha": "a" * 40, "base_sha": "b" * 40, "llvm_hash": "c" * 40},
        None, manager=None,
    )
    for variant in ("base", "candidate"):
        env = executor.environment(variant)
        assert env["PYTHON_BIN"] == f"/task/{variant}/venv/bin/python"
        assert env["VIRTUAL_ENV"] == f"/task/{variant}/venv"
        assert env["PATH"].startswith(env["VIRTUAL_ENV"] + "/bin:")
        assert "PYTHONHOME" not in env
        assert executor.tool_context(variant)["trusted_python_bin"] == "/opt/ci/bin/python"
    assert "/opt/ci/bin/python" in executor.codex_command([])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux container shell behavior")
def test_login_shell_uses_writable_ci_venv_for_generated_tests_and_repairs(tmp_path):
    seed = tmp_path / "ci-seed"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(seed)], check=True)
    seed_python = str(seed / "bin/python")
    site = Path(subprocess.check_output([
        seed_python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"
    ], text=True).strip())
    (site / "ci_dependency.py").write_text("VERSION = 'seed-version'\n")
    command = seed / "bin/ci-check"
    command.write_text(
        f"#!{seed_python}\nimport json,sys; print(json.dumps([sys.executable, sys.prefix]))\n"
    )
    command.chmod(0o755)
    root = tmp_path / "candidate"
    root.mkdir()
    container_fs.seed_venv(root, {"SEED_PYTHON": seed_python})
    python = root / "venv/bin/python"
    test_file = root / "generated_test.py"
    test_file.write_text(
        "import ci_dependency, unittest\n"
        "class GeneratedTest(unittest.TestCase):\n"
        "    def test_dependency(self):\n"
        "        self.assertEqual(ci_dependency.VERSION, 'repaired')\n"
        "unittest.main()\n"
    )
    script = Path(__file__).resolve().parents[1] / "tools/basic_tools/ci_python_env.sh"
    env = {**os.environ, "BASH_ENV": str(script), "PYTHON_BIN": str(python), "PATH": "/usr/bin:/bin"}
    identity = json.loads(subprocess.check_output([
        "bash", "-lc", "python -c 'import json,sys; print(json.dumps([sys.executable, sys.prefix]))'"
    ], env=env, text=True))
    assert identity == [str(python), str(root / "venv")]
    console_identity = json.loads(subprocess.check_output(
        ["bash", "-lc", "ci-check"], env=env, text=True,
    ))
    assert console_identity == identity
    initial = subprocess.run([str(python), str(test_file)], capture_output=True)
    assert initial.returncode != 0
    assert b"FAILED (failures=1)" in initial.stderr
    task_site = Path(subprocess.check_output([
        str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"
    ], text=True).strip())
    (task_site / "ci_dependency.py").write_text("VERSION = 'repaired'\n")
    rerun = subprocess.run([str(python), str(test_file)], capture_output=True, text=True)
    assert rerun.returncode == 0, rerun.stderr
    assert "Ran 1 test" in rerun.stderr
    assert (site / "ci_dependency.py").read_text() == "VERSION = 'seed-version'\n"


def test_management_helper_imports_under_isolated_python():
    # No task operation is requested; exercise import of its trusted sibling.
    result = subprocess.run([
        sys.executable, "-I", "-S", "-B", "-c",
        "import runpy,sys; helper=runpy.run_path(sys.argv[1]); print(helper['ci_python']())",
        str(Path(container_fs.__file__)),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/opt/venv/bin/python"
