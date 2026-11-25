#!/usr/bin/env bash
# Checks the image's offline guarantees.
# Usage: tests/image_test.sh <image>
set -uo pipefail

image="${1:?usage: $0 <image>}"
fail=0

check() { # check <name> <exit-code>
  if [ "$2" -eq 0 ]; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi
}

env_list="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image")"
for want in LITELLM_OFFLINE_MODE=true LITELLM_TELEMETRY_DISABLED=true \
            LITELLM_LOCAL_MODEL_COST_MAP=True PRISMA_OFFLINE_MODE=true CHECKPOINT_DISABLE=1; do
  grep -qxF "$want" <<<"$env_list"; check "env $want" $?
done

user="$(docker inspect --format '{{.Config.User}}' "$image")"
[ "$user" = "65534" ]; check "user is 65534 (got '$user')" $?

source_label="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$image")"
[ "$source_label" = "https://github.com/abralock/airgaped-litellm" ]
check "label org.opencontainers.image.source (got '$source_label')" $?

# No config baked in. The base image ships only /app/docker/tracing-config.yaml.
configs="$(docker run --rm --entrypoint sh "$image" -c \
  'find /app -maxdepth 2 -name "*config*.y*ml" -not -path "*/.venv/*" | grep -vxF /app/docker/tracing-config.yaml')"
[ -z "$configs" ]; check "no config yaml baked into /app${configs:+ (found: $configs)}" $?

exit "$fail"
