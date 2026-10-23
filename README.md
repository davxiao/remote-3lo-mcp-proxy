# StreamableHTTP MCP & 3LO Shim Proxy

A high-fidelity streaming reverse proxy deployed on **Google Cloud Run** to inspect, log, and record all **StreamableHTTP Model Context Protocol (MCP)** sessions and **3-Legged OAuth (3LO)** authorization and token exchanges.

---

## Quickstart: Deploy to Cloud Run

### 1. Configure Target URLs
```bash
cp .env.sample .env
```
Fill in the four required values in `.env`:
* `PROJECT_ID`: Your GCP project ID.
* `TARGET_MCP_URL`: Upstream MCP server endpoint (`https://<REMOTE_MCP_HOST>/.../mcp`).
* `TARGET_AUTH_URL`: Upstream OAuth 2.0 authorization URL (`https://<REMOTE_AUTH_HOST>/.../authorize`).
* `TARGET_TOKEN_URL`: Upstream OAuth 2.0 token URL (`https://<REMOTE_AUTH_HOST>/.../token`).

### 2. Deploy
```bash
chmod +x deploy.sh
./deploy.sh
```
`deploy.sh` automatically reads `.env`, validates variables, and deploys to Cloud Run with `--no-invoker-iam-check` and a 60-minute connection timeout.

### 3. Configure Your MCP Client
Point your MCP client (Google Vertex AI Search, Gemini Enterprise, or Google ADK) to the proxy URLs output by the deployment script:
* **Remote MCP Server URL**: `https://<SERVICE_URL>/mcp`
* **Remote MCP Authorization URL**: `https://<SERVICE_URL>/oauth/authorize`
* **Remote MCP Token URL**: `https://<SERVICE_URL>/oauth/token`

---

## Architecture & Endpoints

```
[MCP Client (Google ADK) / Browser]
                 │
                 ▼
+─────────────────────────────────────────────────────────────────────────────────+
| Google Cloud Run Shim Proxy                                                     |
|                                                                                 |
| 1. GET  /oauth/authorize (or /as/authorize) ──► Logs params, 302 to Auth URL    |
| 2. POST /oauth/token     (or /as/token)     ──► Proxies & logs payload & tokens |
| 3. POST /mcp             (and /mcp/*)       ──► Real-time stream pass-through   |
|                                                                                 |
| Wire-Fidelity Engine:                                                           |
|  ├─► Downstream: aiter_raw() forwards untouched compressed bytes (gzip intact)  |
|  └─► Logger:     decompress_body_safely() unzips in-memory for readable JSON    |
|                                                                                 |
| Logging Sinks:                                                                  |
|  - Google Cloud Logging (Structured JSON to stdout with indexed MCP RPC methods)|
|  - Google Cloud Storage (Offline raw JSON dumps by date/trace)                 |
+─────────────────────────────────────────────────────────────────────────────────+
                 │
                 ▼
[Remote MCP Server supporting 3LO]
  ├─ Remote MCP Authorization URL
  ├─ Remote MCP Token URL
  └─ Remote MCP Server URL
```

### URL Mapping

| Proxy Endpoint | Upstream Target | Behavior |
| :--- | :--- | :--- |
| `GET /oauth/authorize`<br>`GET /as/authorize` | `<TARGET_AUTH_URL>` | Captures OAuth parameters (`client_id`, `redirect_uri`, `code_challenge`, `state`) and returns an HTTP 302 redirect to the remote IdP. |
| `POST /oauth/token`<br>`POST /as/token` | `<TARGET_TOKEN_URL>` | Proxies token exchanges (`authorization_code`, `refresh_token`), logging request credentials and issued access/refresh/ID tokens. |
| `POST /mcp`<br>`ANY /mcp/*` | `<TARGET_MCP_URL>` | Zero-buffer bi-directional streaming for MCP sessions (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`). Raw compressed bytes stream downstream while logging uncompressed JSON. |
| `GET /health` | N/A | Returns proxy health, configured upstream targets, and logging status. |

---

## Key Capabilities

### 1. Wire-Fidelity Streaming & In-Memory Decompression
* **On the Wire**: When upstream responds with `content-encoding: gzip`, the proxy streams raw compressed chunks downstream via `aiter_raw()`, keeping gzip headers intact so MCP clients decompress standard gzip cleanly without corruption.
* **In Cloud Logging**: The proxy branches the raw byte stream in memory, decompressing it solely for structured logging so administrators can view readable JSON-RPC methods (`tools/list`, `tools/call`, `initialize`).

### 2. Domain-Restricted GCP Org Compatibility
In GCP organizations enforcing **Domain Restricted Sharing** (`constraints/iam.allowedPolicyMemberDomains`), binding `allUsers` to IAM policies is forbidden.
The service deploys with:
```bash
--no-invoker-iam-check
```
This bypasses Cloud Run infrastructure-level IAM checks without violating organizational policies. Application authentication is handled end-to-end by OAuth 2.0 PKCE and Bearer tokens.

---

## Live Monitoring & Troubleshooting Logs

### Live Stream Logs
```bash
gcloud beta logging tail \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="<SERVICE_NAME>"' \
  --project=<PROJECT_ID> \
  --format=json
```

### Cloud Logging Queries (GCP Log Explorer)

* **All OAuth & MCP Events**:
  ```
  resource.type="cloud_run_revision"
  resource.labels.service_name="<SERVICE_NAME>"
  jsonPayload.endpoint_type:*
  ```

* **Tool Discovery (`tools/list`)**:
  ```
  resource.type="cloud_run_revision"
  resource.labels.service_name="<SERVICE_NAME>"
  jsonPayload.mcp_rpc.method="tools/list"
  ```

* **Tool Executions (`tools/call`)**:
  ```
  resource.type="cloud_run_revision"
  resource.labels.service_name="<SERVICE_NAME>"
  jsonPayload.mcp_rpc.method="tools/call"
  ```

* **Token Exchanges (`oauth_token`)**:
  ```
  resource.type="cloud_run_revision"
  resource.labels.service_name="<SERVICE_NAME>"
  jsonPayload.endpoint_type="oauth_token"
  ```

---

## Source IP Breakdown

From an external source IP perspective, all operational traffic maps directly to these categories:

| Action / Flow | External Source IP | Description |
| :--- | :--- | :--- |
| `GET /oauth/authorize` | **End-User IP** | User workstation / browser during interactive 3LO authorization redirect |
| `POST /oauth/token` | **Google Service Outbound IP** | Google backend service-to-service token exchange |
| `POST /mcp` (`initialize`) | **Google Service Outbound IP** | Google ADK / Discovery Engine protocol handshake |
| `POST /mcp` (`notifications/initialized`) | **Google Service Outbound IP** | Google ADK / Discovery Engine initialization acknowledgment |
| `POST /mcp` (`tools/list`) | **Google Service Outbound IP** | Google ADK / Discovery Engine tool schema discovery |
| `POST /mcp` (`tools/call`) | **Google Service Outbound IP** | Google ADK / Discovery Engine tool execution |

---

## Local Development & Testing

```bash
# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run local development server
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Run test suite
pytest -v
```
