import os
import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")  # e.g. https://gojpyjeappumajawhrlt.supabase.co
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = os.getenv("STORAGE_BUCKET_NAME", "knowledge-base")

# Free-tier Supabase Storage caps individual files at 50MB.
MAX_FILE_SIZE = 50 * 1024 * 1024


def _headers(content_type: str = "application/octet-stream") -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }


def upload_file(user_id: str, file_name: str, content_type: str, file_bytes: bytes) -> dict:
    """Uploads directly to Supabase Storage. Server-side, using the service role key —
    the client never sees storage credentials, only ever talks to our API."""
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(f"File exceeds {MAX_FILE_SIZE // (1024*1024)}MB limit (free-tier Supabase Storage cap)")

    key = f"raw_inputs/{user_id}/{file_name}"
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}"
    resp = httpx.post(url, headers=_headers(content_type or "application/octet-stream"), content=file_bytes, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase Storage upload failed ({resp.status_code}): {resp.text}")
    return {"file_key": key}


def download_bytes(file_key: str) -> bytes:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{file_key}"
    resp = httpx.get(url, headers=_headers(), timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Supabase Storage download failed ({resp.status_code}): {resp.text}")
    return resp.content


def presign_download_url(file_key: str, expires_in: int = 3600) -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{file_key}"
    resp = httpx.post(url, headers=_headers(), json={"expiresIn": expires_in}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to sign URL ({resp.status_code}): {resp.text}")
    return SUPABASE_URL + resp.json()["signedURL"]


def delete_file(file_key: str) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{file_key}"
    httpx.request("DELETE", url, headers=_headers(), timeout=30)
