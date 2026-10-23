import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from config import settings
from recorder import recorder

# Global HTTP client
http_client: Optional[httpx.AsyncClient] = None

# Hop-by-hop headers to filter out when proxying
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length"
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    recorder.start()
    # Configure long timeouts suited for long-lived MCP SSE connections
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
    timeout = httpx.Timeout(
        timeout=settings.client_timeout_seconds,
        connect=30.0,
        read=settings.client_timeout_seconds,
        write=60.0
    )
    http_client = httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=False,
        http2=True
    )
    yield
    if http_client:
        await http_client.aclose()
    await recorder.stop()

app = FastAPI(
    title="Remote MCP & 3LO Shim Proxy",
    description="Captures, logs, and persists StreamableHTTP MCP & 3LO OAuth traffic for offline troubleshooting.",
    version="1.0.0",
    lifespan=lifespan
)

def get_trace_id(request: Request) -> str:
    """Extracts GCP Cloud Trace or W3C Traceparent or generates a UUID."""
    gcp_trace = request.headers.get("x-cloud-trace-context")
    if gcp_trace:
        return gcp_trace.split("/")[0]
    traceparent = request.headers.get("traceparent")
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 2:
            return parts[1]
    return uuid.uuid4().hex

def filter_headers(headers: httpx.Headers) -> dict:
    """Filters hop-by-hop headers from upstream responses."""
    filtered = {}
    for key, value in headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS:
            filtered[key] = value
    return filtered

def prepare_forward_headers(request: Request, target_url: str) -> dict:
    """Prepares headers to forward upstream, replacing Host."""
    target_parsed = urlparse(target_url)
    forward_headers = {}
    for key, value in request.headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS:
            forward_headers[key] = value
    forward_headers["host"] = target_parsed.netloc
    return forward_headers

# ---------------------------------------------------------
# Health & Status Endpoints
# ---------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "targets": {
            "mcp": settings.target_mcp_url,
            "authorize": settings.target_auth_url,
            "token": settings.target_token_url
        },
        "logging": {
            "gcs_bucket": settings.gcs_log_bucket or "disabled",
            "local_dir": settings.local_log_dir or "disabled",
            "log_payloads": settings.log_payloads
        }
    }

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>MCP 3LO Shim Proxy</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; line-height: 1.6; color: #222; background: #fafafa; }}
            .container {{ max-width: 800px; margin: auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            h1 {{ color: #1a73e8; border-bottom: 2px solid #e8eaed; padding-bottom: 10px; }}
            .endpoint {{ margin: 15px 0; padding: 12px; background: #f1f3f4; border-radius: 6px; font-family: monospace; font-size: 14px; word-break: break-all; }}
            .label {{ font-weight: bold; color: #5f6368; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>MCP & 3LO Shim Proxy</h1>
            <p>This proxy captures and records all MCP streamableHTTP sessions and OAuth 3LO token flows for offline troubleshooting.</p>
            
            <h3>Exposed Proxy Endpoints</h3>
            <div class="endpoint">
                <span class="label">MCP Server URL:</span><br/>
                <code>{base_url}/mcp</code>
            </div>
            <div class="endpoint">
                <span class="label">OAuth Authorization URL:</span><br/>
                <code>{base_url}/oauth/authorize</code> (or <code>{base_url}/as/authorize</code>)
            </div>
            <div class="endpoint">
                <span class="label">OAuth Token URL:</span><br/>
                <code>{base_url}/oauth/token</code> (or <code>{base_url}/as/token</code>)
            </div>

            <h3>Upstream Targets Configured</h3>
            <ul>
                <li><strong>Remote MCP:</strong> <code>{settings.target_mcp_url}</code></li>
                <li><strong>Remote Authorize:</strong> <code>{settings.target_auth_url}</code></li>
                <li><strong>Remote Token:</strong> <code>{settings.target_token_url}</code></li>
            </ul>
        </div>
    </body>
    </html>
    """

# ---------------------------------------------------------
# OAuth 3LO Authorization Endpoints
# ---------------------------------------------------------
@app.get("/oauth/authorize")
@app.get("/as/authorize")
async def oauth_authorize(request: Request):
    """
    Handles 3LO interactive authorization by logging request parameters
    and issuing a 302 redirect to the upstream authorization endpoint.
    """
    start_time = time.time()
    trace_id = get_trace_id(request)
    query_params = dict(request.query_params)

    # Build target authorization URL preserving all query params
    target_parsed = urlparse(settings.target_auth_url)
    # Combine any existing query params on target with incoming params
    redirect_url = f"{settings.target_auth_url}?{urlencode(query_params)}" if query_params else settings.target_auth_url

    duration_ms = (time.time() - start_time) * 1000

    recorder.record_traffic(
        endpoint_type="oauth_authorize",
        trace_id=trace_id,
        method="GET",
        url=str(request.url),
        request_headers=dict(request.headers),
        request_query=query_params,
        request_body=None,
        status_code=302,
        response_headers={"location": redirect_url},
        response_body=f"Redirecting to {redirect_url}".encode("utf-8"),
        duration_ms=duration_ms,
        extra_meta={"redirect_target": redirect_url}
    )

    return RedirectResponse(url=redirect_url, status_code=302)

# ---------------------------------------------------------
# OAuth 3LO Token Endpoints
# ---------------------------------------------------------
@app.post("/oauth/token")
@app.post("/as/token")
async def oauth_token(request: Request):
    """
    Transparently proxies OAuth token exchange (authorization_code, refresh_token, client_credentials).
    Captures grant payload and response tokens for troubleshooting.
    """
    start_time = time.time()
    trace_id = get_trace_id(request)
    body_bytes = await request.body()
    forward_headers = prepare_forward_headers(request, settings.target_token_url)

    try:
        upstream_req = http_client.build_request(
            method="POST",
            url=settings.target_token_url,
            headers=forward_headers,
            content=body_bytes,
            params=dict(request.query_params)
        )
        upstream_resp = await http_client.send(upstream_req, stream=True)
        raw_chunks = [c async for c in upstream_resp.aiter_raw()]
        await upstream_resp.aclose()
        resp_body = b"".join(raw_chunks)
        duration_ms = (time.time() - start_time) * 1000
        filtered_resp_headers = filter_headers(upstream_resp.headers)

        recorder.record_traffic(
            endpoint_type="oauth_token",
            trace_id=trace_id,
            method="POST",
            url=str(request.url),
            request_headers=dict(request.headers),
            request_query=dict(request.query_params),
            request_body=body_bytes,
            status_code=upstream_resp.status_code,
            response_headers=filtered_resp_headers,
            response_body=resp_body,
            duration_ms=duration_ms
        )

        return Response(
            content=resp_body,
            status_code=upstream_resp.status_code,
            headers=filtered_resp_headers
        )

    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        err_msg = f"Upstream token request failed: {str(exc)}"
        recorder.record_traffic(
            endpoint_type="oauth_token",
            trace_id=trace_id,
            method="POST",
            url=str(request.url),
            request_headers=dict(request.headers),
            request_query=dict(request.query_params),
            request_body=body_bytes,
            status_code=502,
            response_headers={},
            response_body=err_msg.encode("utf-8"),
            duration_ms=duration_ms,
            extra_meta={"error": str(exc)}
        )
        return JSONResponse(status_code=502, content={"error": "proxy_error", "message": err_msg})

# ---------------------------------------------------------
# StreamableHTTP MCP Proxy Endpoints
# ---------------------------------------------------------
def build_mcp_target_url(subpath: str, query_params: dict) -> str:
    """Builds the upstream MCP URL, appending any subpath and query parameters."""
    base_target = settings.target_mcp_url.rstrip("/")
    if subpath:
        subpath_clean = subpath.lstrip("/")
        full_url = f"{base_target}/{subpath_clean}"
    else:
        full_url = base_target
    
    if query_params:
        full_url = f"{full_url}?{urlencode(query_params)}"
    return full_url

@app.api_route(
    "/mcp",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
@app.api_route(
    "/mcp/{subpath:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
async def mcp_proxy(request: Request, subpath: str = ""):
    """
    Proxies all streamableHTTP MCP interactions (SSE streams, JSON-RPC requests, session management).
    Streams chunks in real-time while asynchronously recording full request/response bodies.
    """
    start_time = time.time()
    trace_id = get_trace_id(request)
    body_bytes = await request.body()
    query_dict = dict(request.query_params)
    upstream_url = build_mcp_target_url(subpath, query_dict)
    forward_headers = prepare_forward_headers(request, upstream_url)

    try:
        # Build streaming request to upstream, preserving client accept-encoding
        upstream_req = http_client.build_request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=body_bytes if request.method in ("POST", "PUT", "PATCH") else None
        )
        upstream_resp = await http_client.send(upstream_req, stream=True)
        filtered_resp_headers = filter_headers(upstream_resp.headers)

        # Ensure immediate chunk flushing on proxy and Cloud Run
        filtered_resp_headers["X-Accel-Buffering"] = "no"

        # Stream chunk generator with concurrent accumulator
        # Using aiter_raw() yields exact on-the-wire raw compressed bytes directly to the client
        async def stream_and_tee():
            accumulated_chunks = []
            try:
                async for chunk in upstream_resp.aiter_raw():
                    accumulated_chunks.append(chunk)
                    yield chunk
            except Exception as stream_err:
                accumulated_chunks.append(f"\n[STREAM INTERRUPTED: {stream_err}]\n".encode("utf-8"))
                raise
            finally:
                await upstream_resp.aclose()
                full_res_bytes = b"".join(accumulated_chunks)
                duration_ms = (time.time() - start_time) * 1000
                recorder.record_traffic(
                    endpoint_type="mcp_stream",
                    trace_id=trace_id,
                    method=request.method,
                    url=str(request.url),
                    request_headers=dict(request.headers),
                    request_query=query_dict,
                    request_body=body_bytes,
                    status_code=upstream_resp.status_code,
                    response_headers=filtered_resp_headers,
                    response_body=full_res_bytes,
                    duration_ms=duration_ms,
                    extra_meta={
                        "upstream_url": upstream_url,
                        "chunk_count": len(accumulated_chunks),
                        "wire_encoding": filtered_resp_headers.get("content-encoding", "identity")
                    }
                )

        return StreamingResponse(
            stream_and_tee(),
            status_code=upstream_resp.status_code,
            headers=filtered_resp_headers
        )

    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        err_msg = f"Failed to forward MCP request to upstream {upstream_url}: {str(exc)}"
        recorder.record_traffic(
            endpoint_type="mcp_stream",
            trace_id=trace_id,
            method=request.method,
            url=str(request.url),
            request_headers=dict(request.headers),
            request_query=query_dict,
            request_body=body_bytes,
            status_code=502,
            response_headers={},
            response_body=err_msg.encode("utf-8"),
            duration_ms=duration_ms,
            extra_meta={"upstream_url": upstream_url, "error": str(exc)}
        )
        return JSONResponse(status_code=502, content={"error": "mcp_proxy_error", "message": err_msg})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
