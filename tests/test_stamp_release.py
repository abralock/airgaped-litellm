import filecmp
import subprocess

import pytest

from conftest import ROOT, containers, render

SCRIPT = ROOT / "scripts/stamp-release.sh"
LITELLM_DIGEST = "sha256:" + "a" * 64
POSTGRES_DIGEST = "sha256:" + "b" * 64


def stamp(tag, out, litellm=LITELLM_DIGEST, postgres=POSTGRES_DIGEST):
    return subprocess.run([str(SCRIPT), tag, litellm, postgres, str(out)],
                          capture_output=True, text=True)


def images(objs):
    return {c["image"] for o in objs if o["kind"] in ("Deployment", "StatefulSet", "CronJob", "Job")
            for c in containers(o)}


def test_stamp_sets_digests_and_job_name(tmp_path):
    result = stamp("v1.110.3-r1", tmp_path)
    assert result.returncode == 0, result.stderr
    prod = render(str(tmp_path / "deploy/overlays/prod"))
    assert images(prod) == {
        f"REPLACE_ME_HARBOR/airgaped-litellm:v1.110.3-r1@{LITELLM_DIGEST}",
        f"REPLACE_ME_HARBOR/airgaped-litellm-postgres:v1.110.3-r1@{POSTGRES_DIGEST}",
    }
    migrate = render(str(tmp_path / "deploy/overlays/prod/migrate"))
    (job,) = [o for o in migrate if o["kind"] == "Job"]
    assert job["metadata"]["name"] == "litellm-migrate-v1.110.3-r1"
    assert images(migrate) == images(prod)
    assert "unreleased" not in (tmp_path / "deploy/base/kustomization.yaml").read_text()


def test_stamp_accepts_release_candidate(tmp_path):
    assert stamp("v1.110.3-rc1", tmp_path).returncode == 0


@pytest.mark.parametrize("tag", ["v1.110.4-r1", "v1.110.3", "1.110.3-r1", "v1.110.3-beta"])
def test_stamp_rejects_bad_tags(tmp_path, tag):
    result = stamp(tag, tmp_path)
    assert result.returncode == 2, (tag, result.stdout, result.stderr)
    assert not (tmp_path / "deploy").exists()


def test_stamp_rejects_bad_digest(tmp_path):
    assert stamp("v1.110.3-r1", tmp_path, litellm="latest").returncode == 2


def test_stamp_leaves_source_untouched(tmp_path):
    before = (ROOT / "deploy/base/kustomization.yaml").read_text()
    assert stamp("v1.110.3-r1", tmp_path).returncode == 0
    assert (ROOT / "deploy/base/kustomization.yaml").read_text() == before
    assert "name: litellm-migrate   #" in (ROOT / "deploy/base/migrate/job.yaml").read_text()
    assert not filecmp.cmp(ROOT / "deploy/base/kustomization.yaml",
                           tmp_path / "deploy/base/kustomization.yaml", shallow=False)
