# airgaped-litellm

A [LiteLLM](https://github.com/BerriAI/litellm) gateway packaged for an air-gapped
Kubernetes cluster. People log in to LiteLLM's web UI, create their own API key
(`sk-...`), and call models through one OpenAI-compatible address. Nobody but the gateway
holds AWS or upstream credentials.

The gateway reaches models two ways:

| Model names | Backend | Credential (in a Kubernetes Secret) |
| --- | --- | --- |
| `bedrock/claude-sonnet`, `bedrock/claude-haiku` | AWS Bedrock, us-east-1 | IAM access key (production) or Bedrock API key (testing) |
| `corp/claude-sonnet`, `corp/claude-haiku` | Your corporate LiteLLM | Its `sk-...` key |

If a Bedrock call fails, the same request is retried through the `corp/` twin.

> **Shared upstream key.** Every user's `corp/` traffic reaches the corporate LiteLLM under
> the single key in `CORP_LITELLM_KEY` and counts against its budget. Make sure your
> company allows this, or ask for a key issued to this service.

## What's in a release

| Artifact | Where |
| --- | --- |
| Gateway image (official LiteLLM non-root image + offline settings) | `ghcr.io/abralock/airgaped-litellm:<tag>` |
| Postgres image (pinned upstream, copied unchanged) | `ghcr.io/abralock/airgaped-litellm-postgres:<tag>` |
| Kubernetes manifests with those images pinned by digest | `airgaped-litellm-deploy-<tag>.tar.gz` on the GitHub release |

Both images are `linux/amd64` and `linux/arm64`. Tags are `v<litellm-version>-r<N>`, for
example `v1.110.3-r1`.

The image sets `LITELLM_OFFLINE_MODE=true`, `LITELLM_TELEMETRY_DISABLED=true` and
`LITELLM_LOCAL_MODEL_COST_MAP=True` and `CHECKPOINT_DISABLE=1`, so LiteLLM makes no requests of its own (price list
downloads, version checks, telemetry). CI proves this on every build by running the image
on a network with no route to the internet.

## Try it on your PC first

`local-test/` runs the published image and a Postgres with Docker on your own computer,
against real AWS Bedrock (this needs internet, unlike the real deployment):

```sh
cd local-test
cp .env.example .env      # add a Bedrock API key (ABSK...) and your own master/salt keys
docker compose up -d
LITELLM_MASTER_KEY=<your master key> python3 ../scripts/smoke_test.py --base-url http://localhost:4001
```

Then open http://localhost:4001/ui (user `admin`, password: your master key), create a key
and call the models as in [docs/users.md](docs/users.md). `docker compose down -v` removes
everything. The port is 4001, and it only listens on `127.0.0.1`.

## Docs

- [docs/rollout.md](docs/rollout.md): AWS setup, importing images into Harbor, first install and upgrades.
- [docs/operations.md](docs/operations.md): upgrades, backups and restore, rotating keys, adding users.
- [docs/users.md](docs/users.md): for the people using the gateway: get a key, call a model.

## Repository

| Path | What it is |
| --- | --- |
| `Dockerfile` | The thin layer on `ghcr.io/berriai/litellm-non_root`, pinned by digest |
| `deploy/base/config.yaml` | Models, fallbacks, user and key limits |
| `deploy/base/`, `deploy/overlays/{test,prod}/` | Kustomize manifests; `migrate/` is the database step |
| `scripts/smoke_test.py` | Checks a running gateway: one call per model with a throwaway key |
| `ci/offline-test/` | Image test on a network with no internet, against a mock corporate LiteLLM and Bedrock |
| `ci/kind-e2e.sh` | Installs the manifests on a throwaway kind cluster and tests them end to end |

Run the tests locally (Docker, kind, kubectl and Python 3 needed):

```sh
docker build -t airgaped-litellm:ci .
tests/image_test.sh airgaped-litellm:ci
ci/offline-test/run.sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q
ci/kind-e2e.sh
```

Not affiliated with BerriAI. LiteLLM is MIT-licensed; see its repository.
