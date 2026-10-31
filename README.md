# Yamon GitOps

Source-side clients for the company BuildPlane. Build execution, credentials, resource scheduling and release authorization remain in the central service.

The image and review composite actions request a GitHub Actions OIDC assertion, resolve the approved immutable executor, submit once with If-Match, and preserve correlated request/output evidence. They do not build images or execute product/PR code. No personal access token is required.

This initial action revision is intended for pinned use by the reusable workflows published next. Consumer activation requires central repository/profile/runner onboarding and a deployed compatible API. Publication alone does not establish an operational integration.

Support: https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O
