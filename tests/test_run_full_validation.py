import importlib.util
from pathlib import Path


def test_run_full_validation_imports_repo_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_full_validation.py"
    spec = importlib.util.spec_from_file_location("run_full_validation", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert hasattr(module, "main")


def test_run_v4_export_imports_repo_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_v4_export.py"
    spec = importlib.util.spec_from_file_location("run_v4_export", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert hasattr(module, "main")


def test_run_v6_export_imports_repo_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_v6_export.py"
    spec = importlib.util.spec_from_file_location("run_v6_export", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert hasattr(module, "main")
