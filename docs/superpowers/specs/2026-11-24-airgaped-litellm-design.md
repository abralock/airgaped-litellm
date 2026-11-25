# airgaped-litellm — design

Date: 2026-11-24
Status: approved by owner 2026-11-24

## Intent

The company wants to run [LiteLLM](https://github.com/BerriAI/litellm) inside its
air-gapped network as **one internal gateway to large language models**. People log in
to LiteLLM's web UI, create their own API key (`sk-...`), and call models through the
gateway with OpenAI-compatible tools. Nobody outside the gateway ever holds AWS or
upstream credentials.

The gateway reaches models through two backends:

1. **AWS Bedrock** (us-east-1), directly.
2. **The corporate LiteLLM**, an existing company-run LiteLLM that has issued the
   project owner an `sk-...` API key.

This repository holds everything needed to ship that: a container image, a release
pipeline that pushes the image to `ghcr.io/abralock/`, and Kubernetes manifests to run it
with its own Postgres on the company's own (on-premises) Kubernetes. It follows the same pattern as the owner's other
air-gapped projects (`airgaped-pr-agent`, `snapshotkit`): GitHub tag → GitHub Actions
build → image on GHCR → imported into the internal registry.

## Goals

- Users self-serve keys through LiteLLM's built-in UI with username/password login.
- Both backends available; either can be switched on or off per environment without
  rebuilding the image.
- The image starts and works with no internet access; this is proved in CI on every
  build, not assumed.
- A one-command release: pushing a git tag builds, tests, and pushes the image and a
  GitHub Release with the manifests.
- The same image and manifests work for testing and production on the corporate
  Kubernetes cluster; only Secrets and one overlay differ.

## Non-goals

- SSO login to the UI (free for only 5 users; beyond that LiteLLM Enterprise is needed).
- Building LiteLLM from source or patching it.
- A custom key portal; LiteLLM's own UI is the portal.
- Managing models through the UI (models live in config, in git).
- Provisioning AWS (IAM user and policy, Bedrock model access) or the cluster. The
  docs say what is needed; creating it is outside this repo.
- High-availability Postgres. One instance plus nightly backups.

## Anti-goals (would make this a failure even if it technically works)

- The proxy silently reaching the internet at startup or at runtime (price map
  downloads, telemetry, version checks). Hanging or failing in the air gap is the
  main risk this project exists to remove.
- AWS credentials or the corporate `sk-` key baked into the image or committed to git.
- A user key with no expiry and no budget.
- Losing the Postgres data (= every user loses their keys) with no way to restore.

## Constraints and facts this design relies on

- LiteLLM issues per-user keys, budgets and spend tracking only with a Postgres
  database (`DATABASE_URL`).
- `ghcr.io/berriai/litellm` now ships the Prisma toolchain, entrypoint and migrations;
  `litellm-database` is no longer needed.
- `LITELLM_OFFLINE_MODE=true` stops every request LiteLLM makes on its own (price map,
  release check, breached-password check, tokenizer downloads, etc.). It must be an
  environment variable; config.yaml is read too late.
  `LITELLM_TELEMETRY_DISABLED=true` forces telemetry off.
- `LITELLM_SALT_KEY` encrypts credentials stored in the database. It must never change
  after first install.
- The master key must start with `sk-`.
- With more than one replica, schema migrations must not race:
  `DISABLE_SCHEMA_UPDATE=true` on the serving pods and migrations run once, separately.
- Newer Claude models on Bedrock must be called through cross-region inference
  profiles (`us.anthropic.claude-...`).
- The cluster is the company's own Kubernetes, not EKS, so there is no IAM role for
  service accounts. LiteLLM needs a stored AWS credential, which someone must rotate.
  Two kinds are supported; LiteLLM uses whichever Secret is present:
  - **IAM access key** (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) of a dedicated
    IAM user whose policy allows only `bedrock:InvokeModel` and
    `bedrock:InvokeModelWithResponseStream` on the chosen inference profiles and the
    models behind them. **Recommended for production.**
  - **Bedrock API key** (`ABSK...`, `AWS_BEARER_TOKEN_BEDROCK`), quick to create in the
    Bedrock console, with a short expiry. **Fine for testing.**
  - IAM Roles Anywhere (short-lived credentials from a company certificate) would remove
    the stored key; noted as a later improvement, not built.
- The corporate network reaches the Bedrock API (us-east-1). NetworkPolicy cannot use
  hostnames, so Bedrock egress is allowed on 443 to an operator-supplied CIDR (the
  corporate egress path or proxy), a placeholder in the overlay.
- **Shared upstream key:** through the corporate backend, every user's traffic reaches
  the corporate LiteLLM under the owner's single key and counts against its budget.
  The owner should confirm the company allows this, or get a key issued for this
  service.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Thin layer: `FROM ghcr.io/berriai/litellm:<version>@sha256:<digest>` plus offline settings baked in as `ENV` | Small, one-line upgrades, inherits upstream security fixes, offline settings can't be forgotten. Trusts BerriAI's image contents. |
| B | Build from source on `python:slim` with pinned `pip install litellm[proxy]` | Full control, smaller image. Must prepare Prisma engines and UI assets ourselves — the parts most likely to break offline, again on every upgrade. |
| C | Retag the official image unchanged | Simplest. Offline settings must live in every manifest, and nothing proves the image starts offline. |

The owner picked **A**.

Deployment: the company's own Kubernetes with Kustomize, bringing its own Postgres. A `docker compose`
single-VM setup was considered and rejected (owner runs Kubernetes; compose is only used
inside CI for the offline test).

Key distribution: LiteLLM UI with username/password (chosen) over SSO (licence above 5
users) and admin-issued keys only (no self-service).

## Design

### Repository layout

```
Dockerfile                    # thin layer on the official image
config/                       # example config.yaml (models, limits, fallbacks)
deploy/
  base/                       # LiteLLM Deployment, Service, Ingress, ConfigMap,
                              # migration Job, Postgres StatefulSet, backup CronJob,
                              # NetworkPolicies, ServiceAccount
  overlays/test/              # test namespace: Bedrock API key, test host/limits
  overlays/prod/              # prod namespace: IAM access key, prod host/limits
scripts/
  smoke-test.sh               # real-environment check against a running gateway
ci/offline-test/              # compose + mock upstream for the CI offline test
.github/workflows/ci.yml      # build + offline test on every push/PR
.github/workflows/release.yml # tag → test → push images → GitHub Release
docs/                         # rollout, operations, user guide
```

### Image

- `FROM ghcr.io/berriai/litellm:<latest stable at build time>@sha256:<digest>`.
- Baked `ENV`: `LITELLM_OFFLINE_MODE=true`, `LITELLM_TELEMETRY_DISABLED=true`,
  `LITELLM_LOCAL_MODEL_COST_MAP=True` (belt and braces).
- No config, credentials, or model list baked in; config comes from a ConfigMap.
- Platforms: `linux/amd64`, `linux/arm64` (same as airgaped-pr-agent).
- Name: `ghcr.io/abralock/airgaped-litellm`.
- The pinned Postgres image is copied unchanged to
  `ghcr.io/abralock/airgaped-litellm-postgres`, so everything is imported from one place.

### Versioning and release

- Git tags `v<litellm-version>-r<N>`, e.g. `v1.80.7-r1`. Image tag = git tag
  (matching airgaped-pr-agent). `:latest` moves only for tags without `-rc`.
- `release.yml` on tag push: run the offline test → build and push both images
  (multi-arch) → GitHub Release with a `deploy/` tarball and its SHA-256.
- `ci.yml` on push/PR: build + offline test, push nothing.
- Images reach the air gap through the company's import process: pull from GHCR by
  digest → virus/vulnerability scan → push into the company's Harbor registry.
  `docs/rollout.md` gives the exact commands for both images (LiteLLM and Postgres),
  pinned by digest. Each GitHub Release lists the image digests so the scanned image
  can be checked against what CI built. The manifests reference images through a
  kustomize `images:` override so the Harbor path is set in one place.
- Public repo and public GHCR packages (owner's personal account `abralock`): no token
  needed to pull. Nothing environment-specific is committed: AWS account details,
  egress CIDRs, corporate LiteLLM address, hostnames and Harbor path are placeholders in the
  overlays (`REPLACE_ME_...`), filled in by the operator in their own copy.

### Offline test (CI)

Docker compose on an `internal: true` network (no route out) with: the built image,
Postgres, and a tiny mock OpenAI-compatible server standing in for the corporate LiteLLM.
The test:

1. Waits for `/health/readiness` within a time limit (a hang on an outbound call fails it).
2. Creates an `internal_user` and a key with the master key.
3. With the user's key, calls `/v1/models` and one chat completion through the
   `corp/` backend (mock), proving the `litellm_proxy` route end to end.
4. Scans proxy logs for outbound connection errors/attempts and fails if any appear.

Bedrock cannot be reached in CI; the real Bedrock path is covered by `smoke-test.sh`.

### Configuration (config.yaml)

- **Models**, defined only in config (`store_model_in_db: false`):
  - `bedrock/<name>` → `bedrock/us.anthropic.claude-...`, `aws_region_name: us-east-1`.
    No credentials in config: boto3's default chain picks up `AWS_ACCESS_KEY_ID` /
    `AWS_SECRET_ACCESS_KEY` from the environment, or a Bedrock API key is used when
    `AWS_BEARER_TOKEN_BEDROCK` is set. The builder confirms LiteLLM behaves correctly
    with either one present and the other absent.
  - `corp/<name>` → `litellm_proxy/<upstream model>`,
    `api_base: os.environ/CORP_LITELLM_URL`, `api_key: os.environ/CORP_LITELLM_KEY`.
  - Shipped with example entries; the owner fills the actual model list.
- **Fallbacks**: each `bedrock/x` falls back to `corp/x` where a twin exists.
- **Users and keys**:
  - New users get role `internal_user` (create/view/delete own keys, see own spend).
  - Defaults (builder's call, owner can change): user budget $50 per 30 days; key
    `duration` upper bound 90 days; every key must have an expiry and a budget.
  - Only the models listed in config are callable.
- **Admin**: first login with the master key; docs recommend creating a named
  `proxy_admin` user and then setting `disable_env_credential_login: true`.
- **Onboarding**: admin creates a user in the UI, LiteLLM shows an invite link, the
  admin sends it; the user sets a password and creates keys.

### Kubernetes

- **LiteLLM Deployment**: 2 replicas, `DISABLE_SCHEMA_UPDATE=true`, readiness/liveness
  on LiteLLM health endpoints, runs as non-root, resource requests/limits.
- **Migration Job**: runs the image's migration step once per release before rollout.
- **Service + Ingress**: one internal host (UI + API). Ingress class and TLS are set
  per overlay (left to the operator; whatever controller the corporate cluster runs).
- **Postgres**: single-replica StatefulSet with a PVC; credentials from a Secret.
- **Backups**: nightly `pg_dump` CronJob to a separate PVC, keeping 7 days; restore
  steps documented.
- **Secrets** (never committed; docs show `kubectl create secret`): master key, salt
  key, Postgres password / `DATABASE_URL`, `CORP_LITELLM_KEY` (+ URL), and the AWS
  credential (IAM access key pair, or `AWS_BEARER_TOKEN_BEDROCK`).
- **NetworkPolicies**: Postgres accepts only LiteLLM pods (and the backup job). LiteLLM
  egress only to Postgres, DNS, the corporate LiteLLM, and 443 to the Bedrock egress
  CIDR (placeholder). Ingress to LiteLLM only from the ingress controller.
- **Overlays**:
  Both run on the corporate cluster, in separate namespaces, both backends on.
  - `test`: Bedrock API key Secret, test hostname, lower limits.
  - `prod`: IAM access key Secret (least-privilege policy above), prod hostname,
    prod limits.

### smoke-test.sh

`scripts/smoke-test.sh <base-url>` with the master key from the environment: creates a
temporary key, calls every configured model once, prints pass/fail per model, deletes
the key. Used after every install in test and production.

### Docs

- `README.md`: what it is, quick start.
- `docs/rollout.md`: import images, create Secrets, AWS prerequisites (IAM user and
  least-privilege policy JSON, or a Bedrock API key; Claude models enabled in us-east-1), apply overlay,
  run smoke test.
- `docs/operations.md`: upgrade LiteLLM (change the `FROM` digest, tag a new `-r1`),
  backup/restore, rotating the corporate key, never changing the salt key.
- `docs/users.md`: how a user logs in, creates a key, and points OpenAI SDK/curl at the
  gateway.

## Decided by the owner

- Self-hosted LiteLLM in the air gap; users get keys themselves.
- UI login with username/password.
- The company's own Kubernetes (not EKS), own Postgres; Bedrock in us-east-1.
- Both backends: Bedrock direct and corporate LiteLLM via the owner's `sk-` key.
- Test first with a Bedrock API key the owner creates in the Bedrock console.
- Build approach A (thin layer on the official image).
- Publish to `ghcr.io/abralock/`, code in the owner's public GitHub repo.
- Image import: pull → virus scan → push to the company's Harbor.
- The corporate network is the air-gapped network and can reach the AWS Bedrock API.

## Builder's calls (made in this design, owner can override)

- Tag scheme `v<litellm>-r<N>`; multi-arch images; Postgres image mirrored to GHCR.
- Default user budget $50/30 days, key expiry ≤ 90 days.
- Bedrock → corporate fallbacks.
- Nightly `pg_dump` to PVC, 7-day retention.
- Manifests are applied by hand with `kubectl apply -k`. The migration Job is named per
  release (`litellm-migrate-<version>`) because a Job's pod template is immutable;
  `docs/rollout.md` gives the order: apply Job → wait for completion → apply the rest.
  Argo CD / Flux users can map the Job to a PreSync hook / sync wave (noted, not built).
- The "ingress to LiteLLM" NetworkPolicy lives in each overlay, not base, because the
  source depends on the corporate ingress setup (controller pod namespace or load
  balancer IPs). Docs state that NetworkPolicies only take effect if the cluster's
  network plugin enforces them (e.g. Calico, Cilium).
- Production AWS credential: dedicated IAM user access key with a least-privilege
  Bedrock policy, rotated every 90 days (rotation steps in `docs/operations.md`).
  Test uses a Bedrock API key.
- Prerelease detection matches `-rc` only, since every `v<litellm>-r<N>` tag contains
  a hyphen (airgaped-pr-agent's "any hyphen" rule would never move `:latest`).
- Corporate LiteLLM connectivity (unconfirmed by the owner): direct by default;
  optional `HTTPS_PROXY`/`NO_PROXY` settings and an optional mounted company CA bundle
  (`SSL_CERT_FILE`) in the overlays, documented, off unless set. Same options apply to
  the Bedrock path.

## Left to the builder

- Exact LiteLLM version and digest (latest stable at build time).
- Exact health endpoints, probe timings, resource sizes.
- Mock upstream implementation and log-scan heuristics in the offline test.
- How the migration Job invokes the image's migration step.
- Ingress manifest shape within each overlay.
