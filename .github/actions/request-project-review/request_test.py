"""Execute the request transport shared by the action and workflow adapter, without network."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request


WORKFLOW = Path(__file__).resolve().parents[2] / 'workflows/remote-project-review.yml'
ACTION = WORKFLOW.parents[1] / 'actions/request-project-review/action.yml'
SCRIPT = (ACTION.parent / 'request.py').read_text()

RECEIPT = {
    'schemaVersion': 1, 'requestId': '123-456-1', 'status': 'dispatched',
    'executorCommit': 'b' * 40, 'workflowRunId': 789,
    'workflowRunUrl': 'https://github.com/yamonco/fractalops/actions/runs/789',
}


def http_error(code):
    return urllib.error.HTTPError('https://broker.example', code, 'secret-error-body', {}, io.BytesIO())


class RemoteReviewContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.event = self.root / 'event.json'
        self.event.write_text(json.dumps({'repository': {'default_branch': 'main'}}))
        self.output = self.root / 'output'
        self.summary = self.root / 'summary'
        self.env = {
            'IMAGE_BUILD_BROKER_URL': 'https://broker.example',
            'IMAGE_BUILD_OIDC_AUDIENCE': 'fractalops-project-validation',
            'SOURCE_PR': '12', 'SOURCE_REPOSITORY': 'yamonco/example',
            'SOURCE_COMMIT': 'a' * 40, 'SOURCE_REF': 'refs/heads/review/example', 'PROFILE': 'default', 'REQUEST_ID': '123-456-1',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_EVENT_PATH': str(self.event), 'GITHUB_OUTPUT': str(self.output),
            'GITHUB_STEP_SUMMARY': str(self.summary),
            'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://actions.example/token?x=1&audience=old',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'identity-secret',
        }
        self.calls = []
        self.sleeps = []

    def run_workflow(self, *responses, elapsed=None, resolution=None, resolution_headers=None, refresh_error=None, discovery=None):
        self.calls = []
        self.transport_calls = []
        self.output.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        self.sleeps = []
        replies = iter(responses or ({'value': 'assertion-secret'}, RECEIPT))

        self.timeouts = []
        durations = iter(elapsed or ())
        def open_request(request, timeout):
            self.transport_calls.append(request)
            if request.full_url.startswith('https://api.github.com/repos/'):
                response = io.BytesIO(json.dumps(discovery if discovery is not None else []).encode())
                response.headers = {}
                return response
            # Keep existing admission/reconciliation assertions separate from resolution.
            if request.full_url.endswith('/resolve'):
                reply = resolution if resolution is not None else {
                    'executorRepository': 'yamonco/fractalops', 'executorCommit': 'b' * 40,
                }
                if isinstance(reply, Exception):
                    raise reply
                response = io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
                response.headers = resolution_headers if resolution_headers is not None else {
                    'ETag': '"' + 'b' * 40 + '"', 'Cache-Control': 'no-store',
                }
                return response
            self.calls.append(request)
            self.timeouts.append(timeout)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
            if elapsed is not None:
                elapsed_clock[0] += next(durations, 0)
            if request.full_url.startswith('https://actions.example/') and len(self.calls) > 1:
                if refresh_error is not None:
                    raise refresh_error
                return io.BytesIO(json.dumps({'value': 'assertion-secret'}).encode())
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

        log = io.StringIO()
        elapsed_clock = [0]
        clock = patch('time.monotonic', side_effect=lambda: elapsed_clock[0]) if elapsed is not None else contextlib.nullcontext()
        with patch.dict(os.environ, self.env, clear=True), patch('urllib.request.OpenerDirector.open', side_effect=open_request), patch('time.sleep', side_effect=self.sleeps.append), contextlib.redirect_stdout(log), contextlib.redirect_stderr(log), clock:
            try:
                exec(compile(SCRIPT, str(WORKFLOW), 'exec'), {'__name__': '__main__'})
            except SystemExit as error:
                log.write(str(error))
                success = False
            else:
                success = True
        self.log = log.getvalue()
        self.summary_text = self.summary.read_text() if self.summary.exists() else ''
        for secret in ('identity-secret', 'assertion-secret', 'secret-error-body'):
            self.assertNotIn(secret, log.getvalue() + self.summary_text)
        self.assertLessEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)
        if not success:
            self.assertFalse(self.output.exists())
            self.assertIn('::error title=Company BuildPlane request failed::', self.log)
            if self.env.get('GITHUB_STEP_SUMMARY') == str(self.summary):
                self.assertIn('Cause:', self.summary_text)
                self.assertIn('Next action:', self.summary_text)
                self.assertIn('Responsibility:', self.summary_text)
                self.assertIn('https://langboard.yamon.io/board/6v0q6uY44jn/hmD5UBzCv6O', self.summary_text)
        return success


    def test_automatic_push_discovers_before_identity_and_admission(self):
        self.env.update(SOURCE_PR='', GITHUB_EVENT_NAME='push', GITHUB_TOKEN='read-only-test-token')
        pull = {'number': 12, 'state': 'open', 'head': {'repo': {'full_name': 'yamonco/example'},
                'ref': 'review/example', 'sha': 'a' * 40}, 'base': {'repo': {'full_name': 'yamonco/example'}}}
        self.assertTrue(self.run_workflow(discovery=[pull]))
        self.assertTrue(self.transport_calls[0].full_url.startswith('https://api.github.com/repos/'))
        self.assertEqual(json.loads(self.calls[1].data)['sourcePr'], 12)
        self.assertIn('source_pr=12', self.output.read_text())
        for request in self.transport_calls[1:]:
            self.assertNotEqual(request.get_header('Authorization'), 'Bearer read-only-test-token')
        self.assertNotIn('read-only-test-token', self.log + self.summary_text)
        self.assertFalse(self.run_workflow(discovery=[]))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.transport_calls), 1)
        self.assertNotIn('read-only-test-token', self.log + self.summary_text)

    def test_review_success_exact_payload_and_source_identity(self):
        self.assertTrue(self.run_workflow())
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
        self.assertEqual(json.loads(self.calls[1].data), {
            'schemaVersion': 1, 'sourceRepository': 'yamonco/example',
            'sourceCommit': 'a' * 40, 'sourceRef': 'refs/heads/review/example',
            'sourcePr': 12, 'profile': 'default', 'requestId': '123-456-1',
        })
        self.assertEqual(self.calls[1].full_url, 'https://broker.example/v1/builds/project-validations')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0].full_url).query)
        self.assertEqual(query, {'x': ['1'], 'audience': ['fractalops-project-validation']})
        self.assertEqual(self.output.read_text(), 'executor_commit=' + 'b' * 40 + '\nrequest_id=123-456-1\nvalidation_run_url=' + RECEIPT['workflowRunUrl'] + '\nsource_pr=12\n')
        source = ACTION.read_text()
        for field in ('SOURCE_COMMIT: ${{ github.sha }}', 'SOURCE_REF: ${{ github.ref }}',
                      'SOURCE_REPOSITORY: ${{ github.repository }}',
                      'REQUEST_ID: ${{ github.repository_id }}-${{ github.run_id }}-${{ github.run_attempt }}'):
            self.assertIn(field, source)

    def test_explicit_positive_pr_and_trusted_source_events_only(self):
        for name, values in {
            'SOURCE_PR': ['', '0', '-1', '1.2', 'true', '01', ' 12', '12\ninject=x'],
            'SOURCE_REF': ['refs/tags/review/a', 'refs/heads/review/../main',
                           'refs/heads/review/a//b', 'refs/heads/review/a.lock'],
            'SOURCE_COMMIT': ['main', ''], 'REQUEST_ID': ['123-456-0'],
            'PROFILE': ['', 'bad profile', '../default'],
            'GITHUB_EVENT_NAME': ['pull_request', 'pull_request_target', 'workflow_run'],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(self.env, {name: value}):
                    self.assertFalse(self.run_workflow())
                    self.assertEqual(self.calls, [])

    def test_push_preserves_exact_source_and_review_identity(self):
        self.env.update({
            'GITHUB_EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/fix/required-quality',
            'SOURCE_REF': 'refs/heads/fix/required-quality',
        })
        self.assertTrue(self.run_workflow())
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
        payload = json.loads(self.calls[1].data)
        self.assertEqual(payload['sourcePr'], 12)
        self.assertEqual(payload['sourceCommit'], 'a' * 40)
        self.assertEqual(payload['sourceRef'], self.env['GITHUB_REF'])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0].full_url).query)
        self.assertEqual(query['audience'], ['fractalops-project-validation'])

    def test_same_repository_pr_heads_need_no_review_prefix(self):
        for ref in ('refs/heads/fix/native-ci-oom', 'refs/heads/feature/compiler', 'refs/heads/main'):
            with self.subTest(ref=ref), patch.dict(self.env, {'SOURCE_REF': ref}):
                self.assertTrue(self.run_workflow())
                self.assertEqual(json.loads(self.calls[-1].data)['sourceRef'], ref)

    def test_cross_organization_source_preserves_generic_payload(self):
        self.env['SOURCE_REPOSITORY'] = 'Langboard/langboard'
        self.assertTrue(self.run_workflow())
        self.assertEqual(json.loads(self.calls[1].data)['sourceRepository'], 'Langboard/langboard')
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
        self.assertIn('validation_run_url=' + RECEIPT['workflowRunUrl'], self.output.read_text())

    def test_malformed_repository_fails_before_network(self):
        for repository in ('', 'repo', '/repo', 'owner/', 'owner/repo/extra',
                           'owner name/repo', 'owner_name/repo', 'owner/repo name',
                           'a' * 40 + '/repo', 'owner/' + 'a' * 101):
            with self.subTest(repository=repository):
                self.env['SOURCE_REPOSITORY'] = repository
                self.assertFalse(self.run_workflow())
                self.assertEqual(self.calls, [])



    def test_bad_configuration_or_absent_oidc_fails_before_network(self):
        for name, values in {
            'IMAGE_BUILD_BROKER_URL': ['', 'http://broker.example', 'https://user:pass@broker.example', 'https://broker.example?q=x', 'https://broker.example/#fragment'],
            'IMAGE_BUILD_OIDC_AUDIENCE': ['', 'other-audience'],
            'ACTIONS_ID_TOKEN_REQUEST_URL': ['', 'http://actions.example', 'https://user@actions.example', 'https://actions.example/#fragment'],
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': [''],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(self.env, {name: value}):
                    self.assertFalse(self.run_workflow())
                    self.assertEqual(self.calls, [])

    def test_identity_failure_never_submits(self):
        for response in [{}, [], {'value': ''}, {'value': 123}, b'invalid', b'x' * 16385, http_error(403), TimeoutError('secret-error-body')]:
            with self.subTest(response_type=type(response).__name__):
                self.assertFalse(self.run_workflow(response))
                self.assertEqual(len(self.calls), 1)

    def test_ambiguous_post_reads_same_id_without_duplicate_post(self):
        for response in [http_error(429), http_error(500), TimeoutError(), urllib.error.URLError('secret-error-body'), b'invalid', b'x' * 16385]:
            with self.subTest(response_type=type(response).__name__):
                self.assertTrue(self.run_workflow({'value': 'assertion-secret'}, response, RECEIPT))
                self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST', 'GET', 'GET'])
                self.assertEqual(self.calls[-1].full_url, 'https://broker.example/v1/builds/project-validations/123-456-1')
                self.assertIsNone(self.calls[-1].data)

    def test_pending_receipts_read_back(self):
        for state in ('dispatching', 'outcome_unknown', 'dispatched'):
            pending = {**RECEIPT, 'status': state, 'workflowRunId': None, 'workflowRunUrl': None}
            self.assertTrue(self.run_workflow({'value': 'assertion-secret'}, pending, pending, pending, RECEIPT))
            self.assertEqual(self.sleeps, [2, 4])
            broker = [c for c in self.calls if c.full_url.startswith('https://broker.example')]
            self.assertEqual([c.get_method() for c in broker], ['POST', 'GET', 'GET', 'GET'])
            self.assertTrue(all(c.full_url.endswith('/123-456-1') and c.data is None for c in broker[1:]))
            self.assertEqual(sum(c.full_url.startswith('https://actions.example/') for c in self.calls), 4)
            self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, *([pending] * 7)))
            self.assertEqual(self.sleeps, [2, 4, 8, 16, 30])
            self.assertIn('inspect retained request 123-456-1', self.log)

    def test_poll_deadline_stops_before_next_read(self):
        pending = {**RECEIPT, 'workflowRunId': None, 'workflowRunUrl': None}
        with patch('time.monotonic', side_effect=[0, 91]):
            self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, pending))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])
        self.assertIn('inspect retained request 123-456-1', self.log)

    def test_late_assertion_or_receipt_cannot_exceed_reconciliation_deadline(self):
        pending = {**RECEIPT, 'workflowRunId': None, 'workflowRunUrl': None}
        for durations, expected_calls in (([0, 0, 91], 3), ([0, 0, 0, 91], 4)):
            with self.subTest(durations=durations):
                self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, pending,
                                                   RECEIPT, elapsed=durations))
                self.assertEqual(len(self.calls), expected_calls)
                self.assertIn('deadline exceeded', self.log)
                self.assertIn('inspect retained request 123-456-1', self.log)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_assertion_and_receipt_timeouts_use_remaining_budget(self):
        pending = {**RECEIPT, 'workflowRunId': None, 'workflowRunUrl': None}
        self.assertTrue(self.run_workflow({'value': 'assertion-secret'}, pending,
                                          pending, RECEIPT, elapsed=[0, 0, 80, 0, 0, 0]))
        self.assertEqual(self.timeouts[:4], [30, 30, 30, 10])
        self.assertEqual(self.timeouts[4:], [10, 10])

    def test_identity_is_renewed_before_each_retained_receipt_read(self):
        pending = {**RECEIPT, 'workflowRunId': None, 'workflowRunUrl': None}
        self.assertTrue(self.run_workflow({'value': 'assertion-secret'}, pending, RECEIPT))
        self.assertEqual([urllib.parse.urlsplit(c.full_url).hostname for c in self.calls],
                         ['actions.example', 'broker.example', 'actions.example', 'broker.example'])
        self.assertEqual(self.calls[2].get_header('Authorization'), 'Bearer identity-secret')
        self.assertEqual(self.calls[3].get_header('Authorization'), 'Bearer assertion-secret')

    def test_read_identity_failure_stops_without_another_broker_request(self):
        pending = {**RECEIPT, 'status': 'dispatching', 'workflowRunId': None, 'workflowRunUrl': None}
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, pending,
                                          refresh_error=urllib.error.URLError('secret-error-body')))
        self.assertIn('Actions identity unavailable; inspect request 123-456-1', self.log)
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST', 'GET'])

    def test_executor_pin_mismatch_is_explicit_and_never_polled_or_accepted(self):
        mismatch = {**RECEIPT, 'executorCommit': 'c' * 40}
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, mismatch))
        self.assertIn('executor pin mismatch', self.log)
        self.assertEqual(len(self.calls), 2)

    def test_invalid_receipt_cannot_report_success(self):
        invalid = [None, [], {}, {**RECEIPT, 'extra': 1}]
        for key, values in {
            'schemaVersion': [True, 2, '1'], 'requestId': ['other-id'], 'executorCommit': ['c' * 40],
            'status': ['unknown', []], 'workflowRunId': [True, 0, -1, '789', None],
            'workflowRunUrl': [None, 'https://github.com/other/repo/actions/runs/789',
                               'https://github.com/yamonco/fractalops/actions/runs/790',
                               'https://github.com/yamonco/fractalops/actions/runs/789\ninjected=value'],
        }.items():
            invalid.extend({**RECEIPT, key: value} for value in values)
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, receipt, receipt))

    def test_rejection_never_retries_post(self):
        for response in [http_error(400), http_error(401), http_error(403), http_error(409), http_error(302), {**RECEIPT, 'status': 'rejected', 'workflowRunId': None, 'workflowRunUrl': None}]:
            self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, response))
            self.assertEqual(len(self.calls), 2)

    def test_readback_failure_never_reports_success_or_retries(self):
        for response in [http_error(404), http_error(503), TimeoutError(), b'invalid']:
            self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, TimeoutError(), *([response] * 6)))
            self.assertLessEqual(len(self.calls), 14)

    def test_resolution_transport_is_authenticated_identical_and_frozen(self):
        self.assertNotIn('IMAGE_BUILD_COMMIT', self.env)
        self.assertTrue(self.run_workflow())
        self.assertEqual([c.get_method() for c in self.transport_calls], ['GET', 'POST', 'POST'])
        identity, resolve, submit = self.transport_calls
        self.assertEqual(resolve.full_url, submit.full_url + '/resolve')
        self.assertEqual(resolve.data, submit.data)
        self.assertEqual(resolve.get_header('Authorization'), 'Bearer assertion-secret')
        self.assertIsNone(resolve.get_header('If-match'))
        self.assertEqual(submit.get_header('If-match'), '"' + 'b' * 40 + '"')
        self.assertIn('executor_commit=' + 'b' * 40 + '\n', self.output.read_text())
        for text in (SCRIPT, ACTION.read_text(), WORKFLOW.read_text()):
            self.assertNotIn('IMAGE_BUILD_COMMIT', text)
            self.assertNotIn('executor-commit', text)
        self.assertIn('executor_commit:', ACTION.read_text())
        self.assertEqual(WORKFLOW.read_text().count('executor_commit:'), 2)

    def test_resolved_commit_is_the_only_authority(self):
        commit = 'c' * 40
        self.assertTrue(self.run_workflow(
            {'value': 'assertion-secret'}, {**RECEIPT, 'executorCommit': commit},
            resolution={'executorRepository': 'yamonco/fractalops', 'executorCommit': commit},
            resolution_headers={'ETag': '"' + commit + '"', 'Cache-Control': 'no-store'},
        ))
        self.assertEqual(self.transport_calls[-1].get_header('If-match'), '"' + commit + '"')
        self.assertIn('executor_commit=' + commit + '\n', self.output.read_text())

    def test_invalid_resolution_fails_before_admission(self):
        valid = {'executorRepository': 'yamonco/fractalops', 'executorCommit': 'b' * 40}
        invalid = [{}, [], {'extra': 1, **valid}, b'invalid', b'x' * 16385,
                   http_error(302), http_error(401), http_error(500), TimeoutError()]
        for field, values in {
            'executorRepository': ['other/fractalops', 'yamonco/other', None],
            'executorCommit': ['B' * 40, 'main', 'a' * 39, None, 123],
        }.items():
            invalid.extend({**valid, field: value} for value in values)
        for resolution in invalid:
            with self.subTest(resolution=resolution):
                self.assertFalse(self.run_workflow(resolution=resolution))
                self.assertIn('no build requested', self.log)
                self.assertEqual(len(self.transport_calls), 2)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 0)

    def test_resolution_requires_exact_strong_etag_and_no_store(self):
        for headers in ({}, {'ETag': '"' + 'b' * 40 + '"'},
                        {'ETag': 'W/"' + 'b' * 40 + '"', 'Cache-Control': 'no-store'},
                        {'ETag': 'b' * 40, 'Cache-Control': 'no-store'},
                        {'ETag': '"' + 'c' * 40 + '"', 'Cache-Control': 'no-store'},
                        {'ETag': '"' + 'b' * 40 + '"', 'Cache-Control': 'public'}):
            with self.subTest(headers=headers):
                self.assertFalse(self.run_workflow(resolution_headers=headers))
                self.assertEqual(len(self.transport_calls), 2)

    def test_ambiguity_and_changed_receipt_never_resolve_or_submit_again(self):
        changed = {**RECEIPT, 'executorCommit': 'c' * 40}
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, TimeoutError(), changed))
        self.assertIn('executor pin mismatch', self.log)
        self.assertEqual(sum(c.full_url.endswith('/resolve') for c in self.transport_calls), 1)
        self.assertEqual(sum(c.get_method() == 'POST' and not c.full_url.endswith('/resolve')
                             for c in self.transport_calls), 1)

    def test_412_does_not_adopt_changed_authority_or_resolve_again(self):
        response = http_error(412)
        response.headers['ETag'] = '"' + 'c' * 40 + '"'
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, response))
        self.assertIn('HTTP 412', self.log)
        self.assertEqual([c.get_method() for c in self.transport_calls], ['GET', 'POST', 'POST'])
        self.assertEqual(self.transport_calls[-1].get_header('If-match'), '"' + 'b' * 40 + '"')
        self.assertEqual(self.sleeps, [])

    def test_failed_transport_has_action_ownership_and_automatic_context(self):
        for code, cause, action in (
            (401, 'Authentication or authorization', 'No developer configuration change required'),
            (403, 'Authentication or authorization', 'No developer configuration change required'),
            (409, 'Approval or build profile', 'Central BuildPlane must repair approval bindings'),
            (412, 'no build was admitted', 'do not change pins'),
        ):
            with self.subTest(code=code):
                self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, http_error(code)))
                self.assertIn(cause, self.summary_text)
                self.assertIn(action, self.summary_text)
                self.assertIn('Central BuildPlane owner:', self.summary_text)
                self.assertIn('https://github.com/yamonco/example/actions/runs/456/attempts/1', self.summary_text)
                self.assertIn('Request: ' + RECEIPT['requestId'], self.summary_text)
                self.assertIn('a' * 40, self.summary_text)
                self.assertIn('Frozen executor: ' + 'b' * 40, self.summary_text)
                self.assertIn('No infrastructure evidence form or manual coordinator request is required', self.summary_text)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_missing_resolution_api_is_central_deployment_failure(self):
        for code in (404, 405):
            with self.subTest(code=code):
                self.assertFalse(self.run_workflow(resolution=http_error(code)))
                self.assertIn('deployed BuildPlane API does not provide', self.summary_text)
                self.assertIn('do not change project configuration or retry', self.summary_text)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 0)

    def test_identity_and_resolution_auth_errors_keep_status_without_admission(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.assertFalse(self.run_workflow(http_error(code)))
                self.assertIn('Authentication or authorization', self.summary_text)
                self.assertIn(f'HTTP status: {code}', self.summary_text)
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(self.run_workflow(resolution=http_error(code)))
                self.assertIn('Authentication or authorization', self.summary_text)
                self.assertIn(f'HTTP status: {code}', self.summary_text)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 0)

    def test_uncertain_transport_and_pending_receipt_retain_original_request(self):
        pending = {**RECEIPT, 'status': 'outcome_unknown', 'workflowRunId': None, 'workflowRunUrl': None}
        for response in (http_error(429), http_error(503), TimeoutError('secret-error-body'), pending):
            with self.subTest(response=response):
                self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, response, *([pending] * 6)))
                self.assertIn('outcome is unconfirmed', self.summary_text)
                self.assertIn('do not rerun or submit another build', self.summary_text)
                self.assertIn('Preserve the retained request', self.summary_text)
                if isinstance(response, urllib.error.HTTPError):
                    self.assertIn(f'Admission HTTP status: {response.code}', self.summary_text)
                self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_invalid_receipt_and_resolution_give_security_recovery(self):
        for receipt in ({**RECEIPT, 'executorCommit': 'c' * 40},
                        {**RECEIPT, 'workflowRunUrl': 'https://evil.example/\n::warning::injected'}):
            with self.subTest(receipt=receipt):
                self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, receipt))
                self.assertIn('failed security validation', self.summary_text)
                self.assertIn('do not trust returned run links or submit again', self.summary_text)
                self.assertNotIn('evil.example', self.log + self.summary_text)
                self.assertNotIn('::warning::injected', self.log + self.summary_text)
        self.assertFalse(self.run_workflow(resolution={'executorRepository': 'evil/injected'}))
        self.assertIn('failed security validation', self.summary_text)
        self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 0)

    def test_untrusted_config_and_http_error_cannot_inject_commands_or_links(self):
        self.env['PROFILE'] = 'bad\n::warning::injected https://evil.example/?identity-secret'
        self.assertFalse(self.run_workflow())
        self.assertIn('Caller configuration', self.summary_text)
        self.assertIn('Product owner:', self.summary_text)
        self.assertIn('PROFILE: unavailable', self.summary_text)
        self.assertNotIn('evil.example', self.log + self.summary_text)
        self.assertNotIn('::warning::injected', self.log + self.summary_text)
        self.assertEqual(self.transport_calls, [])
        self.env['PROFILE'] = 'default'
        error = urllib.error.HTTPError('https://evil.example', 403,
                                      'identity-secret\n::warning::injected', {},
                                      io.BytesIO(b'assertion-secret https://evil.example ::warning::injected'))
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, error))
        self.assertNotIn('evil.example', self.log + self.summary_text)
        self.assertNotIn('::warning::injected', self.log + self.summary_text)
        self.assertEqual(self.log.count('::error '), 1)

    def test_readback_412_does_not_claim_no_admission_after_uncertain_post(self):
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, TimeoutError(), http_error(412)))
        self.assertIn('outcome is unconfirmed', self.summary_text)
        self.assertIn('Preserve the retained request', self.summary_text)
        self.assertNotIn('no build was admitted', self.summary_text)
        self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_invalid_identity_endpoint_is_configuration_and_output_failure_retains_build(self):
        self.env['ACTIONS_ID_TOKEN_REQUEST_URL'] = 'https://identity-secret@evil.example/token'
        self.assertFalse(self.run_workflow())
        self.assertIn('Caller configuration', self.summary_text)
        self.assertEqual(self.transport_calls, [])
        self.env['ACTIONS_ID_TOKEN_REQUEST_URL'] = 'https://actions.example/token'
        self.env['GITHUB_OUTPUT'] = str(self.root / 'missing' / 'output')
        self.assertFalse(self.run_workflow())
        self.assertIn('confirmed build dispatch', self.summary_text)
        self.assertIn('do not submit again', self.summary_text)
        self.assertNotIn('no build requested', self.summary_text)
        self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_summary_write_failure_preserves_original_failure(self):
        self.env['GITHUB_STEP_SUMMARY'] = str(self.root / 'missing' / 'summary')
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, http_error(412)))
        self.assertIn('HTTP 412', self.log)
        self.assertIn('::error ', self.log)
        self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_redirect_handler_refuses_credential_forwarding(self):
        namespace = {'__name__': 'test'}
        exec(compile(SCRIPT, str(WORKFLOW), 'exec'), namespace)
        handler = namespace['NoRedirect']()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_workflow_permissions_and_no_private_checkout_or_machine_credential(self):
        source = WORKFLOW.read_text()
        template = WORKFLOW.parents[2] / 'workflow-templates/remote-project-review.yml'
        self.assertIn('id-token: write', source)
        self.assertIn('id-token: write', template.read_text())
        self.assertNotIn('[self-hosted, delivery]', source)
        for forbidden in ('YAMON_REMOTE_BUILD_TOKEN', 'GITOPS_MACHINE_TOKEN', 'GH_TOKEN', 'actions/checkout', '/dispatches', 'Langboard', 'PRODUCT_RUNNER'):
            self.assertNotIn(forbidden, source)



class AutomaticReviewDiscoveryTest(unittest.TestCase):
    def setUp(self):
        namespace = {"__name__": "test"}
        exec(compile(SCRIPT, str(ACTION.parent / "request.py"), "exec"), namespace)
        self.discover = namespace.get("current_pull_request")
        self.assertIsNotNone(self.discover, "Shared review caller must discover its current PR")
        self.pull = {"number": 12, "state": "open", "head": {
            "repo": {"full_name": "Langboard/langboard"}, "ref": "review/canary-test", "sha": "a" * 40},
            "base": {"repo": {"full_name": "Langboard/langboard"}}}
        self.calls = []

    def discover_with(self, payload, headers=None):
        class Opener:
            def open(inner, request, timeout):
                self.calls.append(request)
                response = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                response.headers = headers or {}
                return response
        with patch.dict(os.environ, {"GITHUB_TOKEN": "read-only-test-token"}, clear=True):
            return self.discover(Opener(), "Langboard/langboard", "refs/heads/review/canary-test", "a" * 40)

    def test_exact_same_repository_branch_and_commit(self):
        self.assertEqual(self.discover_with([self.pull]), 12)
        request = self.calls[0]
        self.assertEqual(request.get_method(), "GET")
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme + "://" + url.netloc + url.path,
                         "https://api.github.com/repos/Langboard/langboard/pulls")
        self.assertEqual(urllib.parse.parse_qs(url.query)["head"], ["Langboard:review/canary-test"])

    def test_missing_ambiguous_stale_fork_closed_and_incomplete_fail_closed(self):
        for field, value in (("sha", "b" * 40), ("ref", "other"), ("repo", {"full_name": "fork/langboard"})):
            pull = json.loads(json.dumps(self.pull)); pull["head"][field] = value
            with self.subTest(field=field), self.assertRaises(SystemExit):
                self.discover_with([pull])
        for pulls in ([], [self.pull, self.pull], {}, b"not json", b"x" * 1048577):
            with self.subTest(pulls_type=type(pulls).__name__), self.assertRaises(SystemExit):
                self.discover_with(pulls)
        for field, value in (("number", True), ("number", 0), ("state", "closed"),
                             ("base", {"repo": {"full_name": "other/repo"}})):
            pull = json.loads(json.dumps(self.pull)); pull[field] = value
            with self.subTest(field=field), self.assertRaises(SystemExit):
                self.discover_with([pull])
        with self.assertRaises(SystemExit):
            self.discover_with([self.pull], {"Link": '<next>; rel="next"'})

    def test_missing_credential_does_not_make_a_request(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            self.discover(None, "Langboard/langboard", "refs/heads/review/canary-test", "a" * 40)

if __name__ == '__main__':
    unittest.main()
