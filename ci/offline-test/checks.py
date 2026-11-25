"""Offline end-to-end checks, run inside the tester container (stdlib only).

Env: LITELLM_MASTER_KEY, EXPECT_AUTH (bearer | sigv4),
     LITELLM_URL (default http://litellm:4000), MOCK_URL (default http://mock:8080).
Prints PASS/FAIL per check; exits 1 if any check failed.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_URL", "http://litellm:4000")
MOCK = os.environ.get("MOCK_URL", "http://mock:8080")
MASTER = os.environ["LITELLM_MASTER_KEY"]
EXPECT_AUTH = os.environ["EXPECT_AUTH"]
STARTED = time.monotonic()

MODELS = {"bedrock/claude-sonnet", "bedrock/claude-haiku", "corp/claude-sonnet", "corp/claude-haiku"}
SONNET_PROFILE = "us.anthropic.claude-sonnet-5"

state = {}


class CheckFailed(Exception):
    pass


def expect(cond, why):
    if not cond:
        raise CheckFailed(why)


def call(method, url, key=None, body=None, timeout=60):
    """Returns (status, parsed JSON or text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def chat(model, key):
    status, body = call("POST", f"{BASE}/v1/chat/completions", key, {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 16,
    })
    content = body["choices"][0]["message"]["content"] if status == 200 else None
    return status, content, body


def mock_requests():
    return call("GET", f"{MOCK}/_requests")[1]


def mock_clear():
    call("DELETE", f"{MOCK}/_requests")


def mock_fail_bedrock(on):
    call("POST", f"{MOCK}/_fail_bedrock", body={"on": on})


# --- checks -----------------------------------------------------------------

def ready_in_time():
    deadline = STARTED + 90
    last = None
    while time.monotonic() < deadline:
        try:
            last = call("GET", f"{BASE}/health/readiness", timeout=5)
            if last[0] == 200:
                return
        except OSError as err:
            last = err
        time.sleep(2)
    raise CheckFailed(f"/health/readiness not 200 within 90s (last: {last})")


def create_user_and_key():
    status, body = call("POST", f"{BASE}/user/new", MASTER, {"user_role": "internal_user"})
    expect(status == 200 and body.get("key"), f"/user/new -> {status} {body}")
    state["user_key"] = body["key"]
    status, body = call("GET", f"{BASE}/v1/models", state["user_key"])
    names = {m["id"] for m in body.get("data", [])} if status == 200 else set()
    expect(names == MODELS, f"/v1/models -> {status}, models {sorted(names)}")


def chat_via_corp():
    mock_clear()
    status, content, body = chat("corp/claude-sonnet", state["user_key"])
    expect(status == 200 and "pong from corp" in (content or ""), f"{status} {body}")
    calls = [r for r in mock_requests() if r["path"].endswith("/chat/completions")]
    expect(calls, "mock saw no chat/completions call")
    expect(calls[-1]["authorization"] == "Bearer sk-corp-test", f"corp auth was {calls[-1]['authorization']!r}")


def chat_via_bedrock():
    mock_clear()
    status, content, body = chat("bedrock/claude-sonnet", state["user_key"])
    expect(status == 200 and "pong from bedrock" in (content or ""), f"{status} {body}")
    want_path = f"/model/{SONNET_PROFILE}/converse"
    calls = [r for r in mock_requests() if r["path"] == want_path]
    expect(calls, f"mock saw no {want_path}; saw {[r['path'] for r in mock_requests()]}")
    auth = calls[-1]["authorization"] or ""
    if EXPECT_AUTH == "bearer":
        expect(auth.startswith("Bearer ABSK"), f"expected Bedrock API key bearer auth, got {auth[:40]!r}")
    else:
        expect(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIATESTONLY"), f"expected SigV4, got {auth[:40]!r}")


def fallback_to_corp():
    mock_fail_bedrock(True)
    try:
        status, content, body = chat("bedrock/claude-sonnet", state["user_key"])
        expect(status == 200 and "pong from corp" in (content or ""), f"{status} {body}")
    finally:
        mock_fail_bedrock(False)


def _expires_within_90_days(expires):
    expect(expires, "key has no expiry")
    when = dt.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    limit = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90, hours=1)
    expect(when <= limit, f"key expires {when}, later than 90 days")


def key_limits():
    status, body = call("POST", f"{BASE}/key/generate", state["user_key"], {})
    expect(status == 200, f"/key/generate {{}} -> {status} {body}")
    _expires_within_90_days(body.get("expires"))
    expect(body.get("max_budget") == 50, f"default max_budget is {body.get('max_budget')}")

    status, body = call("POST", f"{BASE}/key/generate", state["user_key"],
                        {"duration": "365d", "max_budget": 1000})
    if 400 <= status < 500:
        return
    expect(status == 200, f"/key/generate 365d -> {status} {body}")
    _expires_within_90_days(body.get("expires"))
    expect(body.get("max_budget") is not None and body["max_budget"] <= 50,
           f"over-limit key got max_budget {body.get('max_budget')}")


def unknown_model_rejected():
    mock_clear()
    for model in ("gpt-4o", "bedrock/anthropic.claude-3-opus-20240229-v1:0"):
        status, _, body = chat(model, state["user_key"])
        expect(400 <= status < 500, f"{model} -> {status} {str(body)[:200]}")
    expect(mock_requests() == [], f"upstream was called: {mock_requests()}")


def passthrough_blocked():
    """Provider pass-through routes must not let users reach models outside the config."""
    mock_clear()
    converse = {"messages": [{"role": "user", "content": [{"text": "hi"}]}]}
    for path in ("/bedrock/model/anthropic.claude-3-opus-20240229-v1:0/converse",
                 f"/bedrock/model/{SONNET_PROFILE}/converse"):
        status, body = call("POST", f"{BASE}{path}", state["user_key"], converse)
        expect(400 <= status < 500, f"{path} -> {status} {str(body)[:200]}")
    for path in ("/bedrock/knowledgebases/KB1/retrieve", "/bedrock/agents/A1/agentAliases/L1/sessions/s/text"):
        status, body = call("POST", f"{BASE}{path}", state["user_key"], converse)
        expect(not 200 <= status < 300, f"{path} -> {status} {str(body)[:200]}")
    expect(mock_requests() == [], f"pass-through reached upstream: {mock_requests()}")


EXTERNAL = re.compile(
    r"""<script[^>]+src=["'](https?://[^"']+)"""
    r"""|<link[^>]+href=["'](https?://[^"']+)"""
    r"""|@import\s+(?:url\()?["']?(https?://[^"')\s]+)"""
    r"""|url\(["']?(https?://[^"')\s]+)""",
    re.IGNORECASE,
)


def ui_offline():
    status, html = call("GET", f"{BASE}/ui/")
    expect(status == 200 and isinstance(html, str), f"/ui/ -> {status}")
    hosts = sorted({next(g for g in m.groups() if g) for m in EXTERNAL.finditer(html)})
    expect(not hosts, f"/ui/ loads external resources: {hosts}")


def _run_smoke(*args):
    import subprocess
    proc = subprocess.run(
        [sys.executable, "/scripts/smoke_test.py", "--base-url", BASE, *args],
        capture_output=True, text=True, timeout=300, env={**os.environ, "LITELLM_MASTER_KEY": MASTER},
    )
    return proc.returncode, proc.stdout + proc.stderr


def smoke_script():
    code, out = _run_smoke()
    passes = [line for line in out.splitlines() if line.startswith("PASS ")]
    expect(code == 0 and len(passes) == 4, f"exit {code}, {len(passes)} PASS lines:\n{out}")

    # A broken Bedrock path must show as FAIL, not pass silently through the corp/ fallback.
    mock_fail_bedrock(True)
    try:
        code, out = _run_smoke()
        lines = out.splitlines()
        expect(code == 1, f"expected exit 1 while Bedrock fails, got {code}:\n{out}")
        for model in ("bedrock/claude-sonnet", "bedrock/claude-haiku"):
            expect(any(line.startswith(f"FAIL {model}") for line in lines), f"no 'FAIL {model}':\n{out}")
        for model in ("corp/claude-sonnet", "corp/claude-haiku"):
            expect(any(line.startswith(f"PASS {model}") for line in lines), f"no 'PASS {model}':\n{out}")
        code, out = _run_smoke("--model", "corp/claude-sonnet", "--model", "gpt-4o")
    finally:
        mock_fail_bedrock(False)
    expect(code == 1, f"expected exit 1 with an unknown model, got {code}:\n{out}")
    expect(any(line.startswith("FAIL gpt-4o") for line in out.splitlines()), f"no 'FAIL gpt-4o' line:\n{out}")
    expect(any(line.startswith("PASS corp/claude-sonnet") for line in out.splitlines()), f"corp model did not pass:\n{out}")

    status, body = call("GET", f"{BASE}/key/list?return_full_object=true&size=100", MASTER)
    aliases = [k.get("key_alias") or "" for k in body.get("keys", [])] if status == 200 else None
    expect(aliases is not None, f"/key/list -> {status} {body}")
    leftovers = [a for a in aliases if a.startswith("smoke-test-")]
    expect(not leftovers, f"smoke test left keys behind: {leftovers}")


def no_outbound_network():
    """Runs last. LiteLLM's containers resolve names through the mock's DNS recorder, which
    answers every external name with a tarpit that accepts and never replies (a real air gap
    where DNS works but the firewall drops packets). Any lookup or connection is a failure,
    whether or not LiteLLM logged it."""
    status, seen = call("GET", f"{MOCK}/_outbound")
    expect(status == 200, f"/_outbound -> {status} {seen}")
    expect(seen == {"dns": [], "tarpit": []}, f"LiteLLM tried to reach the network: {seen}")


CHECKS = [ready_in_time, create_user_and_key, chat_via_corp, chat_via_bedrock,
          fallback_to_corp, key_limits, unknown_model_rejected, passthrough_blocked, ui_offline, smoke_script,
          no_outbound_network]


def main():
    failed = 0
    for check in CHECKS:
        try:
            check()
            print(f"PASS {check.__name__}", flush=True)
        except Exception as err:  # noqa: BLE001 - report every failure and keep going
            failed += 1
            print(f"FAIL {check.__name__}: {type(err).__name__}: {err}", flush=True)
            if check is ready_in_time:
                break
    print(f"{len(CHECKS) - failed} passed, {failed} failed" if failed == 0 else f"{failed} check(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
