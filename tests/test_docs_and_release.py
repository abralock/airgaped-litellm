"""Checks on operator-facing instructions that would otherwise only fail during a real rollout."""
import yaml

from conftest import ROOT

ROLLOUT = (ROOT / "docs/rollout.md").read_text()
RELEASE = (ROOT / ".github/workflows/release.yml").read_text()
RELEASE_STEPS = {s.get("name"): s for s in yaml.safe_load(RELEASE)["jobs"]["publish"]["steps"]}


def test_bedrock_cidr_is_the_destination():
    """NetworkPolicy matches the destination before NAT, so a NAT/source range would block Bedrock."""
    for env in ("test", "prod"):
        text = (ROOT / f"deploy/overlays/{env}/networkpolicies.yaml").read_text()
        assert "leaves through" not in text
        assert "destination" in text and "before any NAT" in text
    assert "leaves through" not in ROLLOUT
    assert "proxy's address" in ROLLOUT and "NO_PROXY" in ROLLOUT


def test_company_ca_keeps_public_roots():
    """SSL_CERT_FILE replaces the bundle; a company-CA-only file breaks Bedrock's public TLS."""
    text = (ROOT / "deploy/overlays/prod/proxy-ca-patch.yaml").read_text()
    assert "/etc/ssl/certs/ca-certificates.crt" in text
    assert "cat " in text


def test_harbor_import_keeps_the_release_digest():
    assert "skopeo copy --all" in ROLLOUT
    assert "RepoDigests" not in ROLLOUT
    notes = RELEASE_STEPS["Deploy bundle"]["run"]
    assert "skopeo copy --all" in notes


def test_postgres_copy_is_linked_to_the_repo():
    run = RELEASE_STEPS["Copy pinned Postgres image"]["run"]
    assert "--annotation" in run and "org.opencontainers.image.source=https://github.com/" in run


def test_release_checks_images_are_public():
    step = RELEASE_STEPS["Check images can be pulled without logging in"]
    assert "docker logout ghcr.io" in step["run"]
    assert "Change visibility" in ROLLOUT or "Change visibility" in (ROOT / "README.md").read_text()
