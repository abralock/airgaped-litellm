import pathlib
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def render(path: str) -> list[dict]:
    """Render a kustomization (relative to the repo root) into a list of objects."""
    out = subprocess.run(["kubectl", "kustomize", str(ROOT / path)],
                         check=True, capture_output=True, text=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def find(objs, kind, name=None):
    hits = [o for o in objs if o["kind"] == kind and (name is None or o["metadata"]["name"] == name)]
    assert hits, f"no {kind} {name or ''} rendered"
    return hits[0] if name else hits


def containers(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = spec["template"]["spec"]
    return pod.get("initContainers", []) + pod["containers"]


@pytest.fixture(scope="session")
def base():
    return render("deploy/base")


@pytest.fixture(scope="session")
def base_migrate():
    return render("deploy/base/migrate")
