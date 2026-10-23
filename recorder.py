import asyncio
import gzip
import json
import logging
import os
import sys
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config import settings

# Configure standard root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("mcp-proxy")

# Optional GCS Client initialization
_gcs_storage_client = None
_gcs_bucket = None

def get_gcs_bucket():
    global _gcs_storage_client, _gcs_bucket
    if not settings.gcs_log_bucket:
        return None
    if _gcs_bucket is None:
        try:
            from google.cloud import storage
            _gcs_storage_client = storage.Client()
            _gcs_bucket = _gcs_storage_client.bucket(settings.gcs_log_bucket)
            logger.info(json.dumps({
                "severity": "INFO",
                "message": f"Initialized GCS logging to bucket: {settings.gcs_log_bucket}"
            }))
        except Exception as e:
            logger.error(json.dumps({
                "severity": "ERROR",
                "message": f"Failed to initialize GCS storage client: {e}"
            }))
            return None
    return _gcs_bucket

def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Sanitizes sensitive headers if redact_secrets is enabled."""
    if not settings.redact_secrets:
        return headers
    
    redacted = {}
    sensitive_keys = {"authorization", "cookie", "set-cookie", "x-api-key"}
    for k, v in headers.items():
        if k.lower() in sensitive_keys:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted

def decompress_body_safely(raw_bytes: bytes, encoding: Optional[str] = None) -> bytes:
    """Decompresses raw bytes if compressed with gzip or deflate, for logging readability."""
    if not raw_bytes:
        return raw_bytes
    enc = (encoding or "").lower().strip()
    if enc == "gzip" or raw_bytes.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(raw_bytes)
        except Exception:
            pass
    elif enc in ("deflate", "zlib"):
        try:
            return zlib.decompress(raw_bytes)
        except Exception:
            pass
    return raw_bytes

def parse_body_safely(raw_bytes: bytes, max_bytes: int) -> Any:
    """Decodes body as JSON if possible, else returns truncated text or metadata."""
    if not raw_bytes:
        return None
    
    if len(raw_bytes) > max_bytes:
        return f"[TRUNCATED: payload size {len(raw_bytes)} bytes exceeds limit {max_bytes}]"
    
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except Exception:
            return text
    except Exception as e:
        return f"[BINARY DATA: {len(raw_bytes)} bytes]"

def extract_mcp_rpc_meta(payload: Any) -> Optional[Dict[str, Any]]:
    """Extracts JSON-RPC metadata if payload represents an MCP RPC call or response."""
    if not isinstance(payload, dict):
        return None
    meta = {}
    if "jsonrpc" in payload:
        meta["jsonrpc"] = payload.get("jsonrpc")
    if "id" in payload:
        meta["id"] = payload.get("id")
    if "method" in payload:
        meta["method"] = payload.get("method")
    if "error" in payload:
        meta["is_error"] = True
        meta["error_code"] = payload["error"].get("code") if isinstance(payload["error"], dict) else None
    if "result" in payload:
        meta["has_result"] = True
    return meta if meta else None

def _write_to_gcs_sync(blob_name: str, data: str):
    """Synchronous worker function to upload to GCS."""
    bucket = get_gcs_bucket()
    if not bucket:
        return
    try:
        blob = bucket.blob(blob_name)
        blob.upload_from_string(data, content_type="application/json")
    except Exception as e:
        logger.error(json.dumps({
            "severity": "WARNING",
            "message": f"Failed to upload dump to GCS: {e}",
            "blob_name": blob_name
        }))

def _write_to_local_sync(file_path: str, data: str):
    """Synchronous worker function to save to local disk."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        logger.warning(json.dumps({
            "severity": "WARNING",
            "message": f"Failed to write local log file: {e}",
            "file_path": file_path
        }))

class TrafficRecorder:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    def start(self):
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._process_queue())

    async def stop(self):
        if self._worker_task:
            await self.queue.join()
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def _process_queue(self):
        while True:
            entry = await self.queue.get()
            try:
                await self._persist_entry(entry)
            except Exception as e:
                logger.error(json.dumps({
                    "severity": "ERROR",
                    "message": f"Error in recorder persistence worker: {e}"
                }))
            finally:
                self.queue.task_done()

    async def _persist_entry(self, entry: Dict[str, Any]):
        json_str = json.dumps(entry, indent=2 if not entry.get("is_stdout") else None, ensure_ascii=False)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        trace_id = entry.get("trace_id", str(uuid.uuid4())[:8])
        endpoint_type = entry.get("endpoint_type", "unknown")
        filename = f"{ts_slug}_{endpoint_type}_{trace_id}.json"

        # 1. GCS Upload (if bucket configured)
        if settings.gcs_log_bucket:
            gcs_path = f"traffic/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}/{filename}"
            await asyncio.to_thread(_write_to_gcs_sync, gcs_path, json_str)

        # 2. Local disk backup (if directory configured)
        if settings.local_log_dir:
            local_path = os.path.join(
                settings.local_log_dir,
                datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                filename
            )
            await asyncio.to_thread(_write_to_local_sync, local_path, json_str)

    def record_traffic(
        self,
        endpoint_type: str,
        trace_id: str,
        method: str,
        url: str,
        request_headers: Dict[str, str],
        request_query: Dict[str, Any],
        request_body: Optional[bytes],
        status_code: int,
        response_headers: Dict[str, str],
        response_body: Optional[bytes],
        duration_ms: float,
        extra_meta: Optional[Dict[str, Any]] = None
    ):
        timestamp = datetime.now(timezone.utc).isoformat()

        # Decompress in-memory for human-readable logging while wire traffic remains raw
        resp_encoding = response_headers.get("content-encoding") or response_headers.get("Content-Encoding")
        decompressed_response = decompress_body_safely(response_body, resp_encoding)

        req_encoding = request_headers.get("content-encoding") or request_headers.get("Content-Encoding")
        decompressed_request = decompress_body_safely(request_body, req_encoding)

        req_payload = parse_body_safely(decompressed_request, settings.log_payload_max_bytes) if settings.log_payloads else None
        res_payload = parse_body_safely(decompressed_response, settings.log_payload_max_bytes) if settings.log_payloads else None

        mcp_meta = extract_mcp_rpc_meta(req_payload) or extract_mcp_rpc_meta(res_payload)

        log_entry = {
            "severity": "INFO" if status_code < 400 else "WARNING",
            "time": timestamp,
            "logging.googleapis.com/trace": trace_id,
            "endpoint_type": endpoint_type,
            "trace_id": trace_id,
            "duration_ms": round(duration_ms, 2),
            "request": {
                "method": method,
                "url": url,
                "headers": sanitize_headers(request_headers),
                "query_params": request_query,
                "body": req_payload
            },
            "response": {
                "status_code": status_code,
                "headers": sanitize_headers(response_headers),
                "body": res_payload
            }
        }

        if mcp_meta:
            log_entry["mcp_rpc"] = mcp_meta
        if extra_meta:
            log_entry["meta"] = extra_meta

        # Write structured JSON to stdout for Cloud Run / Cloud Logging
        print(json.dumps(log_entry, ensure_ascii=False), flush=True)

        # Enqueue for GCS and local persistence
        self.queue.put_nowait(log_entry)

recorder = TrafficRecorder()
