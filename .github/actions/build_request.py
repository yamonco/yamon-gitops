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


def report_failure(reason, context, *, context_fields, request_suffix):
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


