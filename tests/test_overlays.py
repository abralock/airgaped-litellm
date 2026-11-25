import os
import re

import pytest

from conftest import ROOT, containers, find, render

ENVS = {"test": "litellm-test", "prod": "litellm-prod"}
CLUSTER_SCOPED = {"Namespace"}


@pytest.fixture(scope="module", params=sorted(ENVS))
def overlay(request):
    return request.param, render(f"deploy/overlays/{request.param}")


def test_overlay_namespace(overlay):
    env, objs = overlay
    for o in objs:
        if o["kind"] not in CLUSTER_SCOPED:
            assert o["metadata"].get("namespace") == ENVS[env], f"{o['kind']}/{o['metadata']['name']}"


def test_overlay_images_point_at_harbor(overlay):
    _, objs = overlay
    images = {c["image"] for o in objs if o["kind"] in ("Deployment", "StatefulSet", "CronJob")
              for c in containers(o)}
    assert images == {"REPLACE_ME_HARBOR/airgaped-litellm:unreleased",
                      "REPLACE_ME_HARBOR/airgaped-litellm-postgres:unreleased"}


def test_overlay_ingress(overlay):
    _, objs = overlay
    ing = find(objs, "Ingress", "litellm")
    assert ing["spec"]["ingressClassName"] == "REPLACE_ME_INGRESS_CLASS"
    assert ing["spec"]["rules"][0]["host"] == "REPLACE_ME_HOSTNAME"
    assert ing["spec"]["tls"] == [{"hosts": ["REPLACE_ME_HOSTNAME"], "secretName": "REPLACE_ME_TLS_SECRET"}]


def test_overlay_network_policies(overlay):
    _, objs = overlay
    eg = find(objs, "NetworkPolicy", "litellm-egress-external")
    assert eg["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "litellm"
    targets = {(r["to"][0]["ipBlock"]["cidr"], r["ports"][0]["port"]) for r in eg["spec"]["egress"]}
    assert targets == {("REPLACE_ME_BEDROCK_EGRESS_CIDR", 443), ("REPLACE_ME_CORP_LITELLM_CIDR", 443)}
    ing = find(objs, "NetworkPolicy", "litellm-ingress")
    assert ing["spec"]["policyTypes"] == ["Ingress"]
    assert ing["spec"]["ingress"][0]["ports"] == [{"port": 4000, "protocol": "TCP"}]
    selector = ing["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]
    assert selector == {"kubernetes.io/metadata.name": "REPLACE_ME_INGRESS_NAMESPACE"}
    find(objs, "NetworkPolicy", "litellm-egress-internal")
    find(objs, "NetworkPolicy", "litellm-postgres-ingress")


def test_overlay_has_no_secrets(overlay):
    _, objs = overlay
    assert not [o for o in objs if o["kind"] == "Secret"]


@pytest.mark.parametrize("env", sorted(ENVS))
def test_overlay_migrate(env):
    objs = render(f"deploy/overlays/{env}/migrate")
    assert sorted(o["kind"] for o in objs) == ["Job", "NetworkPolicy", "Service", "StatefulSet"]
    assert {o["metadata"]["namespace"] for o in objs} == {ENVS[env]}
    images = {c["image"] for o in objs if o["kind"] in ("Job", "StatefulSet") for c in containers(o)}
    assert images == {"REPLACE_ME_HARBOR/airgaped-litellm:unreleased",
                      "REPLACE_ME_HARBOR/airgaped-litellm-postgres:unreleased"}


def test_test_overlay_is_smaller():
    test = find(render("deploy/overlays/test"), "Deployment", "litellm")
    prod = find(render("deploy/overlays/prod"), "Deployment", "litellm")
    assert test["spec"]["replicas"] == 1
    assert prod["spec"]["replicas"] == 2
    (tc,) = containers(test)
    assert tc["resources"]["limits"] == {"cpu": "1", "memory": "1Gi"}
    assert tc["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}


def test_proxy_ca_example_applies(tmp_path):
    """The optional proxy / company-CA patch must apply cleanly when an operator enables it."""
    patch = ROOT / "deploy/overlays/prod/proxy-ca-patch.yaml"
    (tmp_path / "proxy-ca-patch.yaml").write_text(patch.read_text())
    (tmp_path / "kustomization.yaml").write_text(
        "resources:\n"
        f"  - {os.path.relpath(ROOT / 'deploy/overlays/prod', tmp_path)}\n"
        "patches:\n"
        "  - path: proxy-ca-patch.yaml\n"
    )
    dep = find(render(str(tmp_path)), "Deployment", "litellm")
    (c,) = containers(dep)
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["HTTPS_PROXY"] == "REPLACE_ME_HTTPS_PROXY"
    for host in ("litellm-postgres", ".svc", ".cluster.local"):
        assert host in env["NO_PROXY"]
    assert env["SSL_CERT_FILE"] == env["REQUESTS_CA_BUNDLE"] == "/etc/company-ca/ca.crt"
    assert env["DISABLE_SCHEMA_UPDATE"] == "true"  # patch must not drop base env
    vol = next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "company-ca")
    assert vol["configMap"]["name"] == "company-ca"
    assert any(m["mountPath"] == "/etc/company-ca" for m in c["volumeMounts"])


PLACEHOLDER = re.compile(r"REPLACE_ME_[A-Z_]+")


def test_placeholders_listed():
    rollout = ROOT / "docs/rollout.md"
    found = {m for p in (ROOT / "deploy").rglob("*.yaml") for m in PLACEHOLDER.findall(p.read_text())}
    documented = set(PLACEHOLDER.findall(rollout.read_text()))
    assert found <= documented, f"undocumented placeholders: {sorted(found - documented)}"
