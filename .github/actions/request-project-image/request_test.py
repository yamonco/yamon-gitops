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


WORKFLOW = Path(__file__).resolve().parents[2] / 'workflows/remote-project-image.yml'
ACTION = WORKFLOW.parents[1] / 'actions/request-project-image/action.yml'
SCRIPT = (ACTION.parent / 'request.py').read_text()

RECEIPT = {
    'schemaVersion': 1, 'requestId': '123-456-1-default', 'status': 'dispatched',
    'executorCommit': 'b' * 40, 'workflowRunId': 789,
    'workflowRunUrl': 'https://github.com/yamonco/fractalops/actions/runs/789',
}


def http_error(code):
    return urllib.error.HTTPError('https://broker.example', code, 'secret-error-body', {}, io.BytesIO())


class RemoteImageContractTest(unittest.TestCase):
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
            'SOURCE_CHANNEL': 'default', 'SOURCE_REPOSITORY': 'yamonco/example',
            'SOURCE_COMMIT': 'a' * 40, 'PROFILE': 'default', 'REQUEST_ID': '123-456-1',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push',
            'GITHUB_EVENT_PATH': str(self.event), 'GITHUB_OUTPUT': str(self.output),
            'GITHUB_STEP_SUMMARY': str(self.summary),
            'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://actions.example/token?x=1&audience=old',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'identity-secret',
        }
        self.calls = []
        self.sleeps = []

    def run_workflow(self, *responses, elapsed=None, resolution=None, resolution_headers=None):
        self.calls = []
        self.transport_calls = []
        self.output.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        self.sleeps = []
        replies = iter(responses or ({'value': 'assertion-secret'},
                                    dict(RECEIPT, requestId='123-456-1-' + self.env['PROFILE'])))

        self.timeouts = []
        durations = iter(elapsed or ())
        def open_request(request, timeout):
            self.transport_calls.append(request)
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

    def test_default_and_canary_success_exact_payload_and_outputs(self):
        for channel, ref in [('default', 'refs/heads/main'), ('canary', 'refs/heads/deploy/canary')]:
            with self.subTest(channel=channel):
                self.env.update(SOURCE_CHANNEL=channel, GITHUB_REF=ref, PROFILE='api')
                self.assertTrue(self.run_workflow())
                self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
                self.assertEqual(json.loads(self.calls[1].data), {
                    'schemaVersion': 3, 'sourceRepository': 'yamonco/example', 'sourceCommit': 'a' * 40,
                    'sourceRef': ref, 'sourceChannel': channel, 'profile': 'api', 'requestId': '123-456-1-api',
                })
                self.assertEqual(self.calls[1].full_url, 'https://broker.example/v1/builds/project-images')
                self.assertEqual(self.calls[0].get_header('Authorization'), 'Bearer identity-secret')
                self.assertEqual(self.calls[1].get_header('Authorization'), 'Bearer assertion-secret')
                self.assertEqual(self.calls[1].get_header('If-match'), '"' + ('b' * 40) + '"')
                self.assertIsNone(self.calls[0].get_header('If-match'))
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0].full_url).query), {'x': ['1'], 'audience': ['fractalops-project-validation']})
                self.assertEqual(self.output.read_text(), 'executor_commit=' + 'b' * 40 + '\nrequest_id=123-456-1-api\nrelease_run_url=https://github.com/yamonco/fractalops/actions/runs/789\n')

    def test_cross_organization_source_preserves_generic_payload(self):
        self.env['SOURCE_REPOSITORY'] = 'Langboard/langboard'
        self.assertTrue(self.run_workflow())
        self.assertEqual(json.loads(self.calls[1].data)['sourceRepository'], 'Langboard/langboard')
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
        self.assertIn('release_run_url=' + RECEIPT['workflowRunUrl'], self.output.read_text())

    def test_v3_same_run_profiles_have_independent_requests_and_readbacks(self):
        request_ids = []
        for profile in ('api', 'runtime-base'):
            with self.subTest(profile=profile):
                self.env.update(PROFILE=profile)
                request_id = '123-456-1-' + profile
                receipt = dict(RECEIPT, requestId=request_id)
                self.assertTrue(self.run_workflow(
                    {'value': 'assertion-secret'}, http_error(500), receipt,
                ))
                payload = json.loads(self.calls[1].data)
                self.assertEqual(payload['schemaVersion'], 3)
                self.assertEqual(payload['profile'], profile)
                self.assertEqual(payload['requestId'], request_id)
                self.assertEqual(self.calls[-1].full_url,
                                 'https://broker.example/v1/builds/project-images/' + request_id)
                self.assertIn('request_id=' + request_id + '\n', self.output.read_text())
                request_ids.append(payload['requestId'])
        self.assertEqual(len(set(request_ids)), 2)

    def test_v3_rejects_a_receipt_from_a_different_profile(self):
        self.env.update(PROFILE='api')
        receipt = dict(RECEIPT, requestId='123-456-1-worker')
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, receipt, receipt))

    def test_numeric_legacy_receipt_is_rejected_without_another_post(self):
        receipt = dict(RECEIPT, requestId='123-456-1')
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, receipt, receipt))
        self.assertEqual(sum(c.get_method() == 'POST' for c in self.calls), 1)

    def test_malformed_repository_fails_before_network(self):
        for repository in ('', 'repo', '/repo', 'owner/', 'owner/repo/extra',
                           'owner name/repo', 'owner_name/repo', 'owner/repo name',
                           'a' * 40 + '/repo', 'owner/' + 'a' * 101):
            with self.subTest(repository=repository):
                self.env['SOURCE_REPOSITORY'] = repository
                self.assertFalse(self.run_workflow())
                self.assertEqual(self.calls, [])

    def test_bad_source_policy_fails_before_network(self):
        for name, value in [
            ('SOURCE_CHANNEL', 'review'), ('SOURCE_COMMIT', 'main'), ('SOURCE_REPOSITORY', 'owner/repo/extra'),
            ('PROFILE', 'bad profile'), ('REQUEST_ID', '123-456-0'), ('GITHUB_EVENT_NAME', 'pull_request'),
            ('GITHUB_EVENT_NAME', 'pull_request_target'), ('GITHUB_REF', 'refs/heads/other'),
            ('GITHUB_REF', 'refs/tags/main'), ('GITHUB_REF', 'refs/heads/deploy/../main'),
        ]:
            with self.subTest(name=name, value=value), patch.dict(self.env, {name: value}):
                self.assertFalse(self.run_workflow())
                self.assertEqual(self.calls, [])
        with patch.dict(self.env, {'SOURCE_CHANNEL': 'canary'}):
            self.assertFalse(self.run_workflow())
            self.assertEqual(self.calls, [])

    def test_missing_default_branch_fails_before_network(self):
        self.event.write_text('{}')
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
                self.assertEqual(self.calls[-1].full_url, 'https://broker.example/v1/builds/project-images/123-456-1-default')
                self.assertIsNone(self.calls[-1].data)
                self.assertIsNone(self.calls[-1].get_header('If-match'))

    def test_pending_receipts_read_back(self):
        for state in ('dispatching', 'outcome_unknown', 'dispatched'):
            pending = {**RECEIPT, 'status': state, 'workflowRunId': None, 'workflowRunUrl': None}
            self.assertTrue(self.run_workflow({'value': 'assertion-secret'}, pending, pending, pending, RECEIPT))
            self.assertEqual(self.sleeps, [2, 4])
            broker = [c for c in self.calls if c.full_url.startswith('https://broker.example')]
            self.assertEqual([c.get_method() for c in broker], ['POST', 'GET', 'GET', 'GET'])
            self.assertTrue(all(c.full_url.endswith('/123-456-1-default') and c.data is None for c in broker[1:]))
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

    def test_executor_precondition_failure_is_terminal_without_pin_change(self):
        response = http_error(412)
        response.headers['ETag'] = '"' + 'c' * 40 + '"'
        self.assertFalse(self.run_workflow({'value': 'assertion-secret'}, response))
        self.assertEqual([c.get_method() for c in self.calls], ['GET', 'POST'])
        self.assertEqual(self.calls[1].get_header('If-match'), '"' + 'b' * 40 + '"')
        self.assertEqual(self.sleeps, [])
        self.assertIn('HTTP 412', self.log)
        self.assertIn('no new build was admitted by this request', self.log)
        self.assertIn('frozen executor', self.log)
        self.assertIn('approved broker executor', self.log)
        self.assertNotIn(response.headers['ETag'], self.log)

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

    def assert_native_execution(self, source):
        self.assertIn("runs-on: ${{ vars.BUILD_REQUEST_RUNNER || 'fractalops-ci-native' }}", source)
        self.assertNotRegex(source, r'(?m)^\s+["\']?(?:container|services)["\']?\s*:')
        self.assertNotRegex(source, r'docker://|/var/run/docker.sock|--privileged')

    def test_native_caller_jobs_and_unsupported_container_modes(self):
        for name in ('remote-project-image.yml', 'remote-project-review.yml'):
            source = (WORKFLOW.parent / name).read_text()
            self.assert_native_execution(source)
            for addition in (
                '    container: ubuntu:24.04\n',
                '    services: {docker: {image: "docker:dind"}}\n',
                '    steps:\n      - uses: docker://python:3.12\n',
            ):
                with self.subTest(workflow=name, addition=addition):
                    mutated = source.replace('    steps:\n', addition + '    steps:\n', 1)
                    self.assertNotEqual(mutated, source)
                    with self.assertRaises(AssertionError):
                        self.assert_native_execution(mutated)

    def test_workflow_permissions_and_no_private_checkout_or_machine_credential(self):
        source = WORKFLOW.read_text()
        template = WORKFLOW.parents[2] / 'workflow-templates/remote-project-image.yml'
        self.assertIn('id-token: write', source)
        self.assertIn('id-token: write', template.read_text())
        self.assertNotIn('[self-hosted, delivery]', source)
        for forbidden in ('YAMON_REMOTE_BUILD_TOKEN', 'GITOPS_MACHINE_TOKEN', 'GH_TOKEN', 'actions/checkout', '/dispatches', 'Langboard', 'PRODUCT_RUNNER'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
