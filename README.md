# Yamon GitOps

Public source-side clients for the shared company BuildPlane. Actual image builds, signing, cache policy, resource scheduling and release authorization remain central. These clients submit lightweight control requests; they never check out or execute product/PR code.

The reusable image and review workflows use GitHub OIDC, authenticated executor resolution, strong ETag/If-Match, one retained submission and correlated outputs. No personal access token or developer-managed executor SHA is required. A successful request confirms dispatch, not a successful build or deployment.

## Central onboarding

The central owner approves each repository/profile and its source workflow, connects the project/repository assets, provides the non-secret `IMAGE_BUILD_BROKER_URL`, and grants the source repository access to its approved request runner. The label defaults to `fractalops-ci-native`; where an organization uses a different registered label, the central owner sets `BUILD_REQUEST_RUNNER` once. Developers do not configure infrastructure or credentials.

## Consumer contract

Use an immutable public workflow commit. Image workflow: `.github/workflows/remote-project-image.yml`, inputs `profile` (default `default`) and `source_channel` (default `default`); outputs `executor_commit`, `request_id`, `release_run_url`. Review workflow: `.github/workflows/remote-project-review.yml`, inputs `profile` and `source_pr`; outputs `executor_commit`, `request_id`, `validation_run_url`. Source workflows require `contents: read` and `id-token: write`. Same-repository open PRs and protected canary refs remain subject to central admission. External fork code is not executed by these request jobs.

The central executor remains `yamonco/fractalops`. Repository publication does not prove API deployment, caller authorization, runner access, build success or staging acceptance. Consumers activate only after the compatible API and their onboarding have been verified.

## Checks and support

`python3 -B .github/actions/request-project-image/request_test.py` and the equivalent review command exercise 75 transport/identity/error-boundary tests. `python3 -B check_contract.py` checks immutable public references and native request-job boundaries. Repository CI performs only these short client checks; actual product builds use the central BuildPlane.

Failure annotations and Job Summary show cause, next action, responsible owner and retained execution context. Central authentication/admission/executor failures do not require developers to patch infrastructure. Do not resubmit an uncertain accepted request or share tokens, secrets or OIDC assertions.

Support: https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O
