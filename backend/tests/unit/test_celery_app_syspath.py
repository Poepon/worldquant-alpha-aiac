"""celery_app puts the repo root on sys.path (2026-05-25).

backend/ and scripts/ are namespace packages (no __init__.py). q10_tasks'
telemetry beat did `from scripts.q10_layer_telemetry_report import main` at run
time and failed with "No module named 'scripts'" because the Celery worker's
runtime sys.path didn't include the repo root. celery_app now inserts it on
import. Real-world validation is the 09:00 q10 beat no longer failing; here we
assert the mechanism + that the previously-failing module is resolvable.
"""
import importlib.util
import sys
from pathlib import Path


def test_celery_app_inserts_repo_root_on_syspath():
    import backend.celery_app as ca

    repo_root = str(Path(ca.__file__).resolve().parent.parent)
    assert repo_root in sys.path, "celery_app must put the repo root on sys.path"


def test_scripts_namespace_resolvable_for_worker_tasks():
    import backend.celery_app  # noqa: F401 — triggers the path insert on import

    # the exact module q10_tasks imports at run time must now be resolvable
    assert (
        importlib.util.find_spec("scripts.q10_layer_telemetry_report") is not None
    ), "scripts.q10_layer_telemetry_report must be importable once celery_app is loaded"
