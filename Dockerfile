# Thin layer on the official LiteLLM non-root image: offline by default.
# Upgrade: change LITELLM_VERSION and the digest together, then tag v<version>-r1.
ARG LITELLM_VERSION=v1.110.3
FROM ghcr.io/berriai/litellm-non_root:${LITELLM_VERSION}@sha256:d9790b3eba82a99bd074f26ceb0f471385365115f460f04f1f19d62e2f11461b

# Stop every request LiteLLM makes on its own (price map, release check,
# telemetry, tokenizer downloads). Must be env, not config.yaml.
# CHECKPOINT_DISABLE stops the Prisma CLI's update check (checkpoint.prisma.io),
# which runs during database migrations.
ENV LITELLM_OFFLINE_MODE=true \
    LITELLM_TELEMETRY_DISABLED=true \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    CHECKPOINT_DISABLE=1

LABEL org.opencontainers.image.source="https://github.com/abralock/airgaped-litellm" \
      org.opencontainers.image.description="LiteLLM gateway for air-gapped Kubernetes (offline by default)" \
      org.opencontainers.image.licenses="MIT"
