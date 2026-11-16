"""Offline real HTTP entrypoint/session routing regressions."""
import http.client
import io
import json
import pathlib
import sys
import threading
import unittest
import tempfile
from types import SimpleNamespace
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import wb_proxy as P


class QuietHandler(P.Handler):
    def log_message(self, fmt, *args):
        pass


class SessionHTTPFixture(unittest.TestCase):
    # A test mixin: all dispatch and pool methods are production implementations.
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            '_panel_ok': False, '_cross_realm_error': None,
            '_banned_model_error': None, '_key_model_error': None,
        }.items():
            self.stack.enter_context(patch.object(P.Handler, name, return_value=value))
        for name, value in {'identify_key': {'id': 'key-a', 'key': 'fake-a'},
                            'auth_required': True, '_anthropic_messages_enabled': True}.items():
            self.stack.enter_context(patch.object(P, name, return_value=value))
        self.calls = []

        def upstream(payload, session_key=None, target_realm=None, **kwargs):
            self.calls.append((session_key, target_realm, payload))
            raise RuntimeError('offline routing observed')

        self.stack.enter_context(patch.object(P, 'open_upstream_with_fallback', side_effect=upstream))
        self.stack.enter_context(patch.object(P, 'record_error'))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, extra=None, headers=None):
        body = {'model': 'offline-model', 'messages': [{'role': 'user', 'content': 'hello'}],
                'input': 'hello', 'max_tokens': 32}
        body.update(extra or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        conn.request('POST', path, json.dumps(body), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer fake-a',
            'X-Realm': 'intl', **(headers or {})})
        resp = conn.getresponse()
        status, raw = resp.status, resp.read()
        conn.close()
        return status, raw


class SessionRoutingTest(SessionHTTPFixture):
    def test_metadata_variants_survive_translation(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
            for field in ('conversationId', 'session_id', 'sessionId'):
                with self.subTest(path=path, field=field):
                    self.request(path, {'metadata': {field: '  metadata-session  '}})
                    self.assertEqual(self.calls[-1][0], 'metadata-session')

    def test_top_level_session_survives_messages_translation(self):
        self.request('/v1/messages', {'session_id': 'messages-session'})
        self.assertEqual(self.calls[-1][0], 'messages-session')

    def test_only_valid_strings_and_original_precedence(self):
        self.assertEqual(P.extract_session_key({'x-conversation-id': ' first ',
            'X-Session-ID': 'second'}, {'conversation_id': 'third'}), 'first')
        self.assertEqual(P.extract_session_key({'X-Session-Id': 'old',
            'X-Claude-Code-Session-Id': 'new'}, {}), 'old')
        for bad in (None, False, 7, [], {}, '', '  ', 'bad\nid', 'bad\x00id'):
            with self.subTest(bad=bad):
                self.assertEqual(P.extract_session_key({'X-Conversation-Id': bad},
                    {'conversation_id': bad, 'metadata': {'sessionId': 'valid'}}), 'valid')
        for metadata in (None, 'bad', [1], 4):
            self.assertIsNone(P.extract_session_key({}, {'metadata': metadata}))
        self.assertIsNone(P.extract_session_key({}, {'metadata': {'user_id': 'u', 'installID': 'i'}}))

    def test_claude_header_reaches_all_entrypoints(self):
        for path in ('/v1/chat/completions', '/chat/completions', '/v1/messages', '/messages',
                     '/v1/responses', '/responses', '/v1/responses/compact', '/responses/compact'):
            with self.subTest(path=path):
                before = len(self.calls)
                self.request(path, headers={'x-cLaUdE-CoDe-SeSsIoN-iD': '  claude-session  '})
                self.assertEqual(len(self.calls), before + 1)
                self.assertEqual(self.calls[-1][0], 'claude-session')


class BindingIsolationTest(SessionHTTPFixture):
    def setUp(self):
        super().setUp()
        # Remove only the synthetic upstream patch; exercise real routing below it.
        self.stack.close()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.pool = P.wb_accounts.AccountPool(tmp)
        self.pool.accounts = [P.wb_accounts.Account(
            {'uid': realm + str(i), 'accessToken': 'offline', 'realm': realm},
            str(pathlib.Path(tmp) / (realm + str(i) + '.json')))
            for realm in ('intl', 'cn') for i in range(2)]
        self.sent = []
        def upstream(req, **kwargs):
            self.sent.append((dict(req.header_items()), json.loads(req.data)))
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        for name, value in {'_panel_ok': False, '_cross_realm_error': None,
                            '_banned_model_error': None, '_key_model_error': None}.items():
            self.stack.enter_context(patch.object(P.Handler, name, return_value=value))
        entries = [{'id': 'key-' + k, 'key': 'fake-' + k, 'enabled': True} for k in ('a', 'b')]
        self.entries = entries
        self.stack.enter_context(patch.object(P, 'configured_keys', return_value=entries))
        self.stack.enter_context(patch.object(P.wb_settings, 'api_keys', return_value=entries))
        for name in ('apply_daily_guards', 'apply_reserve_credits', 'record_error',
                     'record_upstream_usage', 'reset_switch_counter'):
            self.stack.enter_context(patch.object(P, name))
        for name, value in {'_anthropic_messages_enabled': True, 'restriction_hold_remaining': 0,
                            'remaining_priority_enabled': False, 'auto_switch_product_enabled': False,
                            'prompt_cache_key_enabled': False, 'upstream_timeouts': (2, 2)}.items():
            self.stack.enter_context(patch.object(P, name, return_value=value))
        self.stack.enter_context(patch.object(P, 'POOL', self.pool))
        self.stack.enter_context(patch.object(P.wb_accounts, 'urlopen', side_effect=upstream))
        self.stack.enter_context(patch.object(P, '_apply_stream_idle_timeout'))
        self.stack.enter_context(patch.object(P.wb_settings, 'egress_backoff_enabled', return_value=False))

    def test_codearts_bindings_are_key_scoped_without_changing_client_chat_id(self):
        C = P.wb_codearts
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        pool = C.pool_for(tmp)
        pool._affinity = {}
        accounts = [SimpleNamespace(uid='ca' + str(i), active_concurrent=0, max_concurrent=4,
                    last_used=i, healthy=lambda now: True, has_model=lambda model: True)
                    for i in range(2)]
        pool.accounts = accounts
        seen = []
        def client(acct, body, session_key):
            seen.append((acct.uid, dict(body), session_key))
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), 'offline-model'
        with patch.object(C, 'load_config', return_value={'enabled': True, 'accounts_dir': 'offline'}), \
             patch.object(C, 'configured_config', return_value=True), \
             patch.object(C, 'pool_for', return_value=pool), \
             patch.object(C, '_ensure_token'), patch.object(C, 'ensure_models'), \
             patch.object(C, '_client_chat', side_effect=client), \
             patch.object(pool, 'acquire', return_value=True), patch.object(pool, 'note_success'):
            for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
                pool._affinity.clear()
                for key, session in (('a', 'client-session'), ('b', 'client-session'), ('a', 'other-session')):
                    status, raw = self.request(path, headers={'X-Realm': 'codearts',
                        'Authorization': 'Bearer fake-' + key, 'X-Claude-Code-Session-Id': session})
                    self.assertEqual(status, 200, raw)
                self.assertEqual(len(pool._affinity), 3, pool._affinity)
                self.assertEqual(seen[-1][2], 'other-session')
                self.assertEqual(seen[-1][1]['chat_id'], C._derive_chat_id('other-session', []))

    def test_streaming_entrypoints_keep_independent_sessions(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
            self.pool.affinity.bindings.clear()
            for key, session in (('a', 'one'), ('a', 'two'), ('b', 'one')):
                status, raw = self.request(path, {'stream': True},
                    {'Authorization': 'Bearer fake-' + key, 'X-Claude-Code-Session-Id': session})
                self.assertEqual(status, 200, raw)
                self.assertIn(b'ok', raw)
                self.assertNotIn(b'route-v1-', raw)
            self.assertEqual(len(self.pool.affinity.bindings), 3)

    def test_scope_resets_after_dispatch_and_secret_rotation_is_disjoint(self):
        observed = []
        finished = threading.Event()
        original_leave = P.wb_session.leave
        def leave(token):
            original_leave(token)
            observed.append(P.wb_session._PRINCIPAL.get())
            if len(observed) == 2:
                finished.set()
        with patch.object(P.wb_session, 'leave', side_effect=leave):
            self.request('/v1/chat/completions', headers={'X-Claude-Code-Session-Id': 'rotate'})
            self.entries[0]['key'] = 'rotated-a'
            self.request('/v1/chat/completions', headers={
                'Authorization': 'Bearer rotated-a', 'X-Claude-Code-Session-Id': 'rotate'})
            self.assertTrue(finished.wait(5))
        self.assertEqual(observed, [None, None])
        self.assertEqual(len(self.pool.affinity.bindings), 2)

    def test_prefix_fallback_keeps_page_selection_and_key_isolation(self):
        messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i)} for i in range(6)]
        with patch.object(P, 'AFFINITY_MAX_MSGS', 2), patch.object(P, 'AFFINITY_PAGE_SIZE', 3), \
             patch.object(self.pool, 'pick_for_session', wraps=self.pool.pick_for_session) as pick:
            for key in ('a', 'b'):
                status, raw = self.request('/v1/chat/completions', {'messages': messages},
                    {'Authorization': 'Bearer fake-' + key})
                self.assertEqual(status, 200, raw)
                expected_session, expected_page = P.affinity_route(self.sent[-1][1]['messages'])
                self.assertIsInstance(expected_session, str)
                self.assertTrue(str(expected_session).startswith('pfx-'))
                self.assertEqual(pick.call_args.kwargs['page'], expected_page)
            self.assertEqual(len(self.pool.affinity.bindings), 2)

    def test_account_permissions_remain_enforced_after_binding(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
            self.entries[0]['accounts'] = ['intl0']
            status, raw = self.request(path, headers={'X-Claude-Code-Session-Id': 'permissions'})
            self.assertEqual(status, 200, raw)
            self.entries[0]['accounts'] = ['intl1']
            status, raw = self.request(path, headers={'X-Claude-Code-Session-Id': 'permissions'})
            self.assertEqual(status, 200, raw)
            headers = {k.lower(): v for k, v in self.sent[-1][0].items()}
            self.assertEqual(headers['x-conversation-id'],
                             P.wb_identity.conversation_id_for('intl1', 'permissions'))

    def test_invalid_auth_and_previous_response_never_route(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
            status, raw = self.request(path, headers={'Authorization': 'Bearer invalid'})
            self.assertEqual(status, 401, raw)
        status, raw = self.request('/v1/responses', {'previous_response_id': 'not-stored'})
        self.assertEqual(status, 400, raw)
        self.assertIn(b'not supported', raw)
        self.assertFalse(self.sent)

    def test_key_realm_session_have_independent_real_bindings(self):
        for path in ('/v1/chat/completions', '/chat/completions', '/v1/completions', '/completions',
                     '/v1/messages', '/messages', '/v1/responses', '/responses',
                     '/v1/responses/compact', '/responses/compact'):
            self.pool.affinity.bindings.clear()
            for key, realm, session in (('a', 'intl', 'same'), ('b', 'intl', 'same'),
                                         ('a', 'cn', 'same'), ('a', 'intl', 'other')):
                status, raw = self.request(path, headers={'Authorization': 'Bearer fake-' + key,
                    'X-Realm': realm, 'X-Claude-Code-Session-Id': session})
                self.assertEqual(status, 200, raw)
                self.assertEqual(self.sent[-1][1]['model'], 'offline-model')
                # The routing hash is exclusively internal, not an upstream ID.
                headers = {k.lower(): v for k, v in self.sent[-1][0].items()}
                self.assertIn(headers['x-conversation-id'], [
                    P.wb_identity.conversation_id_for(a.uid, session) for a in self.pool.accounts])
            self.assertEqual(len(self.pool.affinity.bindings), 4, self.pool.affinity.bindings)
            before = dict(self.pool.affinity.bindings)
            self.request(path, headers={'X-Claude-Code-Session-Id': 'same'})
            self.assertEqual(set(before), set(self.pool.affinity.bindings))


if __name__ == '__main__':
    unittest.main(verbosity=2)
