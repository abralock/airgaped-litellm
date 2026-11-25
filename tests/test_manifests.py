from conftest import containers, find


def env_of(container):
    return {e["name"]: e for e in container.get("env", [])}


def test_litellm_deployment(base):
    dep = find(base, "Deployment", "litellm")
    assert dep["spec"]["replicas"] == 2
    pod = dep["spec"]["template"]["spec"]
    (c,) = pod["containers"]
    assert env_of(c)["DISABLE_SCHEMA_UPDATE"]["value"] == "true"
    assert {"secretRef": {"name": "litellm-secrets"}} in c["envFrom"]
    assert c["livenessProbe"]["httpGet"] == {"path": "/health/liveliness", "port": 4000}
    assert c["readinessProbe"]["httpGet"] == {"path": "/health/readiness", "port": 4000}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["requests"] and c["resources"]["limits"]
    assert c["args"][:2] == ["--config", "/app/config.yaml"]
    mount = next(m for m in c["volumeMounts"] if m["mountPath"] == "/app/config.yaml")
    vol = next(v for v in pod["volumes"] if v["name"] == mount["name"])
    assert vol["configMap"]["name"].startswith("litellm-config")
    assert pod["serviceAccountName"] == "litellm"


def test_config_map_holds_gateway_config(base):
    cm = next(o for o in base if o["kind"] == "ConfigMap" and o["metadata"]["name"].startswith("litellm-config"))
    assert "bedrock/claude-sonnet" in cm["data"]["config.yaml"]


def test_migrate_job(base_migrate):
    # The database step also creates Postgres, so a first install needs no special order.
    kinds = sorted(o["kind"] for o in base_migrate)
    assert kinds == ["Job", "NetworkPolicy", "Service", "StatefulSet"]
    job = find(base_migrate, "Job", "litellm-migrate")
    pod = job["spec"]["template"]["spec"]
    (wait,) = pod["initContainers"]
    assert wait["image"].startswith("ghcr.io/abralock/airgaped-litellm:")
    assert "litellm-postgres" in " ".join(wait["command"] + wait.get("args", []))
    (c,) = pod["containers"]
    assert c["args"] == ["--skip_server_startup"]
    assert "DISABLE_SCHEMA_UPDATE" not in env_of(c)
    assert {"secretRef": {"name": "litellm-secrets"}} in c["envFrom"]
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["ttlSecondsAfterFinished"] == 86400
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == "litellm-migrate"


def test_postgres(base):
    sts = find(base, "StatefulSet", "litellm-postgres")
    assert sts["spec"]["replicas"] == 1
    (c,) = containers(sts)
    env = env_of(c)
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "litellm-postgres", "key": "POSTGRES_PASSWORD"}
    assert env["POSTGRES_USER"]["value"] == "litellm"
    assert env["POSTGRES_DB"]["value"] == "litellm"
    assert sts["spec"]["volumeClaimTemplates"]
    find(base, "Service", "litellm-postgres")


def test_backup_cronjob(base):
    cj = find(base, "CronJob", "litellm-backup")
    assert cj["spec"]["schedule"] == "0 2 * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    (c,) = containers(cj)
    script = " ".join(c["command"] + c.get("args", []))
    assert "pg_dump -Fc" in script
    assert "-mtime +6 -delete" in script
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "litellm-backups" for v in pod["volumes"])
    find(base, "PersistentVolumeClaim", "litellm-backups")


def test_no_secrets_committed(base, base_migrate):
    assert not [o for o in base + base_migrate if o["kind"] == "Secret"]


def test_images(base, base_migrate):
    workloads = [o for o in base + base_migrate if o["kind"] in ("Deployment", "StatefulSet", "CronJob", "Job")]
    images = [c["image"] for o in workloads for c in containers(o)]
    assert images
    for image in images:
        assert image.startswith("ghcr.io/abralock/airgaped-litellm"), image
        assert image.endswith(":unreleased"), image


def test_netpols(base):
    pg = find(base, "NetworkPolicy", "litellm-postgres-ingress")
    assert pg["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "litellm-postgres"
    allowed = {f["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for f in pg["spec"]["ingress"][0]["from"]}
    assert allowed == {"litellm", "litellm-backup", "litellm-migrate", "litellm-key-expiry"}
    assert pg["spec"]["ingress"][0]["ports"] == [{"port": 5432, "protocol": "TCP"}]

    eg = find(base, "NetworkPolicy", "litellm-egress-internal")
    assert eg["spec"]["policyTypes"] == ["Egress"]
    assert eg["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "litellm"
    ports = sorted((p["protocol"], p["port"]) for rule in eg["spec"]["egress"] for p in rule["ports"])
    assert ports == [("TCP", 53), ("TCP", 5432), ("UDP", 53)]


def test_service_and_ingress(base):
    svc = find(base, "Service", "litellm")
    assert svc["spec"]["ports"][0]["port"] == 4000
    ing = find(base, "Ingress", "litellm")
    assert ing["spec"]["rules"][0]["host"] == "REPLACE_ME_HOSTNAME"
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "litellm", "port": {"number": 4000}}


def test_key_expiry_cronjob(base):
    """LiteLLM leaves some keys without expiry (/user/new keys, /key/update duration=null).
    A scheduled job gives every such key 90 days."""
    cj = find(base, "CronJob", "litellm-key-expiry")
    assert cj["spec"]["schedule"] == "*/10 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    (c,) = containers(cj)
    script = " ".join(c["command"] + c.get("args", []))
    assert 'UPDATE "LiteLLM_VerificationToken"' in script
    assert "expires IS NULL" in script
    assert "interval '90 days'" in script
    labels = cj["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "litellm-key-expiry"
    pg = find(base, "NetworkPolicy", "litellm-postgres-ingress")
    allowed = {f["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for f in pg["spec"]["ingress"][0]["from"]}
    assert "litellm-key-expiry" in allowed
