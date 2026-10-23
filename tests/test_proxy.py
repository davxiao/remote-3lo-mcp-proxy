import asyncio
import gzip
import json
import os
import shutil
import pytest
from httpx import AsyncClient, ASGITransport, Response, AsyncByteStream
import main
from config import settings
from recorder import recorder

TEST_DIR = "./test_traffic_logs"

class MockStream(AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data
    async def __aiter__(self):
        yield self.data

@pytest.fixture(autouse=True)
def setup_teardown_test_env():
    settings.local_log_dir = TEST_DIR
    if not settings.target_auth_url:
        settings.target_auth_url = "https://auth.example.com/as/authorize"
    if not settings.target_mcp_url:
        settings.target_mcp_url = "https://mcp.example.com/mcp"
    if not settings.target_token_url:
        settings.target_token_url = "https://auth.example.com/as/token"
    os.makedirs(TEST_DIR, exist_ok=True)
    yield
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        response = await ac.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "targets" in data
        assert data["targets"]["mcp"] == settings.target_mcp_url

@pytest.mark.asyncio
async def test_oauth_authorize_redirect():
    async with AsyncClient(
        transport=ASGITransport(app=main.app),
        base_url="http://test",
        follow_redirects=False
    ) as ac:
        response = await ac.get("/oauth/authorize?response_type=code&client_id=myclient&scope=openid&state=xyz123")
        assert response.status_code == 302
        location = response.headers.get("location")
        assert location is not None
        assert settings.target_auth_url in location
        assert "client_id=myclient" in location
        assert "state=xyz123" in location

@pytest.mark.asyncio
async def test_as_authorize_alias():
    async with AsyncClient(
        transport=ASGITransport(app=main.app),
        base_url="http://test",
        follow_redirects=False
    ) as ac:
        response = await ac.get("/as/authorize?response_type=code&client_id=alias_test")
        assert response.status_code == 302
        location = response.headers.get("location")
        assert "client_id=alias_test" in location

@pytest.mark.asyncio
async def test_oauth_token_proxy(monkeypatch):
    mock_body = json.dumps({
        "access_token": "mock-token-abc-123",
        "token_type": "Bearer",
        "expires_in": 3600
    }).encode("utf-8")

    mock_resp = Response(
        status_code=200,
        headers={"content-type": "application/json"},
        stream=MockStream(mock_body)
    )

    def mock_build_request(*args, **kwargs):
        return kwargs

    async def mock_send(req, stream=False):
        return mock_resp

    if main.http_client is None:
        main.http_client = AsyncClient()
    monkeypatch.setattr(main.http_client, "build_request", mock_build_request)
    monkeypatch.setattr(main.http_client, "send", mock_send)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        payload = {
            "grant_type": "authorization_code",
            "code": "test_auth_code_123",
            "redirect_uri": "https://client.example.com/callback",
            "client_id": "test_client_id"
        }
        response = await ac.post("/oauth/token", data=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["access_token"] == "mock-token-abc-123"

@pytest.mark.asyncio
async def test_mcp_raw_gzip_passthrough(monkeypatch):
    # Test that gzipped MCP payload is forwarded raw on the wire to client
    # while the recorder decompresses it for logging
    raw_json = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "echo"}]}}).encode("utf-8")
    gzipped_bytes = gzip.compress(raw_json)

    mock_resp = Response(
        status_code=200,
        headers={"content-type": "application/json", "content-encoding": "gzip"},
        stream=MockStream(gzipped_bytes)
    )

    def mock_build_request(*args, **kwargs):
        return kwargs

    async def mock_send(req, stream=False):
        return mock_resp

    if main.http_client is None:
        main.http_client = AsyncClient()
    monkeypatch.setattr(main.http_client, "build_request", mock_build_request)
    monkeypatch.setattr(main.http_client, "send", mock_send)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        req_payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = await ac.post("/mcp", json=req_payload)
        assert response.status_code == 200
        # Wire headers preserve content-encoding: gzip
        assert response.headers.get("content-encoding") == "gzip"
        # Client successfully decompressing the gzip wire payload
        assert response.read() == raw_json

    # Allow worker queue to persist entry
    await recorder._persist_entry({
        "trace_id": "test-trace-123",
        "endpoint_type": "mcp_stream",
        "test": "sample"
    })
    
    # Verify file was written to disk
    date_folders = os.listdir(TEST_DIR)
    assert len(date_folders) > 0
    day_folder = os.path.join(TEST_DIR, date_folders[0])
    files = os.listdir(day_folder)
    assert len(files) > 0
    with open(os.path.join(day_folder, files[0])) as f:
        saved_data = json.load(f)
        assert saved_data["trace_id"] == "test-trace-123"
