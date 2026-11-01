"""Shared build-request validation and safe failure reporting; no network work on import."""

import json
import os
import re
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


def broker_error_code(error):
    """Accept only fixed broker codes paired with their HTTP status; discard all text."""
    try:
        body = read_json(error)
        detail = body.get("detail") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        return code if isinstance(code, str) and {
            "source_advanced": 409,
            "source_authority_unavailable": 503,
            "broker_internal_error": 500,
        }.get(code) == error.code else None
    except (OSError, ValueError, AttributeError):
        return None


def report_dispatch(request_id, run_url):
    """A dispatch receipt proves submission, never build or deployment completion."""
    message = (
        "Request submitted; build result and deployment are unverified. "
        "Follow the retained execution and wait for its terminal result; "
        "do not submit another build while its outcome is unknown."
    )
    print("::notice title=Build request submitted::" + message + " Execution: " + run_url)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as output:
            output.write(
                "### Build request submitted\n\n" + message + "\n\n"
                + f"Request: `{request_id}`\n\n[Follow this execution]({run_url})\n"
            )


def report_failure(reason, context, *, context_fields, request_suffix):
    """Render only fixed guidance and allowlisted context, never remote error text."""
    code = context.get("http")
    code = code if type(code) is int and 100 <= code <= 599 else None
    phase = context.get("phase")
    phase = phase if phase in ("configuration", "identity", "resolution", "admission", "receipt", "output") else "configuration"
    redirected = code in (301, 302, 303, 307, 308)
    broker_code = context.get("broker_code")
    if broker_code == "source_advanced" and code == 409:
        cause = "Source revision or branch protection changed."
        owner = "Product owner: reconcile the retained request before starting a new authorized source run."
        action = "Read the same request for its outcome. Do not change credentials, profiles or executor pins; do not resubmit while the outcome is unknown."
    elif broker_code == "source_authority_unavailable" and code == 503:
        cause = "Source authority verification is temporarily unavailable."
        owner = "Central BuildPlane owner: recover source authority access."
        action = "Read the same retained request after recovery; do not resubmit."
    elif broker_code == "broker_internal_error" and code == 500:
        cause = "BuildPlane broker failed internally."
        owner = "Central BuildPlane owner: inspect broker diagnostics using the attached request ID."
        action = "Read the same retained request for its outcome; do not resubmit or change product configuration."
    elif redirected:
        cause = "Incorrect browser-SSO routing: the machine API returned a redirect."
        owner = "Central BuildPlane owner: restore the API route for Actions OIDC without browser sign-in."
        action = "Inspect the linked source run and shared guide for API route recovery; keep NoRedirect and Actions OIDC, without adding credentials or runners."
    elif code in (400, 422):
        cause = "Invalid request or configuration rejected by the service."
        owner = "Product owner: verify source/profile/channel/PR inputs; central BuildPlane owner: verify the shared request contract."
        action = "Inspect the linked source run's request inputs against the shared guide and correct the invalid configuration."
    elif code == 429:
        cause = "Request throttled by the service."
        owner = "Central BuildPlane owner: inspect service throttling under the existing request policy."
        action = "Inspect the linked source run and shared guide; wait for service throttling to clear."
    elif code in (502, 503, 504):
        cause = "Request service unavailable."
        owner = "Central BuildPlane owner: verify the shared endpoint and service availability for the reported phase."
        action = "Inspect the linked source run and shared guide for service recovery."
    elif (code in (401, 403) or "Actions identity unavailable" in reason
        or "Actions identity credential unavailable" in reason):
        cause = "Authentication or authorization failed."
        owner = "Central BuildPlane owner: restore Actions OIDC trust and broker access."
        action = "No developer configuration change required. Central BuildPlane must restore authentication; do not add product credentials."
    elif code in (404, 405) and phase == "resolution":
        cause = "The deployed BuildPlane API does not provide the required executor resolution endpoint."
        owner = "Central BuildPlane owner: deploy the compatible API before activating this shared caller."
        action = "No build requested. Wait for the central API rollout; do not change project configuration or retry this request."
    elif code == 409 or "broker rejected request" in reason:
        cause = "Approval or build profile policy rejected the request."
        owner = "Central BuildPlane owner: verify approval and allowed profiles; product owner: verify the selected profile."
        action = "Keep the selected profile unless the product configuration is wrong. Central BuildPlane must repair approval bindings; do not create credentials or a separate runner."
    elif code == 412 and phase != "receipt":
        cause = "Executor precondition changed; no build was admitted by this request."
        owner = "Central BuildPlane owner: reconcile the frozen executor with the approved executor."
        action = "No developer configuration change required. Central BuildPlane must reconcile executor authority; do not change pins or bypass If-Match."
    elif ("pin mismatch" in reason or ("Invalid " in reason and "receipt" in reason)
          or context.get("invalid_resolution")):
        cause = "Receipt or executor authority failed security validation."
        owner = "Central BuildPlane owner: reconcile the retained request and trusted executor identity."
        action = "No developer configuration change required. Central BuildPlane must reconcile this request; do not trust returned run links or submit again."
    elif phase not in ("configuration", "output"):
        cause = "BuildPlane transport unavailable or run identity still pending; outcome is unconfirmed."
        owner = "Central BuildPlane owner: recover service and reconcile this retained request."
        action = ("Keep this request; do not rerun or submit another build while its outcome is unknown."
                  if context.get("submitted") else "No build requested. Central BuildPlane must restore service; no developer infrastructure change required.")
    else:
        cause = ("Caller output configuration failed after a confirmed build dispatch."
                 if phase == "output" else "Caller configuration is invalid or incomplete; no build requested.")
        owner = "Product owner: correct source/profile/channel/PR inputs; central BuildPlane owner: correct shared action or endpoint configuration."
        action = ("Central BuildPlane must correct the shared action output configuration; retain the confirmed build and do not submit again."
                  if phase == "output" else "Correct the invalid workflow input identified in the error above, then rerun. Source/profile/channel/PR are product inputs; shared endpoint and OIDC configuration belong to central BuildPlane.")

    if redirected or code in (400, 422, 429, 502, 503, 504):
        if context.get("submitted"):
            action += " Submission outcome is unconfirmed. Keep this request; do not rerun or submit another build. Status polling reads the same retained request and does not submit a build."
        else:
            action = "No build requested. " + action + " After the reported problem is corrected, rerun the source workflow to request a build."
    if phase == "receipt":
        action += " Preserve the retained request; no new submission until reconciliation confirms its outcome."

    fields = {}
    for name, pattern in context_fields.items():
        value = os.environ.get(name, "")
        fields[name] = value if re.fullmatch(pattern, value) else "unavailable"
    if type(context.get("source_pr")) is int:
        fields["SOURCE_PR"] = str(context["source_pr"])
    request_id = fields["REQUEST_ID"]
    if request_id != "unavailable":
        request_id += request_suffix + fields["PROFILE"] if request_suffix and fields["PROFILE"] != "unavailable" else ""
    repository = fields["SOURCE_REPOSITORY"]
    run = fields["REQUEST_ID"].split("-")
    source_run = (f"https://github.com/{repository}/actions/runs/{run[1]}/attempts/{run[2]}"
                  if repository != "unavailable" and len(run) == 3 else "unavailable")
    admission_http = context.get("admission_http")
    admission_http = admission_http if type(admission_http) is int and 100 <= admission_http <= 599 else "unavailable"
    executor = context.get("executor")
    executor = executor if isinstance(executor, str) and re.fullmatch(r"[0-9a-f]{40}", executor) else "unavailable"
    details = [f"Cause: {cause}", f"Next action: {action}", f"Responsibility: {owner}",
               "Context attached automatically below. No infrastructure evidence form or manual coordinator request is required.",
               "Shared guide: https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O",
               f"Request: {request_id}", f"Source run: {source_run}",
               f"Phase: {phase}",
               f"HTTP status: {code if type(code) is int else 'unavailable'}",
               f"Admission HTTP status: {admission_http}",
               f"Frozen executor: {executor}"]
    details.extend(f"{name}: {value}" for name, value in fields.items() if name != "REQUEST_ID")
    # Escape workflow command data even though context is restricted to safe characters.
    annotation = " | ".join(details[:2] + details[4:] + ["Full context: Job Summary."]).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error title=Company BuildPlane request failed::" + annotation)
    try:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### Company BuildPlane request failed\n\n" + "\n\n".join(details) + "\n")
    except (OSError, KeyError):
        # Reporting must never replace the original failure or initiate network work.
        pass


