# Yamon GitOps

Use the company BuildPlane to build and check your project from GitHub. Start with the centrally installed workflow in `workflow-templates/`.

## Everyday use

1. Maintain your Dockerfile and tests; push a commit or open a PR.
2. Read the GitHub check and its Job Summary for the result and next action.
3. Use the verified image for preview, canary and production promotion when your deployment integration is enabled.

You do not install BuildKit, kubectl or k3s, choose an executor revision, or supply a personal access token. Infrastructure setup belongs to the central owner.

A submitted request is not a completed build or deployment. If a result is uncertain, keep the original request; do not submit another one. For help, post the failing GitHub run link at https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O. Never post credentials.

## Availability

The public client is published. Compatible central API deployment and repository onboarding still require verification before activation. Preview and production acceptance are separate from client publication.

## Central maintenance

The details below are maintained centrally; developers do not configure them for each build.

### Connect a repository

The central owner approves each repository/profile and its source workflow, connects the project/repository assets, provides the non-secret `IMAGE_BUILD_BROKER_URL`, and grants the source repository access to its approved request runner. The label defaults to `fractalops-ci-native`; where an organization uses a different registered label, the central owner sets `BUILD_REQUEST_RUNNER` once. Developers do not configure infrastructure or credentials.

### Request contract

Install the centrally maintained starter from `workflow-templates/`. Developers do not choose internal workflow or executor revisions. Image workflow: `.github/workflows/remote-project-image.yml`, inputs `profile` (default `default`) and `source_channel` (default `default`); outputs `executor_commit`, `request_id`, `release_run_url`. Review workflow: `.github/workflows/remote-project-review.yml`, inputs `profile` and optional `source_pr` (omitted values discover exactly one open same-repository PR matching the current branch and SHA); outputs `executor_commit`, `request_id`, `validation_run_url`. Source workflows require `contents: read` and `id-token: write`; review callers also require `pull-requests: read` for automatic discovery. Explicit PR inputs still pass through central admission. Missing, ambiguous, stale or fork PR discovery fails before OIDC resolution/admission; the automatic GitHub token is sent only to the fixed GitHub API origin. Same-repository open PRs and protected canary refs remain subject to central admission. External fork code is not executed by these request jobs.

The central executor remains `yamonco/fractalops`. Repository publication does not prove API deployment, caller authorization, runner access, build success or staging acceptance. Consumers activate only after the compatible API and their onboarding have been verified.

## Checks and support

`python3 -B .github/actions/request-project-image/request_test.py` and the equivalent review command exercise 79 transport/identity/error-boundary tests. `python3 -B check_contract.py` checks immutable public references and native request-job boundaries. Repository CI performs only these short client checks; actual product builds use the central BuildPlane.

Failure annotations and Job Summary show cause, next action, responsible owner and retained execution context. Central authentication/admission/executor failures do not require developers to patch infrastructure. Do not resubmit an uncertain accepted request or share tokens, secrets or OIDC assertions.

Support: https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O

The contract CI uses the official Sigstore installer directly with cosign v3.1.4. No company installation wrapper or additional developer setup is required.
