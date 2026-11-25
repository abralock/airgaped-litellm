"""kind e2e: every key ends up with an expiry of at most 90 days (stdlib only).

  python3 ci/expiry_check.py make   --base-url URL --state FILE   # create keys LiteLLM leaves without expiry
  python3 ci/expiry_check.py verify --base-url URL --state FILE   # after the litellm-key-expiry job ran
Master key from LITELLM_MASTER_KEY.
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request


def call(base, method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def make(base, master):
    status, user = call(base, "POST", "/user/new", master, {"user_role": "internal_user"})
    assert status == 200, (status, user)
    keys = {"user_new_auto_key": user["key"]}
    status, gen = call(base, "POST", "/key/generate", user["key"], {})
    assert status == 200, (status, gen)
    status, upd = call(base, "POST", "/key/update", user["key"], {"key": gen["key"], "duration": None})
    print(f"/key/update duration=null by the owner -> {status}")
    keys["updated_to_no_expiry"] = gen["key"]
    return keys


def verify(base, master, keys):
    limit = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90, hours=1)
    failed = 0
    for name, key in keys.items():
        status, info = call(base, "GET", f"/key/info?key={key}", master)
        expires = (info.get("info") or {}).get("expires") if status == 200 else None
        when = dt.datetime.fromisoformat(str(expires).replace("Z", "+00:00")) if expires else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if when is None or when > limit:
            failed += 1
            print(f"FAIL {name}: expires={expires}")
        else:
            print(f"PASS {name}: expires={expires}")
    return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["make", "verify"])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    master = os.environ["LITELLM_MASTER_KEY"]
    if args.phase == "make":
        with open(args.state, "w") as fh:
            json.dump(make(args.base_url, master), fh)
        return 0
    with open(args.state) as fh:
        return 1 if verify(args.base_url, master, json.load(fh)) else 0


if __name__ == "__main__":
    sys.exit(main())
