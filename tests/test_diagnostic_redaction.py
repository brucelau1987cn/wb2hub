"""Offline regressions using real gateway logging functions, never app startup.

Every credential in this file is an explicit synthetic placeholder, not a secret.
"""
import ast
import io
import json
import queue
import re
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def gateway_code():
    tree = ast.parse((ROOT / 'wb_proxy.py').read_text())
    ns: dict[str, Any] = dict(re=re, time=time, queue=queue, sys=SimpleNamespace(stderr=io.StringIO()),
              threading=threading, LOG_BUFFER=deque(), _LOG_COUNTER=0,
              _LOG_LOCK=threading.Lock(), LOG_STORE=None,
              _LOG_QUEUE=queue.Queue(), _LOG_SHUTTING_DOWN=False,
              _LOG_STREAM_CLOSED=False, _LOG_DROPPED=0,
              _ensure_log_writer=lambda: None)
    names = {'_mask_text', '_mask_phone', '_redact_account_names', '_log_db_active',
             'add_log_entry', 'log', '_log_writer_loop', 'get_logs', 'record_error',
             'exc_detail'}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                (t.id.startswith('_PHONE') or t.id in {'_LOG_ERROR_HINTS', '_LOG_WARN_HINTS',
                    '_LOG_TAG_HINTS', '_IDENTITY_KEYS'}) for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_proxy.py', 'exec'), ns)
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_proxy.py', 'exec'), ns)
        if isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name in {'_get_logs_export', 'log_message'}:
                    exec(compile(ast.Module(body=[method], type_ignores=[]), 'wb_proxy.py', 'exec'), ns)
    return ns


class TestDiagnosticLogging(unittest.TestCase):
    def test_real_console_buffer_and_db_redact_jwt_before_unicode_boundary(self):
        ns = gateway_code()
        store = SimpleNamespace(enabled=True, append=Mock())
        ns['LOG_STORE'] = store
        jwt = 'eyJ' + 'FAKE' * 10 + '.' + 'PLACEHOLDER' * 8 + '.' + 'NOTASECRET' * 8
        prefix = 'HTTP 401 code=11140 失败 ' + '界' * 4000
        source = prefix + jwt
        ns['log'](source, level='ERROR', tag='chat')
        ns['_LOG_QUEUE'].put(None)
        ns['_log_writer_loop']()
        entry = ns['LOG_BUFFER'][-1]
        expected = prefix + '<redacted>'
        self.assertEqual(entry['msg'], expected)
        self.assertEqual(store.append.call_args.args[0]['msg'], expected)
        self.assertIn(expected, ns['sys'].stderr.getvalue())
        self.assertNotIn('eyJFAKE', ns['sys'].stderr.getvalue())
        self.assertEqual(source, prefix + jwt)

    def test_console_redacts_quoted_and_camelcase_credentials_without_eating_codes(self):
        ns = gateway_code()
        text = ('HTTP 429 code=11140 model=deepseek-v4.1-flash request_id=req-123 '
                'Bearer FAKE-bearer; api_key=FAKE-api; '
                'accessToken="FAKE access token"; password=\'FAKE password\'; '
                'refresh_token=FAKE-refresh&clientSecret=FAKE-client; '
                'Authorization: Basic FAKE-basic; cookie="FAKE-cookie"; '
                'sk-FAKEplaceholdernotasecret')
        ns['log'](text)
        output = ns['LOG_BUFFER'][-1]['msg']
        self.assertNotIn('FAKE', output)
        for allowed in ('HTTP 429', 'code=11140', 'model=deepseek-v4.1-flash', 'request_id=req-123'):
            self.assertIn(allowed, output)
        self.assertIn('<redacted>', output)

    def test_usage_error_persists_full_redacted_reason_and_preserves_metadata(self):
        ns = gateway_code()
        captured = []
        ns.update(_extract_usage=lambda _: {}, _persist_usage=lambda row, _: captured.append(row), POOL=None)
        jwt = 'eyJ' + 'FAKE' * 30 + '.' + 'NOTASECRET' * 12 + '.' + 'PLACEHOLDER' * 10
        source = 'HTTP 403 code=11140 账号受限 ' + '界' * 155 + jwt
        row = ns['record_error']('deepseek-v4.1-flash', 403, source, key='key-id', realm='cn')
        self.assertNotIn('eyJFAKE', json.dumps(captured))
        self.assertIn('<redacted>', row['message'])
        self.assertIn('code=11140', row['message'])
        self.assertEqual(row['status'], 403)
        self.assertEqual(row['model'], 'deepseek-v4.1-flash')
        self.assertEqual(row['key'], 'key-id')
        self.assertEqual(row['realm'], 'cn')
        self.assertIn('<redacted>', ns['LOG_BUFFER'][-1]['msg'])

    def test_legacy_db_panel_read_and_plaintext_export_are_safe_copies(self):
        ns = gateway_code()
        legacy = {'id': 1, 'ts': '2026-01-01', 'time': '01:01:01', 'level': 'ERROR',
                  'tag': 'chat', 'msg': 'HTTP 401 api_key=FAKE-legacy',
                  'headers': {'Authorization': 'FAKE-header'}, 'body': 'FAKE-body'}
        result = {'total': 1, 'max_id': 1, 'logs': [legacy]}
        ns['LOG_STORE'] = SimpleNamespace(enabled=True, query=lambda **_: result)
        safe = ns['get_logs']()
        self.assertNotIn('FAKE', json.dumps(safe))
        self.assertEqual(result['logs'][0]['msg'], 'HTTP 401 api_key=FAKE-legacy')
        self.assertEqual(safe['total'], 1)
        fake = SimpleNamespace(_authorized=lambda: True, wfile=io.BytesIO(), path='/logs/export',
                               send_response=Mock(), send_header=Mock(), end_headers=Mock())
        ns['cors_origin_allowed'] = lambda _: False
        ns['_get_logs_export'](fake)
        self.assertNotIn(b'FAKE', fake.wfile.getvalue())
        self.assertIn(b'HTTP 401', fake.wfile.getvalue())

    def test_error_json_excludes_payload_and_auth_headers_but_keeps_reason(self):
        ns = gateway_code()
        payload = {'code': 11140, 'msg': '账号受限 apiKey=FAKE-json-key', 'requestId': 'req-123',
                   'prompt': 'FAKE-prompt', 'body': {'content': 'FAKE-body'},
                   'headers': {'Authorization': 'Bearer FAKE-header'}, 'data': {'echo': 'FAKE-echo'}}
        ns['log'](json.dumps(payload, ensure_ascii=False))
        output = ns['LOG_BUFFER'][-1]['msg']
        self.assertNotIn('FAKE', output)
        for allowed in ('11140', '账号受限', 'req-123'):
            self.assertIn(allowed, output)
        self.assertIn('FAKE-prompt', payload['prompt'])

    def test_account_error_setter_and_notes_redact_before_old_boundary(self):
        tree = ast.parse((ROOT / 'wb_accounts.py').read_text())
        account = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Account')
        ns: dict[str, Any] = dict(time=time, _auth_failure_text=lambda _: False)
        for node in account.body:
            if isinstance(node, ast.FunctionDef) and node.name in {'_set_last_error', 'note_error'}:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_accounts.py', 'exec'), ns)
        source = 'HTTP 401 code=11140 ' + '界' * 150 + ' accessToken=FAKE-placeholder' * 100
        acc = SimpleNamespace(_throttle_lock=threading.Lock(), cooldown_until=0)
        ns['_set_last_error'](acc, source)
        self.assertNotIn('FAKE', acc.last_error)
        ns['note_error'](acc, source, detail=source)
        self.assertNotIn('FAKE', acc.last_error)
        self.assertNotIn('FAKE', acc.last_error_detail)
        self.assertIn('11140', acc.last_error)
        self.assertIn('<redacted>', acc.last_error)
        self.assertIn('FAKE', source)

    def test_exception_detail_redacts_before_small_limit(self):
        ns = gateway_code()
        exc = RuntimeError('HTTP 401 ' + '界' * 100 + ' password=FAKE-placeholder' * 50)
        output = ns['exc_detail'](exc, limit=180)
        self.assertNotIn('FAKE', output)
        self.assertIn('RuntimeError', output)
        self.assertLessEqual(len(output), 180)

    def test_redaction_is_idempotent_with_structured_error_reason(self):
        from wb_diagnostics import diagnostic_detail
        source = json.dumps({'code': 11140, 'msg': '失败 token=FAKE-test', 'requestId': 'req-123'}, ensure_ascii=False)
        first = diagnostic_detail(source)
        self.assertEqual(diagnostic_detail(first), first)
        self.assertIn('req-123', first)
        self.assertNotIn('FAKE', first)

    def test_access_log_drops_query_prompt_and_unlabelled_values(self):
        ns = gateway_code()
        handler = SimpleNamespace(command='POST', path='/v1/chat/completions?prompt=FAKE-prompt&anything=FAKE-value')
        ns['log_message'](handler, '"%s" %s %s', 'POST ' + handler.path + ' HTTP/1.1', '401', '-')
        output = ns['LOG_BUFFER'][-1]['msg']
        self.assertNotIn('FAKE', output)
        self.assertIn('/v1/chat/completions', output)
        self.assertIn('401', output)

    def test_field_secret_crossing_4096_boundary_is_removed_in_full(self):
        from wb_diagnostics import diagnostic_detail
        for field in ('api_key', 'password', 'accessToken', 'Authorization'):
            source = '界' * 4070 + ' ' + field + '=FAKE' + 'x' * 500
            output = diagnostic_detail(source)
            self.assertNotIn('FAKE', output)
            self.assertNotIn('xxxx', output)
            self.assertLessEqual(len(output), 4096)

    def test_activity_writer_uses_shared_redaction_before_its_short_limit(self):
        import wb_activity
        source = 'HTTP 401 ' + '界' * 150 + ' accessToken="FAKE value with spaces"'
        output = wb_activity.safe_message(source)
        self.assertNotIn('FAKE', output)
        self.assertNotIn('with spaces', output)
        self.assertIn('HTTP 401', output)
        self.assertLessEqual(len(output), wb_activity.MAX_MESSAGE)

    def test_headers_with_spaces_and_multiple_cookies_do_not_leak_suffixes(self):
        from wb_diagnostics import diagnostic_detail
        for text in ('HTTP 401 Authorization: Digest username="FAKE-user", response="FAKE-response"',
                     'HTTP 403 Cookie: session=FAKE-session; other=FAKE-other',
                     'HTTP 401 Set-Cookie: sid=FAKE-session; Path=/; other=FAKE-other',
                     'HTTP 401 password="FAKE escaped \\" quote and tail"',
                     'HTTP 401 bearer\tFAKE-token'):
            with self.subTest(text=text):
                self.assertNotIn('FAKE', diagnostic_detail(text))
                self.assertIn('HTTP', diagnostic_detail(text))

    def test_scheduler_private_ring_is_redacted_not_just_gateway_copy(self):
        tree = ast.parse((ROOT / 'wb_scheduler.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(
            isinstance(m, ast.FunctionDef) and m.name == 'log' for m in n.body))
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'log')
        forwarded = []
        ns: dict[str, Any] = dict(time=time, _proxy_module=lambda: SimpleNamespace(
            add_log_entry=lambda msg, **_: forwarded.append(msg)))
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'wb_scheduler.py', 'exec'), ns)
        obj = SimpleNamespace(logs=[], _log_lock=threading.Lock())
        ns['log'](obj, '签到失败 HTTP 401 api_key=FAKE-placeholder')
        self.assertNotIn('FAKE', ''.join(obj.logs))
        self.assertNotIn('FAKE', ''.join(forwarded))
        self.assertIn('HTTP 401', obj.logs[0])

    def test_real_sqlite_write_receives_sanitized_diagnostic(self):
        import tempfile
        from wb_logstore import LogStore
        ns = gateway_code()
        with tempfile.TemporaryDirectory() as directory:
            store = LogStore(str(Path(directory) / 'logs.sqlite3'))
            store.configure(enabled=True)
            ns['LOG_STORE'] = store
            try:
                ns['log']('HTTP 403 code=11140 password="FAKE password"')
                result: Any = store.query()
                self.assertEqual(result['total'], 1)
                self.assertNotIn('FAKE', json.dumps(result))
                self.assertIn('11140', result['logs'][0]['msg'])
            finally:
                store.close()

    def test_structured_authorization_reason_keeps_request_id_across_repeated_sinks(self):
        from wb_diagnostics import diagnostic_detail
        raw = json.dumps({'code': 401, 'msg': 'Authorization: Digest FAKE-secret', 'requestId': 'req-safe'})
        once = diagnostic_detail(raw)
        self.assertNotIn('FAKE', once)
        self.assertIn('req-safe', once)
        self.assertEqual(diagnostic_detail(once), once)

    def test_explicit_payload_assignments_and_structured_header_fields_are_omitted(self):
        from wb_diagnostics import diagnostic_detail
        for payload in ('HTTP 400 model=gpt-4.1 prompt=FAKE-prompt with spaces',
                        'HTTP 400 body={"content":"FAKE-body"}',
                        'HTTP 400 headers={"Authorization":"FAKE-header"}',
                        json.dumps({'code': 400, 'message': '失败', 'requestId': 'req-ok',
                                    'authHeaders': {'Authorization': 'FAKE-header'}})):
            with self.subTest(payload=payload):
                result = diagnostic_detail(payload)
                self.assertNotIn('FAKE', result)
                self.assertIn('400', result)

    def test_phone_privacy_masking_precedes_unicode_truncation(self):
        ns = gateway_code()
        prefix = '界' * 4090 + ' '
        ns['log'](prefix + '13812345678')
        output = ns['LOG_BUFFER'][-1]['msg']
        self.assertEqual(output, (prefix + '138****5678')[:4095] + '…')

    def test_json_model_and_request_status_metadata_are_not_secrets(self):
        from wb_diagnostics import diagnostic_detail
        payload = {'model': 'deepseek-v4.1-flash', 'status': 'requested', 'code': 11128,
                   'requestId': 'req-123', 'method': 'POST', 'endpoint': '/v1/chat/completions'}
        self.assertEqual(json.loads(diagnostic_detail(json.dumps(payload))), payload)

    def test_unicode_limit_and_innocuous_metadata(self):
        from wb_diagnostics import diagnostic_detail
        for size in (4095, 4096, 4097, 5000):
            with self.subTest(size=size):
                text = '界' * size
                expected = text if size <= 4096 else '界' * 4095 + '…'
                self.assertEqual(diagnostic_detail(text), expected)
                ns = gateway_code()
                self.assertEqual(ns['add_log_entry'](text)['msg'], expected)
        ordinary = ('HTTP 200 code=11128 status=requested key=pool token_count=0 '
                    'prompt_tokens=123 model=gpt-4.1 request_id=abcdefghijklmnopqrstuv1234567890 '
                    'model=deepseek-v4.1-flash ' + 'abcdef0123456789' * 4)
        self.assertEqual(diagnostic_detail(ordinary), ordinary)
        self.assertEqual(diagnostic_detail(None), '')
        self.assertEqual(diagnostic_detail('失败', limit=1), '…')
        self.assertEqual(diagnostic_detail('失败', limit=0), '')


if __name__ == '__main__':
    unittest.main()
