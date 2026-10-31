"""Check public immutable references and source-side request boundaries."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
IMMUTABLE_REF = re.compile(r"@[a-f0-9]{40}$")
RUNNER = "runs-on: ${{ vars.BUILD_REQUEST_RUNNER || 'fractalops-ci-native' }}"

for kind in ("image", "review"):
    wrapper = (ROOT / f".github/workflows/remote-project-{kind}.yml").read_text()

    assert wrapper.count(RUNNER) == 1, kind
    assert f"uses: yamonco/yamon-gitops/.github/actions/request-project-{kind}@" in wrapper, kind
    assert "broker-url: ${{ vars.IMAGE_BUILD_BROKER_URL }}" in wrapper, kind
    assert "timeout-minutes: 5" in wrapper, kind
    for source in (wrapper,):
        assert "contents: read" in source and "id-token: write" in source, kind
        refs = re.findall(r"(?m)^\s*uses:\s*(\S+)", source)
        assert len(refs) == 1 and IMMUTABLE_REF.search(refs[0]) and not refs[0].endswith("@" + "0" * 40), kind
        assert not re.search(r"(?m)^\s*(?:container|services):|docker://|--privileged|actions/checkout|ubuntu-latest|pull_request_target|secrets:|yamonco/\.github", source), kind
    action = (ROOT / f".github/actions/request-project-{kind}/action.yml").read_text()
    assert "using: composite" in action and 'python3 "${{ github.action_path }}/request.py"' in action, kind
    assert "SOURCE_REPOSITORY: ${{ github.repository }}" in action, kind
    assert "REQUEST_ID: ${{ github.repository_id }}-${{ github.run_id }}-${{ github.run_attempt }}" in action, kind
print("PASS: image/review public immutable nested refs, onboarding runner fallback, native request-only wrappers, source identity and permissions")
