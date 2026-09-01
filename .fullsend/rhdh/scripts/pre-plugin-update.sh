#!/usr/bin/env bash
# pre-plugin-update.sh — Fetch PR context and workspace metadata before sandbox creation.
#
# Runs on the GitHub Actions runner host BEFORE the sandbox is created.
# Fetches complete PR metadata, comments, files, and workspace structure via gh,
# and writes /tmp/workspace/pr-input.json to be mounted into the sandbox.

set -euo pipefail

WORKSPACE_DIR="/tmp/workspace"
mkdir -p "${WORKSPACE_DIR}"

PR_URL="${GITHUB_ISSUE_URL:-${GITHUB_PR_URL:-}}"
if [[ -z "${PR_URL}" ]]; then
  echo "::error::No GITHUB_ISSUE_URL or GITHUB_PR_URL set"
  exit 1
fi

PR_NUMBER=$(echo "${PR_URL}" | grep -oP '(?<=pull/)[0-9]+' || echo "${PR_URL##*/}")
REPO="${REPO_FULL_NAME:-redhat-developer/rhdh-plugin-export-overlays}"

echo "Pre-script: fetching metadata for PR #${PR_NUMBER} on ${REPO}..."

# Fetch PR JSON from GitHub
PR_JSON=$(gh pr view "${PR_NUMBER}" --repo "${REPO}" \
  --json number,title,body,baseRefName,headRefName,state,labels,comments,files)

# Extract affected workspace
WORKSPACES=$(echo "${PR_JSON}" | jq -r '.files[].path' | grep -oP '^workspaces/[^/]+' | sort -u | sed 's|workspaces/||')
NUM_WORKSPACES=$(echo "${WORKSPACES}" | grep -v '^$' | wc -l)
AFFECTED_WORKSPACE=$(echo "${WORKSPACES}" | head -1 | tr -d '[:space:]')

# Check if workspace has e2e-tests directory
HAS_E2E=false
if [[ -n "${AFFECTED_WORKSPACE}" && -d "workspaces/${AFFECTED_WORKSPACE}/e2e-tests" ]]; then
  HAS_E2E=true
fi

# Build consolidated pr-input.json
jq -n \
  --argjson pr "${PR_JSON}" \
  --arg workspace "${AFFECTED_WORKSPACE}" \
  --argjson num_workspaces "${NUM_WORKSPACES}" \
  --argjson has_e2e "${HAS_E2E}" \
  --arg base_branch "main-fullsend-experiment" \
  '{
    pr: $pr,
    affected_workspace: $workspace,
    num_workspaces: $num_workspaces,
    has_e2e_tests: $has_e2e,
    base_branch: $base_branch
  }' > "${WORKSPACE_DIR}/pr-input.json"

echo "Pre-script complete. Generated ${WORKSPACE_DIR}/pr-input.json (Workspace: ${AFFECTED_WORKSPACE}, Has E2E: ${HAS_E2E})"
