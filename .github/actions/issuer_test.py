"""Shared issuer failure contracts, exercised by both existing caller test suites."""

import contextlib
import errno
import io
import json
import os
import socket
import ssl
from unittest.mock import Mock, patch
import urllib.error

from build_request import fetch_actions_assertion


PRIVATE = 'eyJhbGciOiJSUzI1NiJ9.private.signature https://private.example/issuer?token=hidden ::warning::injected'


def issuer_http(code):
    return urllib.error.HTTPError('https://private.example/?token=hidden', code, PRIVATE,
                                  {'Authorization': PRIVATE}, io.BytesIO(PRIVATE.encode()))


class IssuerContract:
    def test_issuer_nontransient_categories_never_reach_resolution_or_admission(self):
        for error, category, number in (
            (issuer_http(401), 'http', None),
            (issuer_http(403), 'http', None),
            (issuer_http(302), 'http', None),
            (issuer_http(501), 'http', None),
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, PRIVATE)), 'tls', 1),
            (urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, PRIVATE)), 'dns', socket.EAI_AGAIN),
            (urllib.error.URLError(OSError(errno.ECONNREFUSED, PRIVATE)), 'os_error', errno.ECONNREFUSED),
            (urllib.error.URLError(PRIVATE), 'url_error', None),
            (ValueError(PRIVATE), 'invalid_response', None),
            (b'invalid ' + PRIVATE.encode(), 'invalid_response', None),
            ({'value': 123, 'private': PRIVATE}, 'invalid_response', None),
        ):
            with self.subTest(category=category, code=getattr(error, 'code', None)):
                self.assertFalse(self.run_workflow(identity_responses=[error]))
                self.assertEqual([r.get_method() for r in self.transport_calls], ['GET'])
                self.assertEqual(self.sleeps, [])
                self.assertIn('Issuer category: ' + category, self.log)
                self.assertIn('Issuer errno: ' + (str(number) if number is not None else 'unavailable'), self.log)
                auth = isinstance(error, urllib.error.HTTPError) and error.code in (401, 403)
                self.assertEqual('Authentication or authorization failed.' in self.log, auth)
                for forbidden in ('eyJhbGci', 'private.example', 'token=hidden', '::warning::injected'):
                    self.assertNotIn(forbidden, self.log + self.summary_text)

    def test_issuer_transient_failures_retry_only_one_get(self):
        for make_error, category in (
            (lambda: TimeoutError(PRIVATE), 'timeout'),
            (lambda: urllib.error.URLError(OSError(errno.ETIMEDOUT, PRIVATE)), 'timeout'),
            (lambda: urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, PRIVATE)), 'connection_reset'),
            *((lambda code=code: issuer_http(code), 'http') for code in (429, 500, 502, 503, 504)),
        ):
            with self.subTest(category=category):
                self.assertFalse(self.run_workflow(identity_responses=[make_error(), make_error()]))
                self.assertEqual([r.get_method() for r in self.transport_calls], ['GET', 'GET'])
                self.assertEqual(self.sleeps, [1])
                self.assertIn('Issuer category: ' + category, self.log)
                self.assertIn('No build requested', self.log)
                self.assertNotIn('Authentication or authorization failed.', self.log)
                self.assertNotIn('private.example', self.log + self.summary_text)
                self.assertNotIn('eyJhbGci', self.log + self.summary_text)
                self.assertNotIn('::warning::injected', self.log + self.summary_text)

    def test_issuer_retry_success_clears_error_before_single_admission(self):
        self.assertTrue(self.run_workflow(identity_responses=[
            ConnectionResetError(errno.ECONNRESET, PRIVATE), {'value': 'assertion-secret'},
        ]))
        self.assertEqual([r.get_method() for r in self.transport_calls], ['GET', 'GET', 'POST', 'POST'])
        self.assertEqual(self.sleeps, [1])
        self.assertNotIn('Issuer category:', self.log + self.summary_text)
        self.assertFalse(self.run_workflow(identity_responses=[
            ConnectionResetError(errno.ECONNRESET, PRIVATE), {'value': 'assertion-secret'},
        ], resolution=issuer_http(403)))
        self.assertIn('Phase: resolution', self.log)
        self.assertNotIn('Issuer category:', self.log + self.summary_text)

    def test_issuer_retry_never_extends_initial_or_parent_budget(self):
        for budget, parent, durations, count, expected_timeouts in (
            (0, None, [], 0, []),
            (30, None, [29.5], 1, [30]),
            (30, None, [28, 1], 2, [30, 1]),
            (0.5, None, [0.1], 1, [0.5]),
            (30, 5, [1, 0], 2, [5, 3]),
        ):
            with self.subTest(budget=budget, durations=durations):
                clock = [0]
                calls, sleeps = [], []
                elapsed = iter(durations)
                def open_request(request, timeout):
                    calls.append((request.get_method(), timeout))
                    clock[0] += next(elapsed)
                    raise TimeoutError(PRIVATE)
                def sleep(delay):
                    sleeps.append(delay)
                    clock[0] += delay
                context = {}
                with patch('time.monotonic', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=sleep):
                    with self.assertRaises(TimeoutError):
                        fetch_actions_assertion(Mock(open=open_request), 'https://private.example/?token=hidden',
                                                PRIVATE, context, timeout=budget, deadline=parent)
                self.assertEqual(len(calls), count)
                self.assertEqual([timeout for _, timeout in calls], expected_timeouts)
                self.assertTrue(all(method == 'GET' for method, _ in calls))
                self.assertLessEqual(clock[0], parent if parent is not None else budget)
                self.assertEqual(sleeps, [1] if count == 2 else [])
                self.assertEqual(context['identity_error'], 'timeout')

    def test_issuer_late_success_cannot_escape_budget_or_leak_response(self):
        clock = [0]
        def open_request(request, timeout):
            clock[0] = 31
            return io.BytesIO(json.dumps({'value': PRIVATE}).encode())
        context = {}
        with patch('time.monotonic', side_effect=lambda: clock[0]), patch('time.sleep') as sleep:
            with self.assertRaises(TimeoutError):
                fetch_actions_assertion(Mock(open=open_request), 'https://private.example', PRIVATE,
                                        context, timeout=30)
        sleep.assert_not_called()
        self.assertEqual(context['identity_error'], 'timeout')
        self.assertNotIn(PRIVATE, repr(context))

    def test_issuer_diagnostics_reject_untrusted_category_and_numeric_fields(self):
        from build_request import report_failure
        for category in (PRIVATE, 'tls'):
            output = io.StringIO()
            context = {'phase': 'identity', 'identity_error': category, 'identity_errno': PRIVATE, 'http': PRIVATE}
            with patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(output):
                report_failure('Actions identity unavailable', context, context_fields={
                    'REQUEST_ID': r'123-456-1', 'SOURCE_REPOSITORY': r'yamonco/example', 'PROFILE': r'default',
                }, request_suffix='')
            self.assertEqual('Issuer category:' in output.getvalue(), category == 'tls')
            if category == 'tls':
                self.assertIn('Issuer errno: unavailable', output.getvalue())
            self.assertNotIn('private.example', output.getvalue())
            self.assertNotIn('eyJhbGci', output.getvalue())
