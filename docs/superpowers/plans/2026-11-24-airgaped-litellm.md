# airgaped-litellm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a LiteLLM gateway image (offline by default) to `ghcr.io/abralock/airgaped-litellm`, with a tag-driven release pipeline and Kustomize manifests (LiteLLM + its own Postgres) for the company's own Kubernetes, reaching AWS Bedrock (us-east-1) and the corporate LiteLLM.

**Architecture:** A one-line thin layer on the official non-root LiteLLM image adds offline env vars. All behaviour lives in a single `config.yaml` (ConfigMap). An offline Docker Compose stack on a no-internet network, with a stdlib mock that fakes both the corporate LiteLLM and the Bedrock runtime, proves the image works air-gapped; a kind cluster proves the manifests run. GitHub Actions runs both on every push and publishes on `v*` tags.

**Tech Stack:** Docker / buildx, LiteLLM `v1.110.3` (`ghcr.io/berriai/litellm-non_root`), Postgres 17.12 (alpine), Kustomize (via `kubectl kustomize`), kind, GitHub Actions, Python 3 stdlib (mock + smoke test), pytest + PyYAML (manifest tests), bash.

**Spec:** `docs/superpowers/specs/2026-11-24-airgaped-litellm-design.md`

## Global Constraints

- Base image: `ghcr.io/berriai/litellm-non_root:v1.110.3@sha256:d9790b3eba82a99bd074f26ceb0f471385365115f460f04f1f19d62e2f11461b` (official non-root variant of `ghcr.io/berriai/litellm`; runs as uid 65534, Prisma engines bundled, `PRISMA_OFFLINE_MODE=true`). Plan-level call: satisfies the spec's "runs as non-root" without our own user setup.
- Postgres upstream: `postgres:17.12-alpine@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73`, defined once in `versions.env` as `POSTGRES_UPSTREAM`.
- Baked ENV, exactly: `LITELLM_OFFLINE_MODE=true`, `LITELLM_TELEMETRY_DISABLED=true`, `LITELLM_LOCAL_MODEL_COST_MAP=True`.
- Published images: `ghcr.io/abralock/airgaped-litellm`, `ghcr.io/abralock/airgaped-litellm-postgres`; platforms `linux/amd64,linux/arm64`.
- Release tags `v<litellm-version>-r<N>` (first: `v1.110.3-r1`); image tag = git tag; `:latest` moves unless the tag contains `-rc`; the tag's LiteLLM version must equal the Dockerfile's.
- Nothing environment-specific or secret is committed: placeholders are `REPLACE_ME_<WHAT>`; no `kind: Secret` in `deploy/base` or `deploy/overlays`.
- Secrets (names fixed, consumed via `envFrom`): `litellm-secrets` with `LITELLM_MASTER_KEY` (must start `sk-`), `LITELLM_SALT_KEY`, `DATABASE_URL`, `CORP_LITELLM_URL`, `CORP_LITELLM_KEY`, and either `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY` or `AWS_BEARER_TOKEN_BEDROCK`; `litellm-postgres` with `POSTGRES_PASSWORD`. DB name and user are both `litellm`.
- Limits: new users `internal_user`, $50 per `30d`; keys default and upper bound `duration: 90d`, `max_budget: 50`.
- Serving pods: 2 replicas, `DISABLE_SCHEMA_UPDATE=true`; migrations only via `litellm --skip_server_startup` (Job `litellm-migrate-<tag>`).
- The image has no curl/jq/wget; anything run inside it uses Python stdlib.
- Commits end with the session attribution lines.

## Review Focus

1. **Bedrock returns 5xx or is unreachable** → the same request succeeds through the `corp/` twin (fallback), not an error to the user. Test: Task 2, `fallback_to_corp`.
2. **A user creates a key without expiry/budget, or asks for 365 days** → the key gets `90d`/$50 defaults; the over-long request is capped or rejected, never granted. Test: Task 2, `key_limits`.
3. **Someone requests a model not in the config** (e.g. `bedrock/anthropic.claude-3-opus` or `gpt-4o`) → 4xx, no upstream call recorded by the mock. Test: Task 2, `unknown_model_rejected`.
4. **The admin UI in a browser with no internet** → `/ui/` HTML references no external `http(s)://` script/style/font hosts. Test: Task 2, `ui_offline`.
5. **Postgres data loss** → the nightly backup produces a restorable dump. Test: Task 6, the kind e2e runs the backup CronJob once and checks `pg_restore --list` on the file.

---

## File Structure

```
Dockerfile                         # FROM pinned non-root image + 3 ENV lines + OCI labels
versions.env                       # POSTGRES_UPSTREAM=<pinned ref>
.dockerignore  .gitignore
requirements-dev.txt               # pytest, pyyaml (pinned)
tests/image_test.sh                # asserts ENV, user, no baked config
tests/test_manifests.py            # renders kustomizations, asserts invariants
tests/test_stamp_release.py        # stamp script against a temp copy of deploy/
ci/mock_upstream.py                # fake corp LiteLLM (OpenAI API) + fake Bedrock runtime (Converse)
ci/offline-test/compose.yaml       # internal network: postgres, migrate, litellm, mock, tester
ci/offline-test/run.sh             # runs the stack twice (bearer creds, access-key creds) + assertions
ci/offline-test/checks.py          # assertion suite run inside the tester container
ci/kind/                           # kustomization for e2e: base + mock + fake secrets + local images
ci/kind-e2e.sh                     # create cluster, load images, migrate, deploy, smoke test, backup check
scripts/smoke_test.py              # real-environment check (stdlib only)
scripts/stamp-release.sh           # sets image tags/digests + migrate Job name in a release copy of deploy/
deploy/base/                       # config.yaml, kustomization, litellm, postgres, backup, netpols
deploy/base/migrate/               # Job-only kustomization (applied first)
deploy/overlays/{test,prod}/       # namespace, host, ingress, egress/ingress netpols, (migrate/ subdir)
.github/workflows/ci.yml           # push/PR + workflow_call
.github/workflows/release.yml      # v* tags
README.md  docs/rollout.md  docs/operations.md  docs/users.md
```

Plan-level deviations from the spec's layout (no behaviour change): the model config lives at `deploy/base/config.yaml` instead of `config/` (Kustomize cannot load files outside its directory, and one copy avoids drift); the smoke test is `scripts/smoke_test.py` (the image has no curl/jq, and operators get the same script). "Lower limits" for the `test` overlay means 1 replica and smaller resources; user/key limits are identical in both.

---

### Task 1: Image

**Files:**
- Create: `Dockerfile`, `versions.env`, `.dockerignore`, `.gitignore`, `tests/image_test.sh`

**Interfaces:**
- Produces: local image tag `airgaped-litellm:ci` (built with `docker build -t airgaped-litellm:ci .`); `versions.env` var `POSTGRES_UPSTREAM`; Dockerfile `ARG LITELLM_VERSION=v1.110.3` (read by Task 7's tag check via `grep '^ARG LITELLM_VERSION='`).

- [ ] **Step 1: Write `tests/image_test.sh <image>`** — fails unless, via `docker inspect` / `docker run --rm --entrypoint python <image> -c ...`:
  - `.Config.Env` contains `LITELLM_OFFLINE_MODE=true`, `LITELLM_TELEMETRY_DISABLED=true`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `PRISMA_OFFLINE_MODE=true`;
  - `.Config.User` is `65534`;
  - no `/app/config.yaml` and no file under `/app` matching `*config*.yaml` that we added (compare against the base image's file list is fine);
  - label `org.opencontainers.image.source=https://github.com/abralock/airgaped-litellm`.
- [ ] **Step 2: Run it against the base image** — `tests/image_test.sh ghcr.io/berriai/litellm-non_root:v1.110.3`. Expected: FAIL on `LITELLM_OFFLINE_MODE`.
- [ ] **Step 3: Write the Dockerfile** — `ARG LITELLM_VERSION=v1.110.3`, `FROM ghcr.io/berriai/litellm-non_root:${LITELLM_VERSION}@sha256:d9790b3e…` (full digest from Global Constraints), the three `ENV`s, OCI labels (`source`, `description`, `licenses=MIT`). Nothing else. `versions.env` holds `POSTGRES_UPSTREAM`. `.gitignore`: `.venv/`, `dist/`, `*.env.local`, `__pycache__/`.
- [ ] **Step 4: Build and re-run** — `docker build -t airgaped-litellm:ci . && tests/image_test.sh airgaped-litellm:ci`. Expected: PASS.
- [ ] **Step 5: Commit** — `feat: thin offline layer on official LiteLLM non-root image`.

### Task 2: Config, mock upstream and offline test

**Files:**
- Create: `deploy/base/config.yaml`, `ci/mock_upstream.py`, `ci/offline-test/compose.yaml`, `ci/offline-test/run.sh`, `ci/offline-test/checks.py`

**Interfaces:**
- Consumes: `airgaped-litellm:ci`, `versions.env`.
- Produces:
  - `deploy/base/config.yaml` model names (used by every later task): `bedrock/claude-sonnet` → `bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `bedrock/claude-haiku` → `bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` (both `aws_region_name: us-east-1`, no credentials in config; comment says "replace with the inference profile IDs enabled in your account"), `corp/claude-sonnet` → `litellm_proxy/REPLACE_ME_CORP_MODEL_SONNET`, `corp/claude-haiku` → `litellm_proxy/REPLACE_ME_CORP_MODEL_HAIKU` (both `api_base: os.environ/CORP_LITELLM_URL`, `api_key: os.environ/CORP_LITELLM_KEY`). `router_settings.fallbacks`: each `bedrock/x` → `[corp/x]`. `general_settings`: `master_key: os.environ/LITELLM_MASTER_KEY`, `database_url: os.environ/DATABASE_URL`, `store_model_in_db: false`. `litellm_settings`: `default_internal_user_params` (`user_role: internal_user`, `max_budget: 50`, `budget_duration: 30d`), `default_key_generate_params` and `upperbound_key_generate_params` (`duration: 90d`, `max_budget: 50`).
  - `ci/mock_upstream.py` (stdlib `ThreadingHTTPServer`, `--port 8080`): `GET /v1/models`; `POST /chat/completions` and `/v1/chat/completions` → OpenAI response with content `"pong from corp"`; `POST /model/{id}/converse` → Bedrock Converse response with text `"pong from bedrock"` (or HTTP 500 while failure mode is on); `POST /_fail_bedrock` body `{"on": bool}`; `GET /_requests` → `[{"path", "authorization"}]`; `DELETE /_requests`.
  - `ci/offline-test/run.sh` — exit 0 only if every check passes in both credential modes; usable locally and in CI.

- [ ] **Step 1: Write `checks.py`** (runs in the tester container: our image with `--entrypoint python`; base URL `http://litellm:4000`, mock `http://mock:8080`, `LITELLM_MASTER_KEY` and `EXPECT_AUTH` (`bearer` | `sigv4`) from env). Each check is a named function; the script prints `PASS <name>` / `FAIL <name>: <why>` and exits non-zero on any failure:
  - `ready_in_time`: `/health/readiness` 200 within 90 s of the tester starting.
  - `create_user_and_key`: master key → `POST /user/new {"user_role":"internal_user"}` → user key; `GET /v1/models` with it lists exactly the 4 configured names.
  - `chat_via_corp`: `corp/claude-sonnet` reply contains `pong from corp`; mock recorded `authorization == "Bearer sk-corp-test"`.
  - `chat_via_bedrock`: `bedrock/claude-sonnet` reply contains `pong from bedrock`; mock recorded a `/model/us.anthropic.claude-sonnet-4-5-20250929-v1:0/converse` call whose authorization starts with `Bearer ABSK` when `EXPECT_AUTH=bearer`, or with `AWS4-HMAC-SHA256 Credential=AKIATESTONLY` when `sigv4`.
  - `fallback_to_corp`: `/_fail_bedrock on` → `bedrock/claude-sonnet` reply contains `pong from corp`; then `off`.
  - `key_limits`: with the user's key, `POST /key/generate {}` → `expires` not null and ≤ 90 days out, `max_budget == 50`; `POST /key/generate {"duration":"365d","max_budget":1000}` → either 4xx, or a key whose expiry ≤ 90 days and budget ≤ 50.
  - `unknown_model_rejected`: `gpt-4o` and `bedrock/anthropic.claude-3-opus-20240229-v1:0` → 4xx, and `/_requests` gains no entry.
  - `ui_offline`: `GET /ui/` 200; no `<script src=` / `<link href=` / `@import`/`url(` pointing at `http(s)://` outside `litellm`.
- [ ] **Step 2: Write `compose.yaml` and `run.sh`** — one network `airgap` with `internal: true` (no route out); services `postgres` (`${POSTGRES_UPSTREAM}`), `mock` (our image, `--entrypoint python /ci/mock_upstream.py`), `migrate` (our image, `--config /app/config.yaml --skip_server_startup`, `depends_on postgres healthy`), `litellm` (our image, `--config /app/config.yaml --port 4000`, `DISABLE_SCHEMA_UPDATE=true`, `depends_on migrate completed_successfully`), `tester` (runs `checks.py`). Env: `CORP_LITELLM_URL=http://mock:8080`, `CORP_LITELLM_KEY=sk-corp-test`, `AWS_BEDROCK_RUNTIME_ENDPOINT=http://mock:8080` (verify LiteLLM 1.110.3 reads this env var; if it does not, find the env var it does read for the runtime endpoint — the shipped config must stay endpoint-free). `run.sh` runs the stack twice with `--env-file versions.env` plus a credential env: pass 1 `AWS_BEARER_TOKEN_BEDROCK=ABSK-test-only`, `EXPECT_AUTH=bearer`; pass 2 `AWS_ACCESS_KEY_ID=AKIATESTONLY`, `AWS_SECRET_ACCESS_KEY=test-only`, `EXPECT_AUTH=sigv4`. After each pass: dump `litellm` and `migrate` logs; fail if they match `-iE 'ConnectError|ConnectTimeout|Name or service not known|Temporary failure in name resolution|getaddrinfo|raw\.githubusercontent|Failed to fetch'`; always `down -v`.
- [ ] **Step 3: Run with a deliberately empty config** (`deploy/base/config.yaml` containing only `model_list: []`) — `ci/offline-test/run.sh`. Expected: FAIL at `create_user_and_key` (model list mismatch), proving the harness reports failures.
- [ ] **Step 4: Write the real `config.yaml` and `mock_upstream.py`** per Interfaces.
- [ ] **Step 5: Run** — `ci/offline-test/run.sh`. Expected: all checks `PASS` in both passes, log scan clean. If `key_limits` or `ui_offline` fails because of LiteLLM's real behaviour, stop and report it — do not weaken the assertion.
- [ ] **Step 6: Commit** — `feat: gateway config and offline end-to-end test`.

### Task 3: Smoke test for real environments

**Files:**
- Create: `scripts/smoke_test.py`
- Modify: `ci/offline-test/checks.py` (add a final check that runs it), `ci/offline-test/compose.yaml` (mount `scripts/`)

**Interfaces:**
- Produces: `python3 scripts/smoke_test.py --base-url URL [--model NAME ...] [--insecure]`, master key from `LITELLM_MASTER_KEY`. Creates a key (`key_alias: smoke-test-<utc timestamp>`, `duration: 1h`, `max_budget: 1`), lists `/v1/models` with it (or uses `--model`), sends each `"Reply with the single word: pong"` (`max_tokens: 16`), prints `PASS <model> <latency_ms>` or `FAIL <model> <status> <first 200 chars>`, deletes the key in `finally`, exits 1 if any failed or if no models. Stdlib only (`urllib`, `ssl`, `json`); `--insecure` skips TLS verification; honours `SSL_CERT_FILE` by default.

- [ ] **Step 1: Add check `smoke_script`** to `checks.py`: runs `smoke_test.py --base-url http://litellm:4000` as a subprocess; expects exit 0 and 4 `PASS` lines; then with `/_fail_bedrock on` and `--model corp/claude-sonnet --model gpt-4o` expects exit 1 and one `FAIL gpt-4o` line; afterwards `GET /key/list` contains no `smoke-test-*` alias.
- [ ] **Step 2: Run** `ci/offline-test/run.sh`. Expected: FAIL `smoke_script` (file missing).
- [ ] **Step 3: Implement `scripts/smoke_test.py`.**
- [ ] **Step 4: Run** `ci/offline-test/run.sh`. Expected: all PASS.
- [ ] **Step 5: Commit** — `feat: smoke test script for deployed gateways`.

### Task 4: Kubernetes base manifests

**Files:**
- Create: `deploy/base/kustomization.yaml`, `deploy/base/litellm.yaml` (ServiceAccount `litellm`, Deployment `litellm`, Service `litellm`), `deploy/base/postgres.yaml` (Service + StatefulSet `litellm-postgres`, 10Gi PVC template), `deploy/base/backup.yaml` (PVC `litellm-backups` 20Gi, CronJob `litellm-backup`), `deploy/base/networkpolicies.yaml`, `deploy/base/ingress.yaml`, `deploy/base/migrate/kustomization.yaml`, `deploy/base/migrate/job.yaml`, `requirements-dev.txt`, `tests/test_manifests.py`

**Interfaces:**
- Consumes: `deploy/base/config.yaml` (Task 2).
- Produces:
  - Labels: `app.kubernetes.io/name: litellm | litellm-postgres | litellm-backup | litellm-migrate`, all with `app.kubernetes.io/part-of: airgaped-litellm`.
  - ConfigMap via `configMapGenerator` named `litellm-config` (key `config.yaml`), mounted at `/app/config.yaml`.
  - `images:` in both base kustomizations: `ghcr.io/abralock/airgaped-litellm` and `ghcr.io/abralock/airgaped-litellm-postgres`, `newTag: unreleased` (Task 7 stamps it).
  - Job `litellm-migrate` (Task 7 renames to `litellm-migrate-<tag>`), args `["--config","/app/config.yaml","--skip_server_startup"]`, `backoffLimit: 2`, `ttlSecondsAfterFinished: 86400`, no `DISABLE_SCHEMA_UPDATE`.
  - Ingress `litellm`, host `REPLACE_ME_HOSTNAME`, backend `litellm:4000` (overlays patch host/class/TLS).
  - NetworkPolicies in base: `litellm-postgres-ingress` (Postgres pods accept 5432 only from `litellm`, `litellm-backup`, `litellm-migrate` labels); `litellm-egress-internal` (LiteLLM pods egress to Postgres 5432 and DNS 53 TCP/UDP only). External egress and LiteLLM ingress are added by overlays (policies are additive).
- `tests/test_manifests.py` helper `render(path: str) -> list[dict]` (runs `kubectl kustomize <path>`, `yaml.safe_load_all`) — reused by Tasks 5 and 7.

- [ ] **Step 1: Write tests** (`pytest -q tests/test_manifests.py`), each asserting on `render("deploy/base")` / `render("deploy/base/migrate")`:
  - `test_litellm_deployment`: 2 replicas; env `DISABLE_SCHEMA_UPDATE=true`; `envFrom` secretRef `litellm-secrets`; probes on `/health/liveliness` and `/health/readiness` port 4000; `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, capabilities drop `ALL`; resources requests and limits set; config mounted at `/app/config.yaml`.
  - `test_migrate_job`: one Job, args contain `--skip_server_startup`, no `DISABLE_SCHEMA_UPDATE`, same `envFrom`.
  - `test_postgres`: StatefulSet 1 replica, `POSTGRES_PASSWORD` from secret `litellm-postgres`, `POSTGRES_USER`/`POSTGRES_DB` `litellm`, PVC template.
  - `test_backup_cronjob`: schedule `0 2 * * *`, `concurrencyPolicy: Forbid`, command uses `pg_dump -Fc` and deletes dumps older than 7 days (`-mtime +6`), mounts `litellm-backups`.
  - `test_no_secrets_committed`: no `kind: Secret` in either render.
  - `test_images`: every container image starts with `ghcr.io/abralock/airgaped-litellm` and has tag `unreleased`.
  - `test_netpols`: the two base policies exist with the selectors above.
- [ ] **Step 2: Run** — `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q tests/test_manifests.py`. Expected: FAIL (no kustomization).
- [ ] **Step 3: Write the manifests.** Backup CronJob: Postgres image, `PGPASSWORD` from `litellm-postgres`, writes `/backups/litellm-$(date -u +%Y%m%dT%H%M%SZ).dump`, then `find /backups -name '*.dump' -mtime +6 -delete`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** — `feat: base Kubernetes manifests`.

### Task 5: Overlays `test` and `prod`

**Files:**
- Create: `deploy/overlays/{test,prod}/kustomization.yaml`, `.../ingress-patch.yaml`, `.../networkpolicies.yaml`, `.../migrate/kustomization.yaml`, `deploy/overlays/prod/proxy-ca-patch.yaml` (commented example, not referenced by default)
- Modify: `tests/test_manifests.py`

**Interfaces:**
- Consumes: base, `render()`.
- Produces: namespaces `litellm-test`, `litellm-prod`; each overlay's `migrate/` renders only the Job in that namespace with the same `images:`. Placeholders: `REPLACE_ME_HOSTNAME`, `REPLACE_ME_INGRESS_CLASS`, `REPLACE_ME_TLS_SECRET`, `REPLACE_ME_HARBOR/airgaped-litellm`, `REPLACE_ME_HARBOR/airgaped-litellm-postgres`, `REPLACE_ME_BEDROCK_EGRESS_CIDR`, `REPLACE_ME_CORP_LITELLM_CIDR`, `REPLACE_ME_CORP_LITELLM_PORT` (use `443` as the literal port with a comment if a placeholder port breaks rendering), `REPLACE_ME_INGRESS_SOURCE` (namespace selector label value or CIDR, with a comment showing both forms). Overlay NetworkPolicies: `litellm-egress-external` (443 to Bedrock CIDR; corp CIDR:port) and `litellm-ingress` (4000 only from the ingress source). Placeholder CIDRs must still render; use a valid documentation CIDR (`192.0.2.0/24`) with a `# REPLACE_ME_...` comment if `kubectl kustomize` rejects the literal.
- `test`: `replicas: 1`, requests 250m/512Mi, limits 1/1Gi. `prod`: base sizes. The proxy/CA example shows `HTTPS_PROXY`, `NO_PROXY` (must include `litellm-postgres`, `.svc`, `.cluster.local`) and mounting a ConfigMap `company-ca` at `/etc/company-ca/ca.crt` with `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` pointing at it.

- [ ] **Step 1: Write tests**: `test_overlay_renders[test|prod]` (namespace on every namespaced object; images renamed to the Harbor placeholders; Ingress host/class/TLS patched; both overlay policies present; no `kind: Secret`); `test_overlay_migrate[test|prod]` (only a Job, correct namespace, Harbor image); `test_test_overlay_is_smaller` (1 replica); `test_placeholders_listed` (every `REPLACE_ME_[A-Z_]+` found under `deploy/` appears in `docs/rollout.md` — skip with a clear reason until Task 8 exists, then remove the skip in Task 8).
- [ ] **Step 2: Run** `.venv/bin/pytest -q`. Expected: FAIL.
- [ ] **Step 3: Write the overlays.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: PASS (placeholder test skipped).
- [ ] **Step 5: Commit** — `feat: test and prod overlays`.

### Task 6: kind end-to-end test of the manifests

**Files:**
- Create: `ci/kind/kustomization.yaml`, `ci/kind/mock.yaml`, `ci/kind/migrate/kustomization.yaml`, `ci/kind-e2e.sh`

**Interfaces:**
- Consumes: base, `airgaped-litellm:ci`, `ci/mock_upstream.py`, `scripts/smoke_test.py`, `versions.env`.
- Produces: `ci/kind-e2e.sh` — exit 0 only if everything below passes; deletes its cluster (`airgaped-litellm-e2e`) on exit unless `KEEP_CLUSTER=1`.
- `ci/kind` = base + namespace `litellm-e2e` + mock Deployment/Service (`mock:8080`, our image running the mock from a ConfigMap) + `secretGenerator` with fake values only (`LITELLM_MASTER_KEY=sk-e2e-master`, `CORP_LITELLM_URL=http://mock:8080`, `CORP_LITELLM_KEY=sk-corp-test`, `AWS_BEARER_TOKEN_BEDROCK=ABSK-test-only`, `AWS_BEDROCK_RUNTIME_ENDPOINT` (or the var Task 2 found) `=http://mock:8080`, DB URL/password) + `images:` mapping to `airgaped-litellm:ci` and the upstream Postgres ref + egress policy allowing the mock. Ingress removed (port-forward instead).

- [ ] **Step 1: Write `ci/kind-e2e.sh`**: create cluster; `kind load docker-image` both images; `kubectl apply -k ci/kind/migrate` and Postgres first, `kubectl wait --for=condition=complete job/litellm-migrate --timeout=300s`; `kubectl apply -k ci/kind`; `kubectl rollout status deploy/litellm --timeout=300s`; port-forward `svc/litellm 14000:4000`; `python3 scripts/smoke_test.py --base-url http://127.0.0.1:14000` must pass 4 models; `kubectl create job --from=cronjob/litellm-backup backup-now`, wait complete, then in a pod mounting `litellm-backups` run `pg_restore --list /backups/*.dump | grep -q LiteLLM_VerificationToken`. On failure print `kubectl get events` and logs of all pods.
- [ ] **Step 2: Run** `ci/kind-e2e.sh` before `ci/kind/` exists. Expected: FAIL at the first `kubectl apply`.
- [ ] **Step 3: Write `ci/kind/`.**
- [ ] **Step 4: Run** `ci/kind-e2e.sh`. Expected: smoke test 4× PASS, backup check passes, exit 0. Any manifest fix found here goes into `deploy/base` with a matching assertion in `tests/test_manifests.py`.
- [ ] **Step 5: Commit** — `test: kind end-to-end for manifests, migration and backup`.

### Task 7: CI and release workflows

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `scripts/stamp-release.sh`, `tests/test_stamp_release.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `scripts/stamp-release.sh <tag> <litellm-digest> <postgres-digest> <out-dir>` — copies `deploy/` to `<out-dir>/deploy`, sets both base kustomizations' `images:` to `newTag: <tag>` and `digest: <digest>`, renames Job `litellm-migrate` → `litellm-migrate-<tag>` everywhere it is referenced, refuses (exit 2) a tag not matching `^v[0-9]+\.[0-9]+\.[0-9]+-(r[0-9]+|rc[0-9]+)$` or whose version differs from the Dockerfile's `LITELLM_VERSION`.

- [ ] **Step 1: Write `tests/test_stamp_release.py`**: stamping `v1.110.3-r1` with two fake `sha256:` digests → rendered overlay images end with `@sha256:<digest>` and no `unreleased`; migrate Job named `litellm-migrate-v1.110.3-r1` in `deploy/overlays/prod/migrate`; `v1.110.4-r1` → exit 2; `v1.110.3` → exit 2; `v1.110.3-rc1` → exit 0; source `deploy/` unchanged.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_stamp_release.py`. Expected: FAIL.
- [ ] **Step 3: Implement `stamp-release.sh`** (bash + `sed` or a short `python3` stdlib snippet; no `kustomize` binary, so it runs anywhere `ubuntu-latest` does).
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: PASS.
- [ ] **Step 5: Write `ci.yml`** — `on: push, pull_request, workflow_call`; `permissions: contents: read`; jobs: `image` (build `airgaped-litellm:ci`, `tests/image_test.sh`, `ci/offline-test/run.sh`), `manifests` (python 3.12, `pip install -r requirements-dev.txt`, `pytest -q`), `kind-e2e` (`helm/kind-action` with `install_only: true`, build image, `ci/kind-e2e.sh`), `actionlint` (`docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest`).
- [ ] **Step 6: Write `release.yml`** — `on: push: tags: ["v*"]`; job `test` = `uses: ./.github/workflows/ci.yml`; job `publish` (`needs: test`, `permissions: contents: write, packages: write`): verify tag (call `stamp-release.sh` in a dry-run dir first so a bad tag fails before pushing); compute tags (`:<tag>`, plus `:latest` unless `*-rc*`); buildx multi-arch build-push of `ghcr.io/abralock/airgaped-litellm`, capture digest; `docker buildx imagetools create` to copy `${POSTGRES_UPSTREAM}` to `ghcr.io/abralock/airgaped-litellm-postgres` with the same tags, capture its digest; `stamp-release.sh` into `dist/`; `tar -C dist -czf dist/airgaped-litellm-deploy-<tag>.tar.gz deploy` and `.sha256`; `softprops/action-gh-release@v2` with both files, `prerelease: true` for `-rc` tags, and a body listing both image references by digest plus the pull → scan → Harbor commands from `docs/rollout.md`.
- [ ] **Step 7: Lint** — `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest`. Expected: no output, exit 0.
- [ ] **Step 8: Commit** — `ci: test on push, publish images and deploy bundle on tags`.

### Task 8: Documentation

**Files:**
- Create: `README.md`, `docs/rollout.md`, `docs/operations.md`, `docs/users.md`
- Modify: `tests/test_manifests.py` (remove the skip on `test_placeholders_listed`)

**Interfaces:**
- Consumes: placeholder names (Task 5), secret names and limits (Global Constraints), script CLIs (Tasks 3, 6, 7).

Content each doc must contain (prose is the implementer's):
- `README.md`: what it is (one paragraph), the two backends, image names, links to the three docs, the shared-upstream-key warning from the spec, "Not affiliated with BerriAI".
- `docs/rollout.md`: (1) AWS prerequisites — enable the Claude models in us-east-1; prod: IAM user + this least-privilege policy JSON (actions `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`; resources: the two `arn:aws:bedrock:us-east-1:REPLACE_ME_AWS_ACCOUNT_ID:inference-profile/us.anthropic...` profiles **and** `arn:aws:bedrock:*::foundation-model/anthropic.claude-*` for the regions the US profiles route to); test: Bedrock API key with ≤30-day expiry. (2) Import: `docker pull --platform linux/amd64 ghcr.io/abralock/airgaped-litellm@<digest>` (and Postgres), scan, `docker tag` + `docker push` to Harbor; check digests against the release notes. (3) Download and verify the deploy tarball (`sha256sum -c`). (4) Fill placeholders — a table of every `REPLACE_ME_*` and what goes there; the check `grep -rn REPLACE_ME deploy/` must print nothing. (5) Create secrets with exact `kubectl create secret generic` commands (generate master/salt keys with `openssl rand`; master key prefixed `sk-`; `DATABASE_URL=postgresql://litellm:<pw>@litellm-postgres:5432/litellm`). (6) Order: `apply -k deploy/overlays/<env>/migrate` → `kubectl wait job/litellm-migrate-<tag>` → `apply -k deploy/overlays/<env>` → `rollout status` → `smoke_test.py`. (7) First admin login (username `admin`, password = master key), create a named `proxy_admin`, then `disable_env_credential_login: true`. (8) NetworkPolicies only work if the CNI enforces them; optional proxy/company CA patch.
- `docs/operations.md`: upgrade (change `ARG LITELLM_VERSION` and digest, tag `v<new>-r1`, re-run rollout from step 2); backup schedule/retention and restore (`pg_restore --clean --if-exists` into a scaled-to-zero deployment); rotate the AWS key (every 90 days) and the corporate key (update secret, `rollout restart`); **never change `LITELLM_SALT_KEY`**; onboarding a user (UI → Internal Users → invite link); where limits live in `config.yaml`.
- `docs/users.md`: log in via invite link, create a key, `curl` and OpenAI Python SDK examples against `https://<gateway>/v1` with the four model names, what `corp/` vs `bedrock/` means, keys expire within 90 days and have a $50 budget.

- [ ] **Step 1: Remove the skip** and run `.venv/bin/pytest -q`. Expected: FAIL `test_placeholders_listed`.
- [ ] **Step 2: Write the docs.**
- [ ] **Step 3: Run** `.venv/bin/pytest -q`. Expected: PASS. Also run every command block in `docs/rollout.md` steps 3–6 mentally against the kind e2e (same commands, different overlay) — any difference means the doc is wrong.
- [ ] **Step 4: Commit** — `docs: rollout, operations and user guides`.

### Task 9: Publish (requires the owner's go-ahead before Step 1)

- [ ] **Step 1: Ask the owner to confirm** creating the **public** repo `abralock/airgaped-litellm` and pushing. Do not proceed without a yes.
- [ ] **Step 2: Create and push** — `gh repo create abralock/airgaped-litellm --public --source . --push --description "LiteLLM gateway for air-gapped Kubernetes: offline image, Bedrock + upstream LiteLLM, self-serve keys"`. Watch `gh run watch` on the `ci` run. Expected: all four jobs green. Fix failures in new commits (never skip a check).
- [ ] **Step 3: Release** — after the owner confirms, `git tag v1.110.3-r1 && git push origin v1.110.3-r1`; `gh run watch` the release run. Expected: green, release `v1.110.3-r1` with tarball + `.sha256`.
- [ ] **Step 4: Verify publicly** — `docker logout ghcr.io; docker buildx imagetools inspect ghcr.io/abralock/airgaped-litellm:v1.110.3-r1` (and the Postgres image) shows amd64 + arm64. If unauthenticated access is denied, GHCR created the packages private: tell the owner to set both packages to Public under github.com/abralock → Packages → Package settings (this cannot be done from the API), then re-check.
- [ ] **Step 5: Verify the bundle** — download the release tarball, `sha256sum -c`, extract, `kubectl kustomize deploy/overlays/prod` shows `@sha256:` digests matching the release notes.
