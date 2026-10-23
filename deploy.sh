#!/usr/bin/env bash
set -e

# ==============================================================================
# Remote MCP & 3LO Shim Proxy - Cloud Run Deployment Script
# ==============================================================================

# Automatically load local .env if present
if [ -f .env ]; then
  echo "Loading environment variables from .env..."
  set -a
  source .env
  set +a
fi

SERVICE_NAME=${SERVICE_NAME:-${service_name:-"remote-3lo-mcp-proxy"}}
REGION=${REGION:-"us-central1"}
PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}

if [ -z "$PROJECT_ID" ]; then
  echo "Error: No Google Cloud Project ID configured."
  echo "Set PROJECT_ID in .env or run 'gcloud config set project <PROJECT_ID>'."
  exit 1
fi

if [ -z "$TARGET_MCP_URL" ] || [ -z "$TARGET_AUTH_URL" ] || [ -z "$TARGET_TOKEN_URL" ]; then
  echo "Error: Upstream target URLs are not configured."
  echo "Please copy .env.sample to .env and configure:"
  echo "  TARGET_MCP_URL"
  echo "  TARGET_AUTH_URL"
  echo "  TARGET_TOKEN_URL"
  exit 1
fi

# Optional GCS bucket for offline raw dumps (leave blank if relying solely on Cloud Logging)
GCS_LOG_BUCKET=${GCS_LOG_BUCKET:-""}

echo "=================================================="
echo " Deploying MCP & 3LO Shim Proxy to Cloud Run"
echo "=================================================="
echo " Project:            ${PROJECT_ID}"
echo " Service Name:       ${SERVICE_NAME}"
echo " Region:             ${REGION}"
echo " Upstream MCP URL:   ${TARGET_MCP_URL}"
echo " Upstream Auth URL:  ${TARGET_AUTH_URL}"
echo " Upstream Token URL: ${TARGET_TOKEN_URL}"
echo " GCS Bucket Sink:    ${GCS_LOG_BUCKET:-'(Cloud Logging only)'}"
echo "=================================================="

ENV_VARS="TARGET_MCP_URL=${TARGET_MCP_URL}"
ENV_VARS="${ENV_VARS},TARGET_AUTH_URL=${TARGET_AUTH_URL}"
ENV_VARS="${ENV_VARS},TARGET_TOKEN_URL=${TARGET_TOKEN_URL}"
ENV_VARS="${ENV_VARS},LOG_PAYLOADS=${LOG_PAYLOADS:-true}"
ENV_VARS="${ENV_VARS},REDACT_SECRETS=${REDACT_SECRETS:-false}"

if [ -n "$GCS_LOG_BUCKET" ]; then
  ENV_VARS="${ENV_VARS},GCS_LOG_BUCKET=${GCS_LOG_BUCKET}"
fi

gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source="." \
  --platform="managed" \
  --no-invoker-iam-check \
  --timeout=3600 \
  --concurrency=80 \
  --cpu=1 \
  --memory=512Mi \
  --set-env-vars="${ENV_VARS}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')

echo ""
echo "=================================================="
echo " Deployment Complete!"
echo " Service URL: ${SERVICE_URL}"
echo "=================================================="
echo " MCP Server Endpoint:       ${SERVICE_URL}/mcp"
echo " OAuth Authorization URL:   ${SERVICE_URL}/oauth/authorize"
echo " OAuth Token URL:           ${SERVICE_URL}/oauth/token"
echo " Health Status:             ${SERVICE_URL}/health"
echo "=================================================="
