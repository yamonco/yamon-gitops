import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_request import NoRedirect, required, read_json, broker_error_code, report_dispatch, report_failure


def current_pull_request(opener, repository, source_ref, revision):
    """Discover one exact open same-repository PR; central admission retains authority."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("Current pull request credential unavailable; no build requested")
    branch = source_ref.removeprefix("refs/heads/")
    query = urllib.parse.urlencode({"state": "open", "head": repository.split("/")[0] + ":" + branch, "per_page": 100})
    request = urllib.request.Request(
        "https://api.github.com/repos/" + repository + "/pulls?" + query,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Company-RemoteValidation/1.0"},
    )
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(1048577)
            if len(raw) > 1048576 or response.headers.get("Link"):
                raise ValueError("Incomplete pull request response")
            pulls = json.loads(raw)
        if not isinstance(pulls, list) or len(pulls) != 1:
            raise ValueError("Expected one open pull request")
        pull = pulls[0]
        if (pull["state"] != "open" or type(pull["number"]) is not int or pull["number"] <= 0
            or pull["head"]["repo"]["full_name"] != repository or pull["base"]["repo"]["full_name"] != repository
            or pull["head"]["ref"] != branch or pull["head"]["sha"] != revision):
            raise ValueError("Pull request authority mismatch")
        return pull["number"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SystemExit("Current unique same-repository pull request unavailable; no build requested") from None


CONTEXT_FIELDS = {
    "SOURCE_REPOSITORY": r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}",
    "SOURCE_COMMIT": r"[0-9a-f]{40}",
    "PROFILE": r"[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?",
    "REQUEST_ID": r"[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*",
    "SOURCE_REF": r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*",
    "GITHUB_EVENT_NAME": r"push|workflow_dispatch",
    "SOURCE_PR": r"[1-9][0-9]*",
}
REQUEST_SUFFIX = ''


def main(context):
    repository = required("SOURCE_REPOSITORY", r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
    commit = required("SOURCE_COMMIT", r"[0-9a-f]{40}")
    profile = required("PROFILE", r"[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?")
    request_id = required("REQUEST_ID", r"[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*")
    ref = required("SOURCE_REF", r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
    required("GITHUB_EVENT_NAME", r"push|workflow_dispatch")
    if ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock")) or "@{" in ref:
        raise SystemExit("Invalid source ref")
    base = required("IMAGE_BUILD_BROKER_URL", r"https://[^\s]+")
    parsed = urllib.parse.urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("Invalid broker URL")
    audience = required("IMAGE_BUILD_OIDC_AUDIENCE", r"fractalops-project-validation")
    oidc_url = required("ACTIONS_ID_TOKEN_REQUEST_URL", r"https://[^\s]+")
    oidc = urllib.parse.urlsplit(oidc_url)
    if not oidc.hostname or oidc.username or oidc.password or oidc.fragment:
        raise SystemExit("Invalid Actions identity endpoint")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not token:
        raise SystemExit("Actions identity credential unavailable")
    opener = urllib.request.build_opener(NoRedirect())
    source_pr = (int(required("SOURCE_PR", r"[1-9][0-9]*")) if os.environ.get("SOURCE_PR")
                 else current_pull_request(opener, repository, ref, commit))
    context["source_pr"] = source_pr
    query = [(k, v) for k, v in urllib.parse.parse_qsl(oidc.query, keep_blank_values=True) if k != "audience"]
    query.append(("audience", audience))
    assertion_url = urllib.parse.urlunsplit(oidc._replace(query=urllib.parse.urlencode(query)))
    def remaining_timeout(deadline):
        if deadline is None:
            return 30
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(f"Validation reconciliation deadline exceeded; inspect retained request {request_id} before any new request")
        return min(30, remaining)

    def fresh_assertion(read=False, deadline=None):
        try:
            context["http"] = None
            context["broker_code"] = None
            with opener.open(urllib.request.Request(assertion_url, headers={"Authorization": "Bearer " + token}), timeout=remaining_timeout(deadline)) as response:
                identity = read_json(response)
            remaining_timeout(deadline)
            assertion = identity.get("value") if isinstance(identity, dict) else None
            if not isinstance(assertion, str) or not assertion or len(assertion) > 16384:
                raise ValueError("Invalid identity response")
        except (urllib.error.URLError, OSError, ValueError) as error:
            context["http"] = error.code if isinstance(error, urllib.error.HTTPError) else None
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise SystemExit(f"Actions identity unavailable; inspect request {request_id} before any new request" if read else "Actions identity unavailable; no build requested") from None
        return assertion

    context["phase"] = "identity"
    assertion = fresh_assertion()
    payload = {
        "schemaVersion": 1, "sourceRepository": repository, "sourceCommit": commit,
        "sourceRef": ref, "sourcePr": source_pr, "profile": profile, "requestId": request_id,
    }
    endpoint = base.rstrip("/") + "/v1/builds/project-validations"
    headers = {"Authorization": "Bearer " + assertion, "Content-Type": "application/json", "User-Agent": "Company-RemoteValidation/1.0"}

    # Resolve once, before admission, and freeze this authority for all receipts.
    resolution_request = urllib.request.Request(
        endpoint + "/resolve", method="POST",
        data=json.dumps(payload).encode(), headers=headers,
    )
    context["phase"] = "resolution"
    context["http"] = None
    context["broker_code"] = None
    try:
        with opener.open(resolution_request, timeout=30) as response:
            resolution = read_json(response)
            if (not isinstance(resolution, dict)
                or set(resolution) != {"executorRepository", "executorCommit"}
                or resolution["executorRepository"] != "yamonco/fractalops"
                or not isinstance(resolution["executorCommit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", resolution["executorCommit"])):
                raise ValueError("Invalid executor resolution")
            executor_commit = resolution["executorCommit"]
            executor_etag = response.headers.get("ETag")
            if (executor_etag != f'"{executor_commit}"'
                or response.headers.get("Cache-Control") != "no-store"):
                raise ValueError("Invalid executor resolution headers")
    except (urllib.error.URLError, OSError, ValueError) as error:
        context["http"] = error.code if isinstance(error, urllib.error.HTTPError) else None
        context["invalid_resolution"] = isinstance(error, ValueError)
        if isinstance(error, urllib.error.HTTPError):
            context["broker_code"] = broker_error_code(error)
            error.close()
        raise SystemExit("Executor resolution unavailable or invalid; no build requested") from None

    context["executor"] = executor_commit

    def fetch(read=False, deadline=None):
        context["phase"] = "receipt" if read else "admission"
        context["http"] = None
        context["broker_code"] = None
        if read:
            headers["Authorization"] = "Bearer " + fresh_assertion(read=True, deadline=deadline)
        request = urllib.request.Request(
            endpoint + ("/" + request_id if read else ""),
            method="GET" if read else "POST",
            data=None if read else json.dumps(payload).encode(), headers=headers,
        )
        if not read:
            request.add_header("If-Match", executor_etag)
            context["submitted"] = True
        try:
            with opener.open(request, timeout=remaining_timeout(deadline)) as response:
                result = read_json(response)
                remaining_timeout(deadline)
                if result is None:
                    raise ValueError("Missing receipt")
                return result
        except urllib.error.HTTPError as error:
            context["http"] = error.code
            context["broker_code"] = broker_error_code(error)
            if not read:
                context["admission_http"] = error.code
            error.close()
            raise

    def valid(receipt):
        keys = {"schemaVersion", "requestId", "status", "executorCommit", "workflowRunId", "workflowRunUrl"}
        if not isinstance(receipt, dict) or set(receipt) != keys:
            return False
        if (type(receipt["schemaVersion"]) is not int or receipt["schemaVersion"] != 1
            or receipt["requestId"] != request_id or receipt["executorCommit"] != executor_commit
            or receipt["status"] not in ("dispatching", "dispatched", "outcome_unknown", "rejected")):
            return False
        run_id, run_url = receipt["workflowRunId"], receipt["workflowRunUrl"]
        if run_id is None or run_url is None:
            return run_id is None and run_url is None
        return (type(run_id) is int and run_id > 0
                and run_url == f"https://github.com/yamonco/fractalops/actions/runs/{run_id}")

    receipt = None
    try:
        receipt = fetch()
    except urllib.error.HTTPError as error:
        if error.code == 412:
            raise SystemExit("Validation executor precondition failed: HTTP 412; no new build was admitted by this request; inspect the frozen executor and approved broker executor before any new request") from None
        if error.code != 429 and error.code < 500:
            raise SystemExit(f"Validation broker rejected request: HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        pass
    # Never repeat POST: reconcile the same request with fresh assertions.
    deadline = time.monotonic() + 90
    for delay in (0, 2, 4, 8, 16, 30):
        if isinstance(receipt, dict) and receipt.get("executorCommit") != executor_commit:
            raise SystemExit(f"Validation executor pin mismatch; inspect request {request_id} and frozen executor before any new request")
        if valid(receipt):
            if receipt["status"] == "rejected":
                raise SystemExit(f"Validation broker rejected request {request_id}")
            if receipt["status"] == "dispatched" and receipt["workflowRunId"] is not None:
                break
        elif receipt is not None:
            raise SystemExit(f"Invalid validation receipt; inspect request {request_id} before any new request")
        if time.monotonic() + delay >= deadline:
            break
        if delay:
            time.sleep(delay)
        try:
            receipt = fetch(read=True, deadline=deadline)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise SystemExit(f"Validation receipt unavailable: HTTP {error.code}; inspect request {request_id} before any new request") from None
            receipt = None
        except (urllib.error.URLError, OSError):
            receipt = None
        except ValueError:
            raise SystemExit(f"Invalid validation receipt; inspect request {request_id} before any new request") from None
    # The last read must pass the same identity and readiness predicates.
    if isinstance(receipt, dict) and receipt.get("executorCommit") != executor_commit:
        raise SystemExit(f"Validation executor pin mismatch; inspect request {request_id} and frozen executor before any new request")
    if not valid(receipt) or receipt["status"] != "dispatched" or receipt["workflowRunId"] is None:
        raise SystemExit(f"Validation run identity pending or outcome unknown; inspect retained request {request_id} before any new request")
    context["phase"] = "output"
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"executor_commit={executor_commit}\nrequest_id={request_id}\nvalidation_run_url={receipt['workflowRunUrl']}\nsource_pr={source_pr}\n")

    report_dispatch(request_id, receipt['workflowRunUrl'])

if __name__ == "__main__":
    context = {"phase": "configuration"}
    try:
        try:
            main(context)
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("Invalid validation request configuration or response") from None
    except SystemExit as error:
        report_failure(str(error), context, context_fields=CONTEXT_FIELDS, request_suffix=REQUEST_SUFFIX)
        raise
