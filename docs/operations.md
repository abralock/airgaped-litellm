# Operations

Examples use the `prod` namespace `litellm-prod`; use `litellm-test` for test.

## Adding a user

1. Log in to `https://<hostname>/ui` as an admin.
2. **Internal Users → Invite User**, enter their email, role **Internal User**.
3. Send them the invite link LiteLLM shows. They set a password, log in, and create their
   own keys. [users.md](users.md) is the page to send along.

New users get role `internal_user`: they can create, view and delete their own keys and
see their own spend, but can't add users. Remove a user from the same page. Their keys
stop working immediately.

## Limits

They live in `deploy/base/config.yaml` under `litellm_settings`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `default_internal_user_params.max_budget` / `budget_duration` | `50` / `30d` | Each new user's spend limit (USD) and how often it resets |
| `default_key_generate_params` | `duration: 90d`, `max_budget: 50` | Applied to keys created without limits |
| `upperbound_key_generate_params` | `duration: 90d`, `max_budget: 50` | The most any self-service key may get |

Spend is estimated from LiteLLM's bundled price list. The image doesn't download price
updates (that would need internet), so new models may show zero cost until you upgrade
LiteLLM.

After editing `config.yaml`, apply the overlay again. The ConfigMap's name changes with its
content, so the pods restart with the new config automatically.

## Upgrading LiteLLM

1. Pick the new version on [LiteLLM's releases](https://github.com/BerriAI/litellm/releases)
   and get the digest of the official non-root image:
   `docker buildx imagetools inspect ghcr.io/berriai/litellm-non_root:<version>`.
2. In `Dockerfile`, change `ARG LITELLM_VERSION=` and the `@sha256:` digest together.
3. Run the tests locally or let CI run them (README), then tag the release:
   `git tag v<version>-r1 && git push origin v<version>-r1`. For a release candidate use
   `-rc1`, which doesn't move `:latest`.
4. Roll out from [rollout.md](rollout.md) step 2. The database step migrates the schema
   before the new gateway pods start.

To ship a fix without a LiteLLM upgrade (for example a manifest change), tag the same
version with the next revision: `v1.110.3-r2`.

## Backups and restore

`litellm-backup` runs every night at 02:00 UTC. It writes
`/backups/litellm-<timestamp>.dump` (`pg_dump -Fc`) to the `litellm-backups` volume and
deletes dumps older than 7 days. The volume is in the same cluster: for disaster recovery,
also copy the dumps somewhere else with your usual backup tooling.

Run a backup now:

```sh
kubectl -n litellm-prod create job --from=cronjob/litellm-backup backup-manual-$(date +%s)
```

Restore a dump (this replaces the current users, keys and spend data):

```sh
kubectl -n litellm-prod scale deploy/litellm --replicas=0
kubectl -n litellm-prod apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: litellm-restore
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: litellm-backup   # allowed through the Postgres NetworkPolicy
    spec:
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, runAsUser: 70}
      containers:
        - name: restore
          image: REPLACE_WITH_YOUR_HARBOR/airgaped-litellm-postgres:<tag>
          command: ["/bin/sh", "-ec"]
          args:
            - pg_restore --clean --if-exists -h litellm-postgres -U litellm -d litellm /backups/<file>.dump
          env:
            - name: PGPASSWORD
              valueFrom: {secretKeyRef: {name: litellm-postgres, key: POSTGRES_PASSWORD}}
          volumeMounts: [{name: backups, mountPath: /backups}]
      volumes:
        - name: backups
          persistentVolumeClaim: {claimName: litellm-backups}
EOF
kubectl -n litellm-prod wait --for=condition=complete job/litellm-restore --timeout=15m
kubectl -n litellm-prod logs job/litellm-restore
kubectl -n litellm-prod delete job litellm-restore
kubectl -n litellm-prod scale deploy/litellm --replicas=2
```

List the available dumps with
`kubectl -n litellm-prod logs job/<latest backup job>`. Every backup run prints the
directory listing.

## Rotating credentials

Update one key in the Secret without touching the others, then restart the gateway:

```sh
kubectl -n litellm-prod patch secret litellm-secrets --type merge \
  -p '{"stringData":{"CORP_LITELLM_KEY":"sk-<new key>"}}'
kubectl -n litellm-prod rollout restart deploy/litellm
kubectl -n litellm-prod rollout status deploy/litellm
```

- **AWS access key (every 90 days):** create a second access key for the IAM user, patch
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, restart, run the smoke test, then
  deactivate and delete the old key in IAM.
- **Bedrock API key (test):** generate a new one before the old one expires, patch
  `AWS_BEARER_TOKEN_BEDROCK`, restart.
- **Corporate LiteLLM key:** patch `CORP_LITELLM_KEY`, restart.
- **Master key:** patch `LITELLM_MASTER_KEY` (must start with `sk-`), restart. Users' keys
  keep working.
- **`LITELLM_SALT_KEY`: never change it.** It encrypts credentials stored in the database.
  Changing it makes them unreadable.
- **Postgres password:** change it in Postgres first
  (`ALTER USER litellm PASSWORD '...'`), then patch both `litellm-postgres` and the
  password inside `DATABASE_URL` in `litellm-secrets`, then restart.

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| Gateway pods not ready | `kubectl -n litellm-prod logs deploy/litellm`. Is the database step for this release complete? |
| Migration Job fails | `kubectl -n litellm-prod logs job/litellm-migrate-<tag>`. Postgres running? `DATABASE_URL` correct? |
| `bedrock/...` errors, `corp/...` works | AWS credential, model access in us-east-1, egress to Bedrock (NetworkPolicy CIDR, proxy) |
| `corp/...` errors | `CORP_LITELLM_URL` / `CORP_LITELLM_KEY`, the corporate model names in `config.yaml`, NetworkPolicy CIDR/port |
| TLS errors to an upstream | Company CA: enable `proxy-ca-patch.yaml` (rollout.md step 4) |
