#!/usr/bin/env bash
# Private Cloud Run deploy of the agent.
#
#   PROJECT=harvest-agent bash deploy/deploy.sh setup    # once: service account + roles
#   PROJECT=harvest-agent bash deploy/deploy.sh          # stage, build, deploy
#
# The service is deployed WITHOUT --allow-unauthenticated, so every request
# needs an identity token. It runs as a dedicated service account that can
# call models and write logs and traces, and nothing else: no keys, no
# secrets, no access to the sheet or the mailbox.
set -euo pipefail

: "${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"      # where Cloud Run runs
MODEL_LOCATION="${MODEL_LOCATION:-global}"   # the 3.x models and gemini-embedding-2 are global-only
SERVICE="${SERVICE:-harvest-agent}"
SA="${SERVICE}-run@${PROJECT}.iam.gserviceaccount.com"
MODEL="${JOBBANK_AGENT_MODEL:-gemini-3.5-flash-lite}"

if [ "${1:-}" = "setup" ]; then
  gcloud iam service-accounts create "${SERVICE}-run" --project "$PROJECT" \
    --display-name "harvest agent runtime" || true
  for role in roles/aiplatform.user roles/logging.logWriter roles/cloudtrace.agent; do
    gcloud projects add-iam-policy-binding "$PROJECT" --quiet \
      --member "serviceAccount:$SA" --role "$role" --condition None >/dev/null
    echo "granted $role"
  done
  exit 0
fi

bash "$(dirname "$0")/stage.sh"

gcloud run deploy "$SERVICE" --quiet \
  --project "$PROJECT" --region "$REGION" \
  --source "$(cd "$(dirname "$0")/.." && pwd)/out/deploy_ctx" \
  --no-allow-unauthenticated \
  --service-account "$SA" \
  --min-instances 0 --max-instances 2 --concurrency 4 \
  --cpu 1 --memory 2Gi --timeout 120 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$MODEL_LOCATION,JOBBANK_AGENT_MODEL=$MODEL,TRACE_TO_CLOUD=1,JOBBANK_REFUSAL_COSINE=${JOBBANK_REFUSAL_COSINE:-0.0}"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
        --format 'value(status.url)')
echo
echo "deployed: $URL"
echo "unauthenticated (expect 403):"
curl -s -o /dev/null -w '  %{http_code}\n' "$URL/list-apps"
echo "with an identity token (expect 200):"
curl -s -o /dev/null -w '  %{http_code}\n' \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/list-apps"
