"""Offline scheduler regression: never start threads or load real accounts."""
import ast
import pathlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'wb_scheduler.py'

def load_methods():
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Scheduler')
    ns = {'time': time, '_proxy_module': lambda: SimpleNamespace(restriction_hold_remaining=lambda _: 0)}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CHECKIN_RETRY_DELAYS' for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name in ('_queue_checkin_retry', '_run_checkin_retries'):
            exec(compile(ast.Module(body=[n], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns

class RetryTest(unittest.TestCase):
    def scheduler(self, ns, account):
        obj = SimpleNamespace(pool=SimpleNamespace(accounts=[account]), enabled=True,
                              _stop_event=threading.Event(), _run_lock=threading.Lock(),
                              _checkin_retries={}, log=Mock())
        for k in ('_queue_checkin_retry', '_run_checkin_retries'):
            if k in ns:
                setattr(obj, k, lambda *a, _f=ns[k], **kw: _f(obj, *a, **kw))
        return obj

    def test_pending_retry_deduplicates_without_moving_due_time(self):
        ns = load_methods()
        self.assertIn('_queue_checkin_retry', ns)
        a = SimpleNamespace(uid='test-account', realm='cn')
        s = self.scheduler(ns, a)
        self.assertTrue(s._queue_checkin_retry(a, {'ok': False, 'state': 'pending'}))
        first = dict(s._checkin_retries)
        s._queue_checkin_retry(a, {'ok': False, 'state': 'pending'})
        self.assertEqual(first, s._checkin_retries)
        self.assertEqual(len(first), 1)

    def test_rate_limit_and_restriction_not_queued(self):
        ns = load_methods()
        a = SimpleNamespace(uid='test-account', realm='cn')
        s = self.scheduler(ns, a)
        for result in ({'ok': False, 'state': 'failed', 'code': 429},
                       {'ok': False, 'state': 'pending', 'restricted': True}):
            self.assertFalse(s._queue_checkin_retry(a, result))
        self.assertFalse(s._checkin_retries)

    def ready(self, ns):
        a = SimpleNamespace(uid='test-account', realm='cn', account_banned=False,
                            task_active=lambda: True, can_checkin=lambda: True,
                            checkin=Mock(return_value={'ok': False, 'state': 'pending'}))
        s = self.scheduler(ns, a)
        s._queue_checkin_retry(a, {'ok': False, 'state': 'pending'})
        key = next(iter(s._checkin_retries))
        s._checkin_retries[key]['due'] = 0
        return s, a, key

    def test_retry_bound_and_no_immediate_duplicate(self):
        ns = load_methods()
        s, a, key = self.ready(ns)
        s._run_checkin_retries()
        a.checkin.assert_called_once_with(trigger='scheduler')
        s._run_checkin_retries()
        self.assertEqual(a.checkin.call_count, 1)
        s._checkin_retries[key]['due'] = 0
        s._run_checkin_retries()
        self.assertEqual(a.checkin.call_count, 2)
        s._run_checkin_retries()
        self.assertEqual(a.checkin.call_count, 2)
        self.assertFalse(s._queue_checkin_retry(a, {'ok': False, 'state': 'pending'}))

    def test_disabled_paused_and_banned_never_attempt(self):
        for mode in ('disabled', 'realm_hold', 'banned', 'completed', 'cycle_busy'):
            ns = load_methods()
            s, a, key = self.ready(ns)
            if mode == 'disabled': s.enabled = False
            if mode == 'realm_hold': ns['_proxy_module'] = lambda: SimpleNamespace(restriction_hold_remaining=lambda _: 600)
            if mode == 'banned': a.account_banned = True
            if mode == 'completed': a.can_checkin = lambda: False
            if mode == 'cycle_busy': s._run_lock.acquire()
            s._run_checkin_retries()
            a.checkin.assert_not_called()

    def test_exception_releases_lock_without_retry_storm(self):
        ns = load_methods()
        s, a, key = self.ready(ns)
        a.checkin.side_effect = RuntimeError('network interrupted')
        s._run_checkin_retries()
        self.assertFalse(s._run_lock.locked())
        self.assertTrue(s._checkin_retries[key]['finished'])
        s._run_checkin_retries()
        self.assertEqual(a.checkin.call_count, 1)

    def test_governance_failure_defers_without_sending(self):
        ns = load_methods()
        s, a, key = self.ready(ns)
        ns['_proxy_module'] = Mock(side_effect=RuntimeError('governance unavailable'))
        s._run_checkin_retries()
        a.checkin.assert_not_called()
        self.assertFalse(s._checkin_retries[key]['finished'])

    def test_previous_day_retry_is_discarded(self):
        ns = load_methods()
        s, a, key = self.ready(ns)
        s._checkin_retries[('2000-01-01', a.uid)] = s._checkin_retries.pop(key)
        s._run_checkin_retries()
        a.checkin.assert_not_called()
        self.assertFalse(s._checkin_retries)

if __name__ == '__main__':
    unittest.main()
