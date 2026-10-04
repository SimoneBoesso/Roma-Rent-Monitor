"""S3/R2 helpers for durable OMI ingest (Render → object storage → CI)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

INGEST_PREFIX = "omi-ingest"
MANIFEST_KEY = f"{INGEST_PREFIX}/manifest.json"

SIGHTINGS_PREFIX = "sightings-inbox"
SIGHTINGS_LIST = f"{SIGHTINGS_PREFIX}/sightings.jsonl"
SEEN_SIGHTINGS_LIST = f"{SIGHTINGS_PREFIX}/seen.json"

def cloud_configured() -> bool:
    """True when S3/R2 credentials are present (bucket defaults to rent-tracker-data)."""
    has_key = bool(os.environ.get("AWS_ACCESS_KEY_ID", "").strip())
    has_secret = bool(os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip())
    logger.debug(f"cloud_configured: AWS_ACCESS_KEY_ID={'set' if has_key else 'NOT SET'}, AWS_SECRET_ACCESS_KEY={'set' if has_secret else 'NOT SET'}")
    return has_key and has_secret


def _bucket() -> str:
    bucket = (
        os.environ.get("INGEST_S3_BUCKET", "").strip()
        or os.environ.get("AWS_S3_BUCKET", "").strip()
        or "rent-tracker-data"
    )
    logger.debug(f"_bucket: using bucket={bucket}")
    return bucket


def _client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 required for cloud ingest — pip install boto3"
        ) from exc

    kwargs: dict[str, Any] = {
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"].strip(),
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"].strip(),
    }
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    region = os.environ.get("AWS_DEFAULT_REGION", "").strip() or "auto"
    if endpoint:
        kwargs["endpoint_url"] = endpoint
        kwargs["region_name"] = region
        logger.debug(f"_client: using endpoint={endpoint}, region={region}")
    else:
        logger.debug(f"_client: no endpoint URL set, using AWS default (region={region})")
    return boto3.client("s3", **kwargs)

# path for a file in the bucket
def object_key(ingest_prefix: str, digest: str, filename: str) -> str:
    return f"{ingest_prefix}/files/{digest}/{filename}"


def load_remote_manifest() -> dict[str, Any]:
    client = _client()
    bucket = _bucket()
    try:
        obj = client.get_object(Bucket=bucket, Key=MANIFEST_KEY)
        body = obj["Body"].read().decode("utf-8")
        data = json.loads(body)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {"version": 1, "files": {}}
        raise
    if not isinstance(data, dict):
        return {"version": 1, "files": {}}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    return data


def save_remote_manifest(manifest: dict[str, Any]) -> None:
    client = _client()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    client.put_object(
        Bucket=_bucket(),
        Key=MANIFEST_KEY,
        Body=payload.encode("utf-8"),
        ContentType="application/json",
    )


def append_remote_sighting_line(record: dict[str, Any]) -> None:
    client = _client()
    bucket = _bucket()
    try:
        obj = client.get_object(Bucket=bucket, Key=SIGHTINGS_LIST)
        body = obj["Body"].read() # read the file as bytes
    except ClientError as exc:
        # for the first time, the file sightings.jsonl will not exist
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            body = b""
        else:
            raise
    # generate the line for the json file    
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    client.put_object(Bucket=bucket, Key=SIGHTINGS_LIST, Body=body + line, ContentType="application/x-ndjson")
    

def load_remote_seen_sightings() -> list[dict[str, Any]]:   
    client = _client()
    bucket = _bucket()
    try:
        obj = client.get_object(Bucket=bucket, Key=SEEN_SIGHTINGS_LIST)
        body = obj["Body"].read() # read the file as bytes
        data = json.loads(body)

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {"version": 1, "digests": {}}
        raise
    return data

def save_remote_seen_sightings(digests: dict[str, Any]) -> None:
    client = _client()
    payload = json.dumps(digests, ensure_ascii=False, indent=2) + "\n"
    client.put_object(
        Bucket=_bucket(),
        Key=SEEN_SIGHTINGS_LIST,
        Body=payload.encode("utf-8"),
        ContentType="application/json",
    )

def upload_csv(*, digest: str, filename: str, content: bytes) -> str:
    key = object_key("omi-ingest", digest, filename)
    client = _client()
    client.put_object(
        Bucket=_bucket(),
        Key=key,
        Body=content,
        ContentType="text/csv",
    )
    logger.info("Uploaded ingest object s3://%s/%s", _bucket(), key)
    return key

from pathlib import Path


def download_sightings_jsonl(dest: Path) -> bool:

    if not cloud_configured():
        logger.warning("Cloud ingest not configured — skip sightings pull")
        return False
    client = _client()
    bucket = _bucket()
    try:
        obj = client.get_object(Bucket=bucket, Key=SIGHTINGS_LIST)
        body = obj["Body"].read() # read the file as bytes
    except ClientError as exc:
        # for the first time, the file sightings.jsonl will not exist
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            body = b""
            return False
        else:
            raise
    if not body:
        logger.warning("Empty sightings object at s3://%s/%s", bucket, SIGHTINGS_LIST)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return True


def download_inbox(raw_dir) -> list[str]:
    """Download all CSV objects listed in the remote manifest into raw_dir.

    Skips hashes already present as local files with the same content name.
    Returns list of filenames written.
    """

    raw = Path(raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    if not cloud_configured():
        logger.warning("Cloud ingest not configured — skip inbox pull")
        return []

    manifest = load_remote_manifest()
    files = manifest.get("files") or {}
    client = _client()
    bucket = _bucket()
    written: list[str] = []

    for digest, meta in files.items():
        if not isinstance(meta, dict):
            continue
        filename = str(meta.get("filename") or "")
        key = str(meta.get("key") or object_key("omi-ingest", digest, filename))
        if not filename or not filename.lower().endswith(".csv"):
            continue
        dest = raw / filename
        if dest.is_file():
            # Same name already on disk (e.g. from dvc pull) — keep existing
            continue
        obj = client.get_object(Bucket=bucket, Key=key)
        dest.write_bytes(obj["Body"].read())
        written.append(filename)
        logger.info("Inbox → %s", dest)
    return written


def read_dvc_md5(dvc_path) -> str | None:
    """Parse md5 from a DVC pointer file (``*.dvc``)."""
    from pathlib import Path

    path = Path(dvc_path)
    if not path.is_file():
        logger.warning(f"DVC pointer file not found: {path}")
        return None
    content = path.read_text(encoding="utf-8")
    logger.debug(f"read_dvc_md5: reading {path}\n{content}")
    for line in content.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith("md5:"):
            digest = stripped.split(":", 1)[1].strip()
            logger.debug(f"read_dvc_md5: found md5={digest}")
            return digest or None
    logger.warning(f"read_dvc_md5: no md5 found in {path}")
    return None


def dvc_cache_key(md5: str, remote_prefix: str = "dvc") -> str:
    """Object key for a DVC md5 cache entry under the remote prefix."""
    digest = md5.strip().lower()
    prefix = remote_prefix.strip().strip("/") or "dvc"
    key = f"{prefix}/files/md5/{digest[:2]}/{digest[2:]}"
    logger.debug(f"dvc_cache_key: md5={md5} → key={key}")
    return key


def ensure_features_latest(
    dest,
    dvc_path=None,
    *,
    remote_prefix: str = "dvc",
) -> bool:
    """Ensure ``features_latest.jsonl`` exists locally; pull from R2/DVC if needed.

    Returns True when the file is present after the call.
    Uses the same AWS_* credentials as ingest (Render already has them for DVC).
    """
    from pathlib import Path

    out = Path(dest)
    logger.info(f"ensure_features_latest: checking {out}")
    
    if out.is_file() and out.stat().st_size > 0:
        logger.info(f"ensure_features_latest: file exists and non-empty ({out.stat().st_size} bytes), skipping fetch")
        return True

    pointer = Path(dvc_path) if dvc_path is not None else Path(str(out) + ".dvc")
    logger.debug(f"ensure_features_latest: using pointer {pointer}")
    
    md5 = read_dvc_md5(pointer)
    if not md5:
        logger.error(f"ensure_features_latest: FAILED - No DVC md5 at {pointer} — cannot fetch features")
        return False
    
    if not cloud_configured():
        logger.error(f"ensure_features_latest: FAILED - Cloud not configured (AWS credentials missing) — cannot fetch features from R2")
        return False

    key = dvc_cache_key(md5, remote_prefix=remote_prefix)
    client = _client()
    bucket = _bucket()
    logger.info(f"ensure_features_latest: attempting to fetch s3://{bucket}/{key}")
    
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        payload = obj["Body"].read()
    except ClientError as exc:
        logger.error(f"ensure_features_latest: FAILED - Could not download s3://{bucket}/{key}: {exc}")
        return False
    
    if not payload:
        logger.error(f"ensure_features_latest: FAILED - Empty features object at s3://{bucket}/{key}")
        return False

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    logger.info(f"ensure_features_latest: SUCCESS - Features ← s3://{bucket}/{key} → {out} ({len(payload)} bytes)")
    return True
