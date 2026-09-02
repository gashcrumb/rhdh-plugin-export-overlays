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

## 4. Remedying OCI Artifact References and Versions in Metadata

When the publish workflow reports OCI reference or version mismatches:
- OCI mismatch: `expected "oci://<expected-prefix>/<pkg-slug>" but got "oci://<old-prefix>/<pkg-slug>"`
- Version mismatch: `expected "<built-version>" but got "<old-version>"`

Read the published images from `#### Publishing process` -> `Published container images:` in the publish comment (e.g., `<expected-prefix>/<pkg-slug>:pr_<number>__<pkg-version>`).

Update `workspaces/<workspace>/metadata/<pkg-slug>.yaml`:
```yaml
spec:
  version: "<pkg-version>"
  dynamicArtifact: "oci://<expected-prefix>/<pkg-slug>:bs_<target-backstage-version>__<pkg-version>!<pkg-slug>"
  backstage:
    supportedVersions: "<target-backstage-version>"
```

**CRITICAL RULE**: NEVER revert `spec.version` or `spec.backstage.supportedVersions` to older versions from `source.json` or base branch.

---

## 5. Creating Local `backstage.json` Override

When the publish workflow reports `#### Backstage-incompatible workspaces`:

Extract the target Backstage version from the publish comment (e.g., `1.54.4`).
Create `workspaces/<workspace>/backstage.json`:
```json
{
  "version": "<target-backstage-version>"
}
```
Add `workspaces/<workspace>/backstage.json` to `modified_files`.

---

## 6. Complete Single-Turn Reconciliation Recipe

When `/publish` reports Backstage version incompatibility AND metadata validation errors:

1. **Create `workspaces/<workspace>/backstage.json`**:
   ```json
   {
     "version": "1.54.4"
   }
   ```
2. **Update all `workspaces/<workspace>/metadata/*.yaml` files**:
   - `spec.version`: matching the published container image version (e.g., `1.21.0`)
   - `spec.backstage.supportedVersions`: target Backstage version (e.g., `1.54.4`)
   - `spec.dynamicArtifact`: `oci://<expected-prefix>/<pkg-slug>:bs_<target-backstage-version>__<pkg-version>!<pkg-slug>`
3. **Emit Result**:
   - Add `workspaces/<workspace>/backstage.json` and all updated `metadata/*.yaml` files to `modified_files`.
   - Set `commit_message: "chore(${WORKSPACE}): override backstage compatibility to ${TARGET_BS_VERSION} and reconcile metadata"`.
   - Set `stage: "publish_evaluation"`, `status: "remediation_applied"`, and `slash_command: "/publish"`.
