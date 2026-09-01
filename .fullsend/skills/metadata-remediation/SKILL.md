---
name: metadata-remediation
description: Generate missing Package CRD metadata YAML files, repair appConfigExamples syntax, and create smoke test environment files for RHDH dynamic plugins.
allowed-tools: Bash(curl:*),Bash(jq:*),Bash(git:*),Bash(node:*),Edit,Write
---

# Metadata Remediation Skill

This skill provides patterns and templates for remediating missing or invalid plugin metadata and test configuration in `workspaces/<workspace>/`.

---

## 1. Generating Missing `metadata/<package>.yaml`

When `/publish` reports missing Package metadata for an exported plugin:

### Step 1: Read `source.json`
```bash
SOURCE_REPO=$(jq -r '.repo' "workspaces/${WORKSPACE}/source.json")
SOURCE_REF=$(jq -r '."repo-ref"' "workspaces/${WORKSPACE}/source.json")
REPO_FLAT=$(jq -r '."repo-flat" // false' "workspaces/${WORKSPACE}/source.json")

# Convert GitHub repo URL to raw content prefix
# e.g., https://github.com/backstage/community-plugins -> https://raw.githubusercontent.com/backstage/community-plugins
RAW_URL_PREFIX=$(echo "${SOURCE_REPO}" | sed 's|github.com|raw.githubusercontent.com|')
```

### Step 2: Fetch Upstream `package.json`
```bash
# For a plugin path (e.g., plugins/catalog-backend-module-foo or workspaced package):
PKG_JSON_URL="${RAW_URL_PREFIX}/${SOURCE_REF}/${PLUGIN_REL_PATH}/package.json"
curl -sSL "${PKG_JSON_URL}" -o /tmp/upstream-package.json

PKG_NAME=$(jq -r '.name' /tmp/upstream-package.json)
PKG_VERSION=$(jq -r '.version' /tmp/upstream-package.json)
BACKSTAGE_ROLE=$(jq -r '.backstage.role // empty' /tmp/upstream-package.json)

# If role is missing, infer from package name
if [[ -z "${BACKSTAGE_ROLE}" ]]; then
  if [[ "${PKG_NAME}" =~ -module- ]]; then
    BACKSTAGE_ROLE="backend-plugin-module"
  elif [[ "${PKG_NAME}" =~ -backend ]]; then
    BACKSTAGE_ROLE="backend-plugin"
  else
    BACKSTAGE_ROLE="frontend-plugin"
  fi
fi
```

### Step 3: Write Package YAML (`workspaces/<workspace>/metadata/<pkg-slug>.yaml`)
The metadata file name is derived by stripping `@<scope>/` and replacing `/` with `-` (e.g., `@backstage-community/plugin-foo` -> `backstage-community-plugin-foo.yaml` or `plugin-foo.yaml` depending on workspace convention):

```yaml
apiVersion: rhdh.redhat.com/v1alpha1
kind: Package
metadata:
  name: <pkg-slug>
spec:
  packageName: "<pkg-name>"
  version: "<pkg-version>"
  dynamicArtifact: "oci://ghcr.io/redhat-developer/rhdh-plugin-export-overlays/<pkg-slug>:pr_${PR_NUMBER}__<pkg-version>"
  backstage:
    role: "<backstage-role>"
  support: community
  appConfigExamples: []
```

---

## 2. Generating Smoke Test Environment (`test.env`)

When smoke tests fail because plugins require environment variables at startup:

Create or edit `workspaces/<workspace>/smoke-tests/test.env`:

```env
# Dummy environment configuration for smoke test container boot verification
KEY_NAME=dummy-test-value
ANOTHER_ENV_VAR=dummy-test-value
```

Ensure keys match the missing variable names reported in the smoke test failure logs.

---

## 3. Repairing `appConfigExamples` Syntax Errors

When `validate-app-config-examples.yaml` fails:

1. `appConfigExamples` must be an array of YAML objects (or empty `[]`).
2. Example entries must follow standard Backstage config structure:
   ```yaml
   appConfigExamples:
     - title: Default Configuration
       content:
         app:
           title: My Hub
         catalog:
           providers:
             myProvider:
               baseUrl: https://example.com
   ```
3. Indentation must be strictly 2 spaces, and keys must not contain raw tab characters.

---

## 4. Remedying OCI Artifact References in Metadata

When the publish workflow reports OCI reference mismatch:
`expected "oci://<expected-registry-and-repo>/<pkg-slug>" but got "oci://<old-registry-and-repo>/<pkg-slug>"`

Update `spec.dynamicArtifact` in `workspaces/<workspace>/metadata/<pkg-slug>.yaml`:

```yaml
spec:
  dynamicArtifact: "oci://<expected-registry-and-repo>/<pkg-slug>:pr_${PR_NUMBER}__<pkg-version>"
```

Add the modified file path(s) to `modified_files` in `agent-result.json` and set `slash_command: "/publish"`.

---

## 5. Creating Local `backstage.json` Override

When overriding Backstage compatibility directly without `/override-backstage`:

Create `workspaces/<workspace>/backstage.json`:
```json
{
  "version": "<target-backstage-version>"
}
```
Add `workspaces/<workspace>/backstage.json` to `modified_files` and issue `slash_command: "/publish"`.

---

## 6. Reconciling Metadata YAML After Version Override or Publish Errors

When `backstage.json` is created or after `/override-backstage` completes partially:

1. **Determine Target Backstage Version**:
   - If `workspaces/<workspace>/backstage.json` exists, use `.version` (e.g., `1.54.4`).
   - Else, use `repo-backstage-version` from `workspaces/<workspace>/source.json`.

2. **Inspect and Update each `workspaces/<workspace>/metadata/*.yaml`**:
   - Update `spec.backstage.supportedVersions` to match the target Backstage version.
   - Update `spec.dynamicArtifact`:
     - If the publish validation comment reported `expected "oci://<expected-prefix>/<pkg>" but got "oci://<actual-prefix>/<pkg>"`, update the OCI prefix to match `<expected-prefix>`.
     - Standard format: `oci://<registry-repo>/<pkg-slug>:bs_<backstage-version>__<pkg-version>!<pkg-slug>`
   - Verify `spec.version` matches the plugin version in `package.json` / `plugins-list.yaml`.

3. **Stage and Emit**:
   - Add all modified `workspaces/<workspace>/metadata/*.yaml` paths to `modified_files`.
   - Set `commit_message: "chore(${WORKSPACE}): reconcile metadata supportedVersions and dynamicArtifact refs"`.
   - Set `stage: "publish_evaluation"`, `status: "remediation_applied"`, and `slash_command: "/publish"`.
