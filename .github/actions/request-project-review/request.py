import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def required(name, pattern):
    value = os.environ.get(name, "")
    if not re.fullmatch(pattern, value):
        raise SystemExit(f"Invalid or missing {name}")
    return value


def read_json(response):
    raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError("Response too large")
    return json.loads(raw)


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


def report_failure(reason, context):
    """Render only fixed guidance and allowlisted context, never remote error text."""
    code = context.get("http")
    if (code in (401, 403) or "Actions identity unavailable" in reason
        or "Actions identity credential unavailable" in reason):
        cause = "Authentication or authorization failed."
        owner = "Central BuildPlane owner: restore Actions OIDC trust and broker access."
        action = "No developer configuration change required. Central BuildPlane must restore authentication; do not add product credentials."
    elif code in (404, 405) and context.get("phase") == "resolution":
        cause = "The deployed BuildPlane API does not provide the required executor resolution endpoint."
        owner = "Central BuildPlane owner: deploy the compatible API before activating this shared caller."
        action = "No build requested. Wait for the central API rollout; do not change project configuration or retry this request."
    elif code == 409 or "broker rejected request" in reason:
        cause = "Approval or build profile policy rejected the request."
        owner = "Central BuildPlane owner: verify approval and allowed profiles; product owner: verify the selected profile."
        action = "Keep the selected profile unless the product configuration is wrong. Central BuildPlane must repair approval bindings; do not create credentials or a separate runner."
    elif code == 412 and context.get("phase") != "receipt":
        cause = "Executor precondition changed; no build was admitted by this request."
        owner = "Central BuildPlane owner: reconcile the frozen executor with the approved executor."
        action = "No developer configuration change required. Central BuildPlane must reconcile executor authority; do not change pins or bypass If-Match."
    elif ("pin mismatch" in reason or ("Invalid " in reason and "receipt" in reason)
          or context.get("invalid_resolution")):
        cause = "Receipt or executor authority failed security validation."
        owner = "Central BuildPlane owner: reconcile the retained request and trusted executor identity."
        action = "No developer configuration change required. Central BuildPlane must reconcile this request; do not trust returned run links or submit again."
    elif context.get("phase") not in ("configuration", "output"):
        cause = "BuildPlane transport unavailable or run identity still pending; outcome is unconfirmed."
        owner = "Central BuildPlane owner: recover service and reconcile this retained request."
        action = ("Keep this request; do not rerun or submit another build while its outcome is unknown."
                  if context.get("submitted") else "No build requested. Central BuildPlane must restore service; no developer infrastructure change required.")
    else:
        cause = ("Caller output configuration failed after a confirmed build dispatch."
                 if context.get("phase") == "output" else "Caller configuration is invalid or incomplete; no build requested.")
        owner = "Product owner: correct source/profile/channel/PR inputs; central BuildPlane owner: correct shared action or endpoint configuration."
        action = ("Central BuildPlane must correct the shared action output configuration; retain the confirmed build and do not submit again."
                  if context.get("phase") == "output" else "Correct the invalid workflow input identified in the error above, then rerun. Source/profile/channel/PR are product inputs; shared endpoint and OIDC configuration belong to central BuildPlane.")

    if context.get("phase") == "receipt":
        action += " Preserve the retained request; no new submission until central reconciliation confirms its outcome."

    fields = {}
    for name, pattern in CONTEXT_FIELDS.items():
        value = os.environ.get(name, "")
        fields[name] = value if re.fullmatch(pattern, value) else "unavailable"
    request_id = fields["REQUEST_ID"]
    if request_id != "unavailable":
        request_id += REQUEST_SUFFIX + fields["PROFILE"] if REQUEST_SUFFIX and fields["PROFILE"] != "unavailable" else ""
    repository = fields["SOURCE_REPOSITORY"]
    run = fields["REQUEST_ID"].split("-")
    source_run = (f"https://github.com/{repository}/actions/runs/{run[1]}/attempts/{run[2]}"
                  if repository != "unavailable" and len(run) == 3 else "unavailable")
    details = [f"Cause: {cause}", f"Next action: {action}", f"Responsibility: {owner}",
               "Context attached automatically below. No infrastructure evidence form or manual coordinator request is required.",
               "Shared guide: https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O",
               f"Request: {request_id}", f"Source run: {source_run}",
               f"Phase: {context.get('phase', 'configuration')}",
               f"HTTP status: {code if type(code) is int else 'unavailable'}",
               f"Admission HTTP status: {context.get('admission_http', 'unavailable')}",
               f"Frozen executor: {context.get('executor', 'unavailable')}"]
    details.extend(f"{name}: {value}" for name, value in fields.items() if name != "REQUEST_ID")
    # Escape workflow command data even though context is restricted to safe characters.
    annotation = " | ".join(details[:2] + ["Full context: Job Summary.", details[4]]).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error title=Company BuildPlane request failed::" + annotation)
    try:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### Company BuildPlane request failed\n\n" + "\n\n".join(details) + "\n")
    except (OSError, KeyError):
        # Reporting must never replace the original failure or initiate network work.
        pass


def main(context):
    repository = required("SOURCE_REPOSITORY", r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
    commit = required("SOURCE_COMMIT", r"[0-9a-f]{40}")
    profile = required("PROFILE", r"[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?")
    request_id = required("REQUEST_ID", r"[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*")
    ref = required("SOURCE_REF", r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
    source_pr = int(required("SOURCE_PR", r"[1-9][0-9]*"))
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
            error.close()
        raise SystemExit("Executor resolution unavailable or invalid; no build requested") from None

    context["executor"] = executor_commit

    def fetch(read=False, deadline=None):
        context["phase"] = "receipt" if read else "admission"
        context["http"] = None
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
        output.write(f"executor_commit={executor_commit}\nrequest_id={request_id}\nvalidation_run_url={receipt['workflowRunUrl']}\n")


if __name__ == "__main__":
    context = {"phase": "configuration"}
    try:
        try:
            main(context)
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("Invalid validation request configuration or response") from None
    except SystemExit as error:
        report_failure(str(error), context)
        raise
