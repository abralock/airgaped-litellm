#!/usr/bin/env python3
"""Smoke test a running gateway: one short chat per model, with a throwaway key.

Usage:
  LITELLM_MASTER_KEY=sk-... python3 scripts/smoke_test.py --base-url https://litellm.example.internal
  ... --model bedrock/claude-sonnet --model corp/claude-sonnet   # only these models
  ... --insecure                                                 # skip TLS verification

Creates a key (1 hour, $1 budget), calls each model with "Reply with the single word: pong"
(fallbacks disabled, so each route is tested on its own), prints PASS/FAIL per model, and
always deletes the key. Exit 1 if any model failed.
Uses only the Python standard library; honours SSL_CERT_FILE for a company CA.
"""
import argparse
import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


def make_caller(base_url, insecure):
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context(
        cafile=os.environ.get("SSL_CERT_FILE") or None)

    def call(method, path, key, body=None, timeout=120):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        text = raw.decode("utf-8", "replace")
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    return call


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", action="append", default=[], help="model to test (repeatable)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    args = parser.parse_args(argv)

    master = os.environ.get("LITELLM_MASTER_KEY")
    if not master:
        print("LITELLM_MASTER_KEY is not set", file=sys.stderr)
        return 2
    call = make_caller(args.base_url, args.insecure)

    alias = "smoke-test-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    status, body = call("POST", "/key/generate", master,
                        {"key_alias": alias, "duration": "1h", "max_budget": 1})
    if status != 200 or not isinstance(body, dict) or not body.get("key"):
        print(f"FAIL create-key {status} {str(body)[:200]}")
        return 1
    key = body["key"]

    failed = 0
    try:
        models = args.model
        if not models:
            status, body = call("GET", "/v1/models", key)
            models = [m["id"] for m in body.get("data", [])] if status == 200 else []
            if not models:
                print(f"FAIL list-models {status} {str(body)[:200]}")
                return 1
        for model in models:
            started = time.monotonic()
            status, body = call("POST", "/v1/chat/completions", key, {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                "max_tokens": 16,
                # Test each route on its own: a failing bedrock/ model must not pass via its corp/ fallback.
                "disable_fallbacks": True,
            })
            latency_ms = int((time.monotonic() - started) * 1000)
            if status == 200 and isinstance(body, dict) and body.get("choices"):
                print(f"PASS {model} {latency_ms}ms")
            else:
                failed += 1
                print(f"FAIL {model} {status} {json.dumps(body)[:200] if not isinstance(body, str) else body[:200]}")
    finally:
        status, body = call("POST", "/key/delete", master, {"keys": [key]})
        if status != 200:
            print(f"WARN could not delete key {alias}: {status} {str(body)[:200]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
