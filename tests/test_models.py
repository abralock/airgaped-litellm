"""The Bedrock model list lives in deploy/base/config.yaml; everything else must follow it."""
import re

import yaml

from conftest import ROOT

BASE = yaml.safe_load((ROOT / "deploy/base/config.yaml").read_text())


def bedrock_models(config):
    return {m["model_name"]: m["litellm_params"]["model"].removeprefix("bedrock/")
            for m in config["model_list"] if m["model_name"].startswith("bedrock/")}


def test_sonnet_is_sonnet_5():
    assert bedrock_models(BASE)["bedrock/claude-sonnet"] == "us.anthropic.claude-sonnet-5"


def test_iam_policy_covers_every_bedrock_model():
    rollout = (ROOT / "docs/rollout.md").read_text()
    for profile in bedrock_models(BASE).values():
        base_model = profile.split(".", 1)[1]  # us.anthropic.x -> anthropic.x
        assert f"inference-profile/{profile}\"" in rollout, profile
        assert f"foundation-model/{base_model}\"" in rollout, base_model


def test_local_test_uses_the_same_bedrock_models():
    local = yaml.safe_load((ROOT / "local-test/config.yaml").read_text())
    assert bedrock_models(local) == bedrock_models(BASE)
    assert local["litellm_settings"] == BASE["litellm_settings"]


def test_local_test_image_matches_the_dockerfile():
    version = re.search(r"^ARG LITELLM_VERSION=(\S+)", (ROOT / "Dockerfile").read_text(), re.M).group(1)
    compose = yaml.safe_load((ROOT / "local-test/compose.yaml").read_text())
    images = {svc["image"] for name, svc in compose["services"].items() if name != "postgres"}
    assert len(images) == 1
    (image,) = images
    assert re.fullmatch(rf"ghcr\.io/abralock/airgaped-litellm:{re.escape(version)}-r\d+", image), image


def test_local_env_is_not_committed():
    assert "local-test/.env" in (ROOT / ".gitignore").read_text().splitlines()
