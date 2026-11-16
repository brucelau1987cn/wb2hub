"""Credential-free regression tests of real AST-extracted account methods."""
import ast
import contextlib
import io
import json
import math
import os
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def account_code():
    tree = ast.parse((ROOT / 'wb_accounts.py').read_text())
    ns = dict(json=json, math=math, re=re, os=os, time=time, urllib=__import__('urllib'),
              threading=threading, http_json=Mock(),
              get_realm_config=lambda _: {'billing_upstream': 'https://offline.invalid'},
              CHECKIN_PATH='/checkin', urlopen=Mock(),
              wb_settings=SimpleNamespace(daily_chat_web=lambda _: True,
                  daily_chat_web_fallback_activity=lambda _: True),
              is_tls_verify_text=lambda text: 'CERTIFICATE_VERIFY_FAILED' in str(text),
              _auth_failure_text=lambda _: False,
              unverified_soft_backoff=lambda count: 60 * 2 ** (count - 1))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                t.id.startswith('CHECKIN_') and t.id.endswith('HINTS') for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
        if isinstance(node, ast.FunctionDef) and node.name not in {
                'http_json', 'get_realm_config', 'urlopen', '_auth_failure_text',
                'is_tls_verify_text', 'unverified_soft_backoff'}:
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
        if isinstance(node, ast.ClassDef) and node.name == 'Account':
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name in {
                    '_checkin_upstream', 'note_error', 'note_unscoped_rate', '_decay_soft_streak',
                    'can_daily_chat', '_daily_chat_web_step', '_daily_chat_desktop_once',
                    'daily_chat_web'}:
                    exec(compile(ast.Module(body=[method], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
    return ns


class TestCheckinClassification(unittest.TestCase):
    def test_already_is_distinct_and_explicit_failure_has_priority(self):
        classify = account_code()['checkin_state']
        cases = [(0, '今天已签到', 'already'),
                 (0, '签到失败，已签到天数不足7天，请稍后再试', 'failed'),
                 (10001, '签到失败，已签到天数不足7天', 'failed'),
                 (0, 'checkin failed; already checked in', 'failed'),
                 (0, '签到成功，距离7天奖励还不足', 'checked'),
                 (10001, 'already checked in today', 'already'),
                 (0, '请求处理中，请勿重复操作', 'pending')]
        for code, msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertEqual(classify(code, msg), expected)

    def test_terminal_codes_never_become_pending_from_retry_wording(self):
        classify = account_code()['checkin_state']
        for code in (11140, 14003, 6004, 6005, 401, 429):
            with self.subTest(code=code):
                self.assertEqual(classify(code, '系统繁忙，请稍后再试'), 'failed')

    def test_real_checkin_preserves_flat_and_enveloped_credit_without_guessing(self):
        cases = [({'credit': 37, 'streak_days': 8, 'is_streak_day': False},
                  'checked', True, 37, 8),
                 ({'code': 0, 'data': {'credit': 0, 'streak_days': 9}},
                  'checked', True, 0, 9),
                 ({'code': 0, 'msg': 'ok'}, 'checked', False, None, None),
                 ({'code': 0, 'msg': '今天已签到', 'data': {'credit': 100}},
                  'already', False, None, None)]
        for payload, state, known, credit, streak in cases:
            with self.subTest(payload=payload):
                ns = account_code()
                ns['http_json'].return_value = payload
                acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
                result = ns['_checkin_upstream'](acc)
                self.assertTrue(result['ok'])
                self.assertEqual(result['state'], state)
                self.assertEqual(result.get('credit_known'), known)
                self.assertEqual(result.get('credit'), credit)
                self.assertEqual(result.get('streak_days'), streak)
                self.assertIsNotNone(acc.last_checkin)

    def test_malformed_flat_receipt_does_not_latch_daily_success(self):
        for payload in ({}, {'credit': None}, {'credit': '100'}, {'streak_days': 'nope'},
                        {'credit': True}, {'credit': -2}):
            ns = account_code()
            ns['http_json'].return_value = payload
            acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
            result = ns['_checkin_upstream'](acc)
            self.assertFalse(result['ok'], payload)
            self.assertIsNone(acc.last_checkin)

    def test_http_400_business_pending_remains_pending_for_scheduler(self):
        ns = account_code()
        ns['http_json'].side_effect = urllib.error.HTTPError('https://offline.invalid', 400,
            'pending', {}, io.BytesIO(json.dumps({'code': 10001, 'msg': '请求处理中，请勿重复操作'}).encode()))
        acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
        result = ns['_checkin_upstream'](acc)
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(result['ok'])
        self.assertIsNone(acc.last_checkin)

    def test_transient_http_does_not_override_terminal_business_failure(self):
        for code, msg in [(11140, '账号已被限制使用'), (14003, 'too many requests'),
                          (6004, 'quota exhausted'), (10001, '签到失败，不支持')]:
            ns = account_code()
            ns['http_json'].side_effect = urllib.error.HTTPError('https://offline.invalid',
                503, 'error', {}, io.BytesIO(json.dumps({'code': code, 'msg': msg}).encode()))
            acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
            self.assertIs(ns['_checkin_upstream'](acc)['retryable'], False)

    def test_only_transport_and_server_errors_are_retryable(self):
        for status, body, retryable in [
                (503, b'{"code":500,"msg":"busy"}', True),
                (502, b'bad gateway', True),
                (429, b'{"code":0,"msg":"request processing"}', False),
                (403, b'{"code":11140,"msg":"request illegal"}', False),
                (400, '{"code":10001,"msg":"今天已签到"}'.encode(), False)]:
            with self.subTest(status=status):
                ns = account_code()
                ns['http_json'].side_effect = urllib.error.HTTPError(
                    'https://offline.invalid', status, 'error', {}, io.BytesIO(body))
                acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
                result = ns['_checkin_upstream'](acc)
                self.assertEqual(result.get('retryable'), retryable)
                if status == 400:
                    self.assertTrue(result['ok'])
                    self.assertIs(result['credit_known'], False)
                else:
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['state'], 'failed')
                    self.assertIsNone(acc.last_checkin)
        for error in [urllib.error.URLError('connection reset'), TimeoutError('timed out')]:
            ns = account_code()
            ns['http_json'].side_effect = error
            acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {}, last_checkin=None)
            self.assertIs(ns['_checkin_upstream'](acc).get('retryable'), True)
        ns = account_code()
        ns['http_json'].side_effect = ValueError('invalid JSON')
        acc = SimpleNamespace(path='', proxy=None, headers=lambda **_: {})
        self.assertIs(ns['_checkin_upstream'](acc).get('retryable'), False)


class TestRestriction(unittest.TestCase):
    def test_code_requires_account_restriction_corroboration(self):
        classify = account_code().get('restriction_is_account_level', lambda *args: False)
        cases = [(403, json.dumps({'code': 11140, 'msg': 'request illegal',
                     'displayMsg': {'en': 'Your account has been restricted due to unusual activity',
                                    'zh': '账号存在异常行为，已被限制使用'}}), True),
                 (403, '{"code":11140,"msg":"request illegal"}', False),
                 (11140, 'request illegal', False),
                 (403, json.dumps({'code': 11140, 'msg': 'request illegal',
                    'displayMsg': '账号存在异常行为，已被限制使用'}), True),
                 (11140, 'Account has been restricted due to abnormal behavior', True),
                 (403, '{"error":{"code":"11140","displayMsg":"account has been banned"}}', True),
                 (403, '{"code":11140,"msg":"request content restricted by policy"}', False),
                 (403, '{"code":11140,"msg":"model is restricted for this account"}', False),
                 (11140, 'account is not restricted', False),
                 (403, '{"code":11140,"msg":"account quota exceeded"}', False),
                 (403, '{"code":10001,"msg":"account has been restricted"}', False)]
        for code, detail, expected in cases:
            with self.subTest(code=code, detail=detail):
                self.assertEqual(classify(code, detail), expected)

    def test_daily_uses_shared_classifier_without_banning_generic_11140(self):
        import sys
        proxy = SimpleNamespace(CHAT_PATH='/chat', intl_model_alias=lambda m, _: m,
                                build_upstream_body=lambda b: b)
        for message, banned in [('request illegal', False),
                                ('账号存在异常行为，已被限制使用', True)]:
            ns = account_code()
            body = json.dumps({'code': 11140, 'displayMsg': message}).encode()
            ns['urlopen'].side_effect = urllib.error.HTTPError(
                'https://offline.invalid', 403, 'error', {}, io.BytesIO(body))
            acc = SimpleNamespace(realm='intl', path='', proxy=None,
                chat_base_url=lambda: 'https://offline.invalid',
                headers=lambda *a, **k: {}, is_cli_identity=lambda: False,
                note_restricted=Mock())
            with patch.dict(sys.modules, wb_proxy=proxy):
                result = ns['_daily_chat_desktop_once'](acc, 'free-model')
            self.assertEqual(bool(result.get('restricted')), banned)
            self.assertEqual(acc.note_restricted.called, banned)


class TestCooldown(unittest.TestCase):
    def test_concurrent_same_model_failure_does_not_slide_active_window(self):
        ns = account_code()
        clock = SimpleNamespace(time=Mock(return_value=1000))
        ns['time'] = clock
        acc = SimpleNamespace(_throttle_lock=threading.Lock(), model_cooldowns={},
                              model_cooldowns_at={}, cooldown_until=0)
        note = ns['note_error']
        note(acc, 'busy', model='a', cooldown=60)
        clock.time.return_value = 1010
        threads = [threading.Thread(target=note, args=(acc, 'busy'),
                                    kwargs={'model': 'a', 'cooldown': 300}) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(acc.model_cooldowns['a'], 1060)
        self.assertEqual(acc.model_cooldowns_at['a'], 1000)
        note(acc, 'busy', model='b', cooldown=60)
        self.assertEqual(acc.model_cooldowns['b'], 1070)
        clock.time.return_value = 1061
        note(acc, 'busy', model='a', cooldown=120)
        self.assertEqual(acc.model_cooldowns['a'], 1181)
        self.assertEqual(acc.model_cooldowns_at['a'], 1061)

    def test_unscoped_duplicate_does_not_increase_backoff_while_model_cools(self):
        ns = account_code()
        ns['time'] = SimpleNamespace(time=lambda: 1010)
        acc = SimpleNamespace(_throttle_lock=threading.Lock(), model_cooldowns={'a': 1060},
                              unscoped_streak=1, _decay_soft_streak=Mock())
        self.assertEqual(ns['note_unscoped_rate'](acc, 'a'), 50)
        self.assertEqual(acc.unscoped_streak, 1)
        acc._decay_soft_streak.assert_not_called()

    def test_explicit_busy_uses_short_model_window_but_real_rate_keeps_backoff(self):
        ns = account_code()
        ns['time'] = SimpleNamespace(time=lambda: 1000)
        for detail, duration in [('{"msg":"model is busy"}', 15),
                                 ('{"code":14003,"msg":"too many requests"}', 300)]:
            acc = SimpleNamespace(_throttle_lock=threading.Lock(), model_cooldowns={},
                                  model_cooldowns_at={})
            ns['note_error'](acc, 'HTTP 429', model='free', cooldown=300, detail=detail)
            self.assertEqual(acc.model_cooldowns['free'], 1000 + duration)

    def test_model_busy_needs_explicit_model_wording_and_never_overrides_rate_evidence(self):
        classify = account_code().get('model_busy_is_transient', lambda *_: False)
        for detail, expected in [
            ('{"code":6005,"msg":"model is busy, please retry later"}', True),
            ('{"msg":"模型繁忙，请稍后再试"}', True),
            ('{"code":14003,"msg":"model is busy; too many requests"}', False),
            ('{"code":6004,"msg":"model is busy; quota exhausted"}', False),
            ('{"msg":"model busy, usage exceeds frequency limit"}', False),
            ('{"code":6005,"msg":"service unavailable"}', False),
            ('{"msg":"system busy, try again"}', False)]:
            with self.subTest(detail=detail):
                self.assertEqual(classify(detail), expected)


class TestDailyEvidence(unittest.TestCase):
    def test_verified_fallback_never_latches_web_completion_and_remains_retryable(self):
        ns = account_code()
        acc = SimpleNamespace(path='/nonexistent/accounts/test.json', realm='intl', account_banned=False,
            last_daily_chat=None,
            daily_chat_web=lambda **_: {'ok': False, 'phase': 'create',
                'error': 'CERTIFICATE_VERIFY_FAILED'},
            _daily_activity_verified_fallback=lambda: {'ok': True, 'score': 2}, save=Mock())
        result = ns['_daily_chat_web_step'](acc, {'ok': True}, web=True)
        self.assertTrue(result['ok'])
        self.assertIsNone(acc.last_daily_chat)
        self.assertIs(result.get('activityVerified'), True)
        self.assertIs(result.get('webCompleted'), False)
        self.assertIsNone(result.get('rewardVerified'))
        self.assertTrue(ns['can_daily_chat'](acc))
        self.assertIs(acc.daily_chat_state['activityVerified'], True)
        self.assertIs(acc.daily_chat_state['webCompleted'], False)

    def test_completed_web_does_not_invent_reward_or_growth_evidence(self):
        ns = account_code()
        acc = SimpleNamespace(path='', realm='intl', account_banned=False, last_daily_chat=None,
            daily_chat_web=lambda **_: {'ok': True, 'status': 'completed'}, save=Mock())
        result = ns['_daily_chat_web_step'](acc, {'ok': True}, web=True)
        self.assertIs(result.get('webCompleted'), True)
        self.assertIsNone(result.get('activityVerified'))
        self.assertIsNone(result.get('rewardVerified'))
        self.assertFalse(ns['can_daily_chat'](acc, web=True))

    def test_desktop_only_completion_cannot_block_later_web_retry(self):
        ns = account_code()
        acc = SimpleNamespace(path='', realm='intl', account_banned=False, last_daily_chat=None)
        ns['_daily_chat_web_step'](acc, {'ok': True}, web=False)
        self.assertFalse(ns['can_daily_chat'](acc, web=False))
        self.assertTrue(ns['can_daily_chat'](acc, web=True))

    def test_daily_evidence_survives_account_serialization(self):
        tree = ast.parse((ROOT / 'wb_accounts.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Account')
        to_dict = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'to_dict')
        ns = {}
        exec(compile(ast.Module(body=[to_dict], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
        class FakeAccount:
            def __getattr__(self, name):
                return [] if name == 'disabled_models' else {} if name == 'model_caps' else None
        acc = FakeAccount()
        acc.daily_chat_state = {'date': '2026-11-16', 'activityVerified': True,
                                'webCompleted': False, 'rewardVerified': None}
        self.assertEqual(ns['to_dict'](acc).get('dailyChatState'), acc.daily_chat_state)
        init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        assignments = [n for n in init.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == 'daily_chat_state' for t in n.targets)]
        self.assertTrue(assignments, 'state must restore after restart')
        exec(compile(ast.Module(body=assignments, type_ignores=[]), 'wb_accounts.py', 'exec'),
             {'self': acc, 'data': {'dailyChatState': dict(acc.daily_chat_state)}})
        self.assertIs(acc.daily_chat_state['activityVerified'], True)

    def test_explicit_web_retry_overrides_desktop_only_setting(self):
        ns = account_code()
        tree = ast.parse((ROOT / 'wb_accounts.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Account')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'daily_chat')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
        ns['wb_activity'] = SimpleNamespace(record_attempt=Mock(), TASK_DAILY_CHAT='daily')
        acc = SimpleNamespace(realm='intl', path='', account_banned=False,
            can_daily_chat=Mock(side_effect=lambda web=None: web is True),
            _daily_chat_upstream=Mock(return_value={'ok': True}))
        ns['daily_chat'](acc, web=True)
        acc._daily_chat_upstream.assert_called_once_with(True)

    def test_direct_web_success_records_completion_and_banned_never_skips_as_success(self):
        ns = account_code()
        ns['wb_activity'] = SimpleNamespace(record_attempt=Mock(), TASK_DAILY_CHAT='daily')
        acc = SimpleNamespace(realm='intl', account_banned=False, path='', last_daily_chat=None,
            daily_chat_state={}, can_daily_chat=lambda **_: True,
            _daily_chat_web_upstream=Mock(return_value={'ok': True, 'status': 'completed'}))
        result = ns['daily_chat_web'](acc)
        self.assertIs(result.get('webCompleted'), True)
        self.assertIsNone(result.get('rewardVerified'))
        self.assertTrue(acc.daily_chat_state['webCompleted'])
        acc.account_banned = True
        acc.can_daily_chat = lambda **_: False
        result = ns['daily_chat_web'](acc)
        self.assertFalse(result['ok'])
        self.assertTrue(result['restricted'])


class TestWebModelSafety(unittest.TestCase):
    def test_confirmed_model_keeps_permission_and_tool_events_intact(self):
        import importlib.util
        import queue
        spec = importlib.util.spec_from_file_location('offline_webagent', ROOT / 'wb_webagent.py')
        webagent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(webagent)
        permission = {'id': 'permission-7', 'method': 'session/request_permission',
                      'params': {'options': [{'optionId': 'allow-once', 'kind': 'allow_once'}]}}
        tool = {'method': 'session/update', 'params': {'update': {'sessionUpdate': 'tool_call',
                                                               'toolCallId': 'tool-1'}}}
        class Channel:
            updates = chunks = 0
            def __init__(self, *args, **kwargs):
                self.events = queue.Queue()
                self.requests = []
            def open(self):
                pass
            def request(self, method, params, request_id):
                self.requests.append((method, params))
                if method == 'session/set_model':
                    for event in (permission, tool, {'id': request_id, 'result': {}}):
                        self.events.put(event)
            def close(self):
                pass
        channel = Channel()
        result = webagent.run_turn('https://offline.invalid', 'dummy', 'sid', '/', 'Hi',
            'test', poll_status=lambda: 'completed', poll_interval=0,
            channel_factory=lambda *a, **k: channel, model='free')
        self.assertTrue(result['ok'])
        self.assertEqual(channel.events.get_nowait(), permission)
        self.assertEqual(channel.events.get_nowait(), tool)
        self.assertEqual([m for m, _ in channel.requests],
                         ['initialize', 'session/load', 'session/set_model', 'session/prompt'])
        self.assertEqual(channel.requests[1][1]['mcpServers'], [])

    def test_model_failure_stops_before_prompt_even_if_poll_says_completed(self):
        import importlib.util
        import queue
        spec = importlib.util.spec_from_file_location('offline_webagent', ROOT / 'wb_webagent.py')
        webagent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(webagent)
        for failure in ['http', 'rpc']:
            calls = []
            class Channel:
                updates = chunks = 0
                events = queue.Queue()
                def __init__(self, *args, **kwargs):
                    self.closed = False
                def open(self):
                    pass
                def request(self, method, params, request_id):
                    calls.append(method)
                    if method == 'session/set_model':
                        if failure == 'http':
                            raise webagent.AcpError('set_model HTTP 500')
                        self.events.put({'id': request_id, 'error': {'message': 'model denied'}})
                def close(self):
                    self.closed = True
            result = webagent.run_turn('https://offline.invalid', 'dummy', 'sid', '/', 'Hi',
                'test', poll_status=lambda: 'completed', poll_interval=0,
                channel_factory=Channel, model='free')
            self.assertFalse(result['ok'])
            self.assertIn('模型', result['error'])
            self.assertNotIn('session/prompt', calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
