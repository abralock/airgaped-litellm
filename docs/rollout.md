# Rollout

How to install the gateway on your corporate Kubernetes cluster, and how to roll out a new
release. The `test` and `prod` overlays work the same way; the examples use `prod` and the
namespace `litellm-prod` (`litellm-test` for test).

Order for every release, including the first: **AWS (once) → import images → unpack the
bundle → fill in placeholders → Secrets (once) → database step → gateway → smoke test.**

## 1. AWS prerequisites (once)

1. In the AWS console, **Amazon Bedrock → Model access** in **us-east-1**: make sure the
   Claude models in `deploy/base/config.yaml` are enabled for your account. The first
   time Anthropic models are used, an account admin may need to submit the use-case form.
2. Create the credential LiteLLM will use:
   - **Production: an IAM user with an access key.** Create a dedicated IAM user (e.g.
     `litellm-gateway`) with no console access and attach this policy. The `us.`
     inference profiles route requests to several US regions, so the foundation-model
     resources use `*` for the region.

     ```json
     {
       "Version": "2012-10-17",
       "Statement": [
         {
           "Sid": "InvokeClaudeThroughUsProfiles",
           "Effect": "Allow",
           "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
           "Resource": [
             "arn:aws:bedrock:us-east-1:REPLACE_ME_AWS_ACCOUNT_ID:inference-profile/us.anthropic.claude-sonnet-5",
             "arn:aws:bedrock:us-east-1:REPLACE_ME_AWS_ACCOUNT_ID:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
             "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
             "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
           ]
         }
       ]
     }
     ```

     If you change the models in `config.yaml`, change the policy to match. Then create
     an access key for the user. Rotate it every 90 days (see operations.md).
   - **Testing: a Bedrock API key.** **Amazon Bedrock → API keys → Generate long-term
     API key**, with an expiry of 30 days or less. It starts with `ABSK`.
3. Make sure the cluster can reach `https://bedrock-runtime.us-east-1.amazonaws.com`
   (directly, or through your proxy: see step 4).

## 2. Import the images into Harbor

Each GitHub release lists both images by digest. The deploy bundle pins those exact
digests, so import the images **with all platforms and digests unchanged**. With
[skopeo](https://github.com/containers/skopeo), copy each image to a local directory,
scan it there, then push it to your Harbor project:

```sh
TAG=v1.110.3-r1
HARBOR=harbor.example.internal/platform       # your Harbor project
LITELLM_DIGEST=sha256:<from the release notes>
POSTGRES_DIGEST=sha256:<from the release notes>

skopeo copy --all --preserve-digests \
  docker://ghcr.io/abralock/airgaped-litellm@$LITELLM_DIGEST oci:airgaped-litellm:$TAG
skopeo copy --all --preserve-digests \
  docker://ghcr.io/abralock/airgaped-litellm-postgres@$POSTGRES_DIGEST oci:airgaped-litellm-postgres:$TAG

# ... virus / vulnerability scan the two OCI directories here
#     (e.g. trivy image --input airgaped-litellm) ...

skopeo copy --all --preserve-digests oci:airgaped-litellm:$TAG docker://$HARBOR/airgaped-litellm:$TAG
skopeo copy --all --preserve-digests oci:airgaped-litellm-postgres:$TAG docker://$HARBOR/airgaped-litellm-postgres:$TAG
```

Check the digest Harbor shows for each image matches the release notes. If your process
only allows `docker pull` / `docker push`, that imports one platform, Harbor gives the
image a new digest, and you must re-pin it (see step 4).

The images on `ghcr.io/abralock` are public. If a pull says `denied`, the package hasn't
been made public yet (the project owner does this once on GitHub: **Package settings →
Change visibility → Public**).

## 3. Unpack and verify the deploy bundle

Download `airgaped-litellm-deploy-<tag>.tar.gz` and its `.sha256` from the GitHub release:

```sh
sha256sum -c airgaped-litellm-deploy-$TAG.tar.gz.sha256
tar -xzf airgaped-litellm-deploy-$TAG.tar.gz      # creates deploy/ and scripts/smoke_test.py
```

Keep your filled-in copy of `deploy/` (step 4) under version control on your side. On
the next release, unpack the new bundle and carry your values over.

## 4. Fill in the placeholders

Every value that depends on your environment is a `REPLACE_ME_*` placeholder:

| Placeholder | File(s) | What goes there |
| --- | --- | --- |
| `REPLACE_ME_HARBOR` | `deploy/overlays/<env>/kustomization.yaml`, `deploy/overlays/<env>/migrate/kustomization.yaml` | Your Harbor project, e.g. `harbor.example.internal/platform` |
| `REPLACE_ME_HOSTNAME` | `deploy/overlays/<env>/ingress-patch.yaml` | The gateway's hostname, e.g. `litellm.example.internal` |
| `REPLACE_ME_INGRESS_CLASS` | `deploy/overlays/<env>/ingress-patch.yaml` | Your ingress class, e.g. `nginx` |
| `REPLACE_ME_TLS_SECRET` | `deploy/overlays/<env>/ingress-patch.yaml` | A TLS Secret for the hostname in the namespace |
| `REPLACE_ME_INGRESS_NAMESPACE` | `deploy/overlays/<env>/networkpolicies.yaml` | The namespace your ingress controller runs in |
| `REPLACE_ME_BEDROCK_EGRESS_CIDR` | `deploy/overlays/<env>/networkpolicies.yaml` | Where the gateway's packets to AWS are addressed (the rule matches the destination, before any NAT): your HTTP proxy's address (and change port `443` to the proxy's port), a Bedrock interface endpoint's addresses over VPN / Direct Connect, or `0.0.0.0/0` if pods go straight out |
| `REPLACE_ME_CORP_LITELLM_CIDR` | `deploy/overlays/<env>/networkpolicies.yaml` | The corporate LiteLLM's address range (change port `443` there if needed) |
| `REPLACE_ME_CORP_MODEL_SONNET`, `REPLACE_ME_CORP_MODEL_HAIKU` | `deploy/base/config.yaml` | Model names on the corporate LiteLLM (`GET <CORP_LITELLM_URL>/v1/models`) |
| `REPLACE_ME_HTTPS_PROXY` | `deploy/overlays/prod/proxy-ca-patch.yaml` (optional) | Only if you enable the proxy patch below |
| `REPLACE_ME_AWS_ACCOUNT_ID` | the IAM policy in step 1 (not a file) | Your AWS account ID |

Also check the Bedrock model IDs in `deploy/base/config.yaml` against what is enabled in
your account (**Bedrock → Cross-region inference**).

If you imported with `docker pull` / `docker push` instead of `skopeo copy --all` (step 2),
replace the `digest:` values in `deploy/base/kustomization.yaml` and
`deploy/base/migrate/kustomization.yaml` (one per image, in both files) with the digests
Harbor gave them:

```sh
docker buildx imagetools inspect "$HARBOR/airgaped-litellm:$TAG" --format '{{json .Manifest}}' | jq -r .digest
docker buildx imagetools inspect "$HARBOR/airgaped-litellm-postgres:$TAG" --format '{{json .Manifest}}' | jq -r .digest
```

**Optional, proxy or company CA:** if LiteLLM must reach Bedrock or the corporate
LiteLLM through an HTTP proxy, or they use a certificate from your company's CA, follow
the instructions at the top of `deploy/overlays/prod/proxy-ca-patch.yaml` (copy it into
the `test` overlay too if needed). With `HTTPS_PROXY` set, traffic to the corporate
LiteLLM also goes through the proxy unless its host is in `NO_PROXY`. In that case its
NetworkPolicy rule needs the proxy's address too. The CA file must contain the public
roots as well as your company CA; the patch shows how to build it.

**Check:** both commands must print nothing.

```sh
kubectl kustomize deploy/overlays/prod | grep -n REPLACE_ME
kubectl kustomize deploy/overlays/prod/migrate | grep -n REPLACE_ME
```

## 5. Create the namespace and Secrets (once per environment)

Secrets are never in the bundle. Create them by hand. Keep the generated values in your
password manager.

```sh
NS=litellm-prod
kubectl create namespace "$NS"

PG_PASSWORD="$(openssl rand -hex 24)"
MASTER_KEY="sk-$(openssl rand -hex 24)"      # admin key; must start with sk-
SALT_KEY="$(openssl rand -hex 32)"           # encrypts stored credentials: NEVER change it later

kubectl -n "$NS" create secret generic litellm-postgres \
  --from-literal=POSTGRES_PASSWORD="$PG_PASSWORD"

kubectl -n "$NS" create secret generic litellm-secrets \
  --from-literal=LITELLM_MASTER_KEY="$MASTER_KEY" \
  --from-literal=LITELLM_SALT_KEY="$SALT_KEY" \
  --from-literal=DATABASE_URL="postgresql://litellm:$PG_PASSWORD@litellm-postgres:5432/litellm" \
  --from-literal=CORP_LITELLM_URL="https://<corporate litellm address>" \
  --from-literal=CORP_LITELLM_KEY="sk-<your corporate key>" \
  --from-literal=AWS_ACCESS_KEY_ID="<from step 1>" \
  --from-literal=AWS_SECRET_ACCESS_KEY="<from step 1>"
```

For `test`, use `NS=litellm-test` and replace the two `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` lines with
`--from-literal=AWS_BEARER_TOKEN_BEDROCK="ABSK..."`. LiteLLM uses whichever credential is
present. Don't set both.

## 6. Database step, then the gateway

The `migrate/` kustomization makes sure Postgres exists and runs the database migrations
once (`litellm-migrate-<tag>`). The gateway pods never migrate (`DISABLE_SCHEMA_UPDATE=true`),
so apply it first and wait for it on every release:

```sh
kubectl apply -k deploy/overlays/prod/migrate
kubectl -n litellm-prod wait --for=condition=complete job/litellm-migrate-$TAG --timeout=15m

kubectl apply -k deploy/overlays/prod
kubectl -n litellm-prod rollout status deploy/litellm --timeout=10m
```

If the Job fails, read `kubectl -n litellm-prod logs job/litellm-migrate-$TAG` and don't
apply the gateway. The pods of the previous release keep serving. Finished Jobs are
deleted after a day.

If you use Argo CD or Flux instead of `kubectl`, make the `migrate/` kustomization a
PreSync hook or an earlier sync wave.

## 7. Smoke test

From a machine that can reach the gateway's hostname:

```sh
LITELLM_MASTER_KEY="$MASTER_KEY" python3 scripts/smoke_test.py --base-url https://litellm.example.internal
```

`scripts/smoke_test.py` comes with the deploy bundle (Python 3, no dependencies). It
creates a throwaway key, calls every model once, prints `PASS` or `FAIL` per model, and
deletes the key. If the gateway's certificate comes from your company's CA, set
`SSL_CERT_FILE` to the CA bundle. A `FAIL` on `corp/...` usually means a wrong
`CORP_LITELLM_URL`, key or model name. A `FAIL` on `bedrock/...` usually means a wrong AWS
credential, model not enabled, or blocked egress.

## 8. First admin login (first install only)

1. Open `https://<hostname>/ui` and log in with username `admin` and the master key as
   the password.
2. **Internal Users → Invite User**: create a named user for yourself with role
   **Admin** (`proxy_admin`), open the invite link, and set a password.
3. Turn off the shared master-key login: add `disable_env_credential_login: true` under
   `general_settings:` in `deploy/base/config.yaml` and apply the overlay again. Do this
   only after your named admin can log in. If you lock yourself out, remove the line and
   apply again.

Adding users: see operations.md.

## About the network rules

The NetworkPolicies limit the gateway to Postgres, DNS, the Bedrock egress range and the
corporate LiteLLM, and only Postgres's own clients can reach Postgres. **They only take
effect if your cluster's network plugin enforces NetworkPolicy** (Calico, Cilium and
others do; some basic plugins do not). Ask your cluster team if you're not sure.
