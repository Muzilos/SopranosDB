#!/usr/bin/env python3
"""Upload scene keyframes to the R2 bucket for the static site.

Mirrors the on-disk layout so the front-end's URL scheme works unchanged:
    <ARTIFACTS_DIR>/<EP>/keyframes/<file>.jpg  ->  r2://<bucket>/<EP>/keyframes/<file>.jpg
which the page requests as `${keyframeBase}/<EP>/keyframes/<file>.jpg`.

Auth: derives S3 credentials from CLOUDFLARE_API_TOKEN (R2's documented method —
Access Key ID = token id, Secret = SHA-256 of the token value), so it works even
if the R2_* keys in .env are wrong. Reads R2_ENDPOINT / R2_BUCKET from env.

Resumable: lists existing objects once and skips them. Parallel uploads.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.config import Config

from sopranos.config import ARTIFACTS_DIR

WORKERS = 32


def _derive_s3_creds() -> tuple[str, str]:
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/user/tokens/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        token_id = json.load(r)["result"]["id"]
    secret = hashlib.sha256(token.encode()).hexdigest()
    return token_id, secret


def _client():
    akid, secret = _derive_s3_creds()
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=akid, aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", max_pool_connections=WORKERS + 4),
    )


def _existing_keys(s3, bucket: str) -> set[str]:
    keys: set[str] = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            keys.add(o["Key"])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def main() -> None:
    bucket = os.environ["R2_BUCKET"]
    artifacts = Path(ARTIFACTS_DIR)
    files = sorted(artifacts.glob("*/keyframes/*.jpg"))
    if not files:
        sys.exit(f"no keyframes found under {artifacts}")
    print(f"found {len(files)} keyframes under {artifacts}", flush=True)

    s3 = _client()
    print("listing existing objects in bucket…", flush=True)
    existing = _existing_keys(s3, bucket)
    print(f"{len(existing)} objects already in bucket", flush=True)

    todo = [(f, str(f.relative_to(artifacts))) for f in files]
    todo = [(f, k) for f, k in todo if k not in existing]
    print(f"{len(todo)} to upload ({len(files) - len(todo)} skipped)", flush=True)
    if not todo:
        print("nothing to do.", flush=True)
        return

    done = 0
    errors = 0

    def put(item):
        f, key = item
        s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": "image/jpeg"})
        return key

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(put, it): it for it in todo}
        for fut in as_completed(futs):
            try:
                fut.result()
                done += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 5:
                    print(f"  ERROR {futs[fut][1]}: {e}", flush=True)
            if done % 1000 == 0:
                print(f"  uploaded {done}/{len(todo)}…", flush=True)

    print(f"done: {done} uploaded, {errors} errors.", flush=True)


if __name__ == "__main__":
    main()
