"""Offline regression tests: extract actual methods, never load credentials/network."""
import ast
import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(os.environ.get('WB_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'wb_accounts.py')))


def methods():
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Account')
    chosen = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in
              ('_daily_chat_web_step', '_daily_activity_verified_fallback')]
    ns = {'time': time, 'os': os, 'json': json,
          'wb_settings': SimpleNamespace(daily_chat_web_fallback_activity=lambda _: True),
          'is_tls_verify_text': lambda text: 'CERTIFICATE_VERIFY_FAILED' in str(text)}
    import urllib.request
    ns.update({'urllib': __import__('urllib'), 'urlopen': Mock(), 'WEB_ORIGIN': 'https://www.workbuddy.ai'})
    for node in chosen:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class TestDailyFallback(unittest.TestCase):
    def test_missing_web_key_uses_verified_activity_not_fake_web_success(self):
        ns = methods()
        acc = SimpleNamespace(path='/nonexistent/accounts/test.json',
            daily_chat_web=lambda **_: {'ok': False, 'phase': 'create',
                'error': 'HTTP 401: {"code":10034,"msg":"CreateConversation: 10034:CODEBUDDY_API_KEY is required but could not be resolved"}'},
            _daily_activity_verified_fallback=Mock(return_value={'ok': True, 'score': 2, 'already_active': True}),
            save=Mock())
        result = ns['_daily_chat_web_step'](acc, {'ok': True}, web=True)
        self.assertTrue(result['ok'])
        acc._daily_activity_verified_fallback.assert_called_once()
        self.assertIn('连登', result['msg'])
        self.assertIn('未完成', result['msg'])
        self.assertNotIn('积分到账', result['msg'])

    def test_verified_fallback_skips_event_if_heatmap_already_lit(self):
        ns = methods()
        self.assertIn('_daily_activity_verified_fallback', ns)
        response = SimpleNamespace(read=lambda: json.dumps({'code': 0, 'data': {
            'cells': [{'date': time.strftime('%Y-%m-%d'), 'score': 2}]}}).encode())
        ns['urlopen'].return_value = contextlib.nullcontext(response)
        acc = SimpleNamespace(web_headers=lambda: {}, proxy=None, report_activity=Mock())
        result = ns['_daily_activity_verified_fallback'](acc)
        self.assertTrue(result['ok'])
        self.assertTrue(result['already_active'])
        acc.report_activity.assert_not_called()

    def test_heatmap_failure_does_not_send_event(self):
        ns = methods()
        ns['urlopen'].side_effect = OSError('offline')
        acc = SimpleNamespace(web_headers=lambda: {}, proxy=None, report_activity=Mock())
        result = ns['_daily_activity_verified_fallback'](acc)
        self.assertFalse(result['ok'])
        acc.report_activity.assert_not_called()

    def test_report_is_only_success_after_readback(self):
        for after, expected in [(2, True), (0, False)]:
            ns = methods()
            def response(score):
                payload = {'code': 0, 'data': {'cells': [
                    {'date': time.strftime('%Y-%m-%d'), 'score': score}]}}
                return contextlib.nullcontext(SimpleNamespace(read=lambda: json.dumps(payload).encode()))
            ns['urlopen'].side_effect = [response(0), response(after)]
            acc = SimpleNamespace(web_headers=lambda: {}, proxy=None,
                                  report_activity=Mock(return_value={'ok': True}))
            result = ns['_daily_activity_verified_fallback'](acc)
            self.assertEqual(result['ok'], expected)
            acc.report_activity.assert_called_once()

    def test_unrelated_401_never_falls_back(self):
        ns = methods()
        acc = SimpleNamespace(path='/nonexistent/accounts/test.json',
            daily_chat_web=lambda **_: {'ok': False, 'phase': 'create', 'error': 'HTTP 401 token expired'},
            _daily_activity_verified_fallback=Mock(), save=Mock())
        result = ns['_daily_chat_web_step'](acc, {'ok': True}, web=True)
        self.assertFalse(result['ok'])
        acc._daily_activity_verified_fallback.assert_not_called()

    def test_fallback_disabled_never_falls_back(self):
        ns = methods()
        ns['wb_settings'].daily_chat_web_fallback_activity = lambda _: False
        acc = SimpleNamespace(path='/nonexistent/accounts/test.json',
            daily_chat_web=lambda **_: {'ok': False, 'phase': 'create',
                'error': 'HTTP 401 code10034 CODEBUDDY_API_KEY is required but could not be resolved'},
            _daily_activity_verified_fallback=Mock(), save=Mock())
        result = ns['_daily_chat_web_step'](acc, {'ok': True}, web=True)
        self.assertFalse(result['ok'])
        acc._daily_activity_verified_fallback.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
