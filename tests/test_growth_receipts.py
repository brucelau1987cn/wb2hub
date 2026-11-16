"""Offline tests execute real AST-extracted functions; never import the runtime."""
import ast
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def load_tasks():
    tree = ast.parse((ROOT / 'wb_tasks.py').read_text())
    ns = dict(json=json, re=__import__('re'), urllib=__import__('urllib'),
              time=SimpleNamespace(sleep=lambda _: None, time=lambda: 1,
                  localtime=lambda: SimpleNamespace(tm_hour=1)),
              _accounts=SimpleNamespace(urlopen=Mock()), _log=lambda _: None,
              CHAT_BASE='https://chat.invalid', WEB_BASE='https://web.invalid',
              WEB_UA='offline', BILL_BASE='https://bill.invalid')
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {
                'TASK_SPECS', 'MP_TASK_SPECS', 'MP_HEADERS', 'MP_REPORT_HEADERS',
                'DESKTOP_ONLY_TASKS', 'NIGHT_TASK_CODES', 'EXPERT_ID_POOL', 'TEAM_ID_POOL',
                'LIB_DOC_URL', '_EXPERT_ID_RE'} for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_tasks.py', 'exec'), ns)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_tasks.py', 'exec'), ns)
    return ns


def account(**kwargs):
    fields = dict(realm='cn', uid='offline', nickname='test', access_token='NOT-A-TOKEN',
                  proxy=None, headers=lambda _: {}, is_enterprise=lambda: False,
                  credits={'remain': 0}, fetch_credits=Mock())
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def task(code='Library_read', status='not_accepted', **kwargs):
    return dict(task_code=code, name=code, status=status, current=0, target=1,
                task_type='single', **kwargs)


def response(payload):
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.read.return_value = json.dumps(payload).encode()
    return value


def receipt(*codes):
    return response({'code': 0, 'data': {'results': [
        {'task_code': c, 'status': 'accepted'} for c in codes]}})


class FinalTerminalBoundaryTests(unittest.TestCase):
    def test_long_http_business_body_stops_next_chunk(self):
        for length in (600, 5000, 70000):
            ns = load_tasks()
            body = json.dumps({'code': 11140, 'msg': 'x' * length}).encode()
            ns['_accounts'].urlopen.side_effect = [urllib.error.HTTPError(
                'https://offline.invalid', 400, 'business restriction', {}, io.BytesIO(body)), receipt('b')]
            result = ns['accept_tasks'](account(), ['a', 'b'], chunk=1)
            self.assertTrue(result['terminal'])
            self.assertEqual(ns['_accounts'].urlopen.call_count, 1)

    def test_manual_claim_blocks_partial_terminal_discovery(self):
        import sys
        from unittest.mock import patch
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = [urllib.error.HTTPError(
            'https://offline.invalid', 429, 'limited', {}, io.BytesIO(b'{}')),
            response({'code': 0, 'data': {'tasks': [
                {'task_code': 'Library_read', 'accept_status': 'completed'}]}})]
        claim = Mock(return_value={'ok': True, 'reward_verified': True, 'credit': 7})
        module = SimpleNamespace(discover_growth_tasks=ns['discover_growth_tasks'], claim_task=claim)
        route = proxy_code(dict(POOL=SimpleNamespace(get=lambda _: account()),
            restriction_hold_remaining=lambda _: 0, invalidate_tasks_cache=Mock()))
        with patch.dict(sys.modules, {'wb_tasks': module}):
            result = route['_route_tasks_claim'](SimpleNamespace(_json_panel=lambda status, data: data),
                {'uid': 'offline', 'code': 'Library_read'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['terminal'])
        claim.assert_not_called()

    def test_night_terminal_report_stops_progress_and_claim(self):
        ns = load_tasks()
        failure = ns['_growth_failure']('rate limit', code=429)
        ns.update(fetch_growth_tasks=Mock(return_value=[task('black_cat', 'accepted')]),
            report_events=Mock(return_value=ns['_GrowthProgressFailure'](failure)),
            build_event=Mock(return_value={}), claim_task=Mock())
        result = ns['run_night_growth'](account())
        self.assertFalse(result['ok'])
        self.assertTrue(result['terminal'])
        self.assertEqual(ns['fetch_growth_tasks'].call_count, 1)
        ns['claim_task'].assert_not_called()


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_tasks()
        self.net = self.ns['_accounts'].urlopen

    def test_receipt_accepted_but_readback_unchanged_is_not_success(self):
        self.net.return_value = receipt('Library_read')
        self.ns['fetch_growth_tasks'] = Mock(return_value=[task()])
        result = self.ns['accept_tasks'](account(), ['Library_read'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['accepted'], [])
        self.assertEqual(result['failed'], ['Library_read'])

    def test_unchanged_readback_uses_web_once_then_verifies_original_origin(self):
        self.net.return_value = receipt('Library_read')
        fetch = self.ns['fetch_growth_tasks'] = Mock(side_effect=[
            [task()], [task(status='accepted')]])
        result = self.ns['accept_tasks'](account(), ['Library_read'], mp=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['accepted'], ['Library_read'])
        self.assertEqual(self.net.call_count, 2)
        web = self.net.call_args_list[1].args[0]
        self.assertEqual(web.full_url, 'https://web.invalid/activity/growth/tasks/accept')
        self.assertEqual(web.get_header('X-client-platform'), 'web')
        self.assertTrue(all(call.kwargs.get('mp') for call in fetch.call_args_list))

    def test_web_fallback_is_bounded_even_if_still_unchanged(self):
        self.net.return_value = receipt('Library_read')
        self.ns['fetch_growth_tasks'] = Mock(return_value=[task()])
        result = self.ns['accept_tasks'](account(), ['Library_read', 'Library_read'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed'], ['Library_read'])
        self.assertEqual(self.net.call_count, 2)

    def test_terminal_http_never_falls_back(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.net.reset_mock()
                self.net.side_effect = urllib.error.HTTPError('https://offline.invalid', status,
                    'terminal', {}, io.BytesIO(b'{}'))
                result = self.ns['accept_tasks'](account(), ['Library_read'])
                self.assertFalse(result['ok'])
                self.assertEqual(self.net.call_count, 1)

    def test_read_error_does_not_authorize_web_write(self):
        self.net.return_value = receipt('Library_read')
        self.ns['fetch_growth_tasks'] = Mock(side_effect=RuntimeError('list failed'))
        result = self.ns['accept_tasks'](account(), ['Library_read'])
        self.assertFalse(result['ok'])
        self.assertEqual(self.net.call_count, 1)


class TerminalReadbackTests(unittest.TestCase):
    def test_terminal_readback_or_web_failure_stops_accept_chunks(self):
        for stage in ('readback', 'web', 'web_readback'):
            for kind in ('http', 'business'):
                for status in (401, 403, 429, 11140, 14003, 6004, 6005, 10001):
                    if kind == 'http' and status > 599:
                        continue
                    with self.subTest(stage=stage, kind=kind, status=status):
                        ns = load_tasks()
                        net = ns['_accounts'].urlopen
                        failure = (urllib.error.HTTPError('https://offline.invalid', status,
                            'terminal', {}, io.BytesIO(b'{}')) if kind == 'http' else
                            response({'code': status, 'msg': 'terminal diagnostic'}))
                        unchanged = response({'code': 0, 'data': {'tasks': [
                            {'task_code': 'a', 'accept_status': 'not_accepted'}]}})
                        sequence = [receipt('a')]
                        if stage != 'readback':
                            sequence.append(unchanged)
                        if stage == 'web_readback':
                            sequence.append(receipt('a'))
                        sequence.append(failure)
                        net.side_effect = sequence + [receipt('b')] * 5
                        result = ns['accept_tasks'](account(), ['a', 'b'], chunk=1)
                        self.assertEqual(net.call_count, len(sequence), 'wrote after terminal failure')
                        self.assertTrue(result.get('terminal'), result)
                        self.assertFalse(result['ok'])
                        self.assertEqual(result['failed'], ['a', 'b'])
                        self.assertIn(str(status), str(result['reasons']))

    def test_http_400_with_terminal_business_body_never_falls_back_or_continues(self):
        for entry in ('accept_tasks', 'run_growth_tasks', 'run_miniprogram_tasks'):
            ns = load_tasks()
            failure = urllib.error.HTTPError('https://offline.invalid', 400, 'business restriction', {},
                io.BytesIO(b'{"code":11140,"msg":"account restriction"}'))
            net = ns['_accounts'].urlopen
            net.side_effect = [failure] + [receipt('a')] * 10
            if entry == 'accept_tasks':
                result = ns[entry](account(), ['a', 'b'], chunk=1)
            else:
                rows = [task('Library_read' if entry == 'run_growth_tasks' else 'Sequential_Tasks_1',
                             'completed'), task('skill_1', 'completed')]
                ns['discover_growth_tasks'] = Mock(return_value={'ok': True, 'tasks': rows, 'errors': []})
                result = ns[entry](account(), gap=0,
                    **({'tasks': rows} if entry == 'run_miniprogram_tasks' else {}))
            with self.subTest(entry=entry):
                self.assertEqual(net.call_count, 1)
                self.assertTrue(result.get('terminal'), result)

    def test_http_status_stays_terminal_even_with_nonterminal_body(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = urllib.error.HTTPError(
            'https://offline.invalid', 429, 'terminal', {}, io.BytesIO(b'{"code":400}'))
        with self.assertRaises(RuntimeError) as raised:
            ns['fetch_growth_tasks'](account())
        self.assertTrue(getattr(raised.exception, 'terminal', False))

    def test_web_http_business_restriction_preserves_body_diagnostic(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(return_value=[task('a')])
        ns['_accounts'].urlopen.side_effect = [receipt('a'), urllib.error.HTTPError(
            'https://offline.invalid', 400, 'business restriction', {},
            io.BytesIO(b'{"code":11140,"msg":"account restriction detail"}'))]
        result = ns['accept_tasks'](account(), ['a', 'b'], chunk=1)
        self.assertTrue(result.get('terminal'), result)
        self.assertIn('account restriction detail', str(result['reasons']))
        self.assertEqual(ns['_accounts'].urlopen.call_count, 2)

    def test_web_business_rejection_diagnostic_is_not_discarded(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = [receipt('a'),
            response({'code': 400, 'msg': 'web rejection diagnostic'})]
        ns['fetch_growth_tasks'] = Mock(return_value=[task('a')])
        result = ns['accept_tasks'](account(), ['a'])
        self.assertIn('web rejection diagnostic', str(result['reasons']))
        self.assertFalse(result.get('terminal', False))


class DiscoveryTests(unittest.TestCase):
    def test_failed_or_malformed_list_is_not_an_empty_list(self):
        for payload in ({'code': 401, 'data': {'tasks': []}}, {},
                        {'code': 0, 'data': {}}, {'code': 0, 'data': {'tasks': None}},
                        {'code': 0, 'data': {'tasks': {}}}):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = response(payload)
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                ns['fetch_growth_tasks'](account())

    def test_transport_failure_is_not_an_empty_list(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = RuntimeError('offline failure')
        with self.assertRaises(RuntimeError):
            ns['fetch_growth_tasks'](account())

    def test_missing_status_is_unknown_not_not_accepted(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.return_value = response({'code': 0,
            'data': {'tasks': [{'task_code': 'Library_read'}]}})
        self.assertEqual(ns['fetch_growth_tasks'](account())[0]['status'], 'unknown')

    def test_discovery_merges_duplicates_preferring_default_without_losing_mp_only(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(side_effect=[
            [task('shared', 'accepted')], [task('shared', 'claimed'), task()]])
        discover = ns.get('discover_growth_tasks')
        self.assertIsNotNone(discover, 'default+MP discovery is missing')
        result = discover(account())
        self.assertTrue(result['ok'])
        self.assertEqual([t['task_code'] for t in result['tasks']], ['shared', 'Library_read'])
        self.assertEqual([t['mp'] for t in result['tasks']], [False, True])
        self.assertEqual(result['tasks'][0]['status'], 'accepted')

    def test_discovery_partial_failure_retains_tasks_and_error(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(side_effect=[RuntimeError('default failed'), [task()]])
        discover = ns.get('discover_growth_tasks')
        self.assertIsNotNone(discover, 'default+MP discovery is missing')
        result = discover(account())
        self.assertFalse(result['ok'])
        self.assertEqual(result['tasks'][0]['mp'], True)
        self.assertIn('default failed', str(result['errors']))


class ClaimTests(unittest.TestCase):
    def test_zero_receipt_distinguishes_readback_states_without_credit_success(self):
        for rows, state in (([task(status='claimed')], 'claimed'),
                            ([task(status='completed')], 'pending'),
                            ([task(status='accepted')], 'pending'),
                            ([task(status='unknown')], 'unknown'), ([], 'unknown')):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': {'credit': 0, 'energy': 0}})
            fetch = ns['fetch_growth_tasks'] = Mock(return_value=rows)
            result = ns['claim_task'](account(), 'Library_read', mp=True)
            with self.subTest(state=state):
                self.assertFalse(result['ok'])
                self.assertEqual(result['state'], state)
                self.assertFalse(result['reward_verified'])
                self.assertEqual(result['credit'], 0)
                self.assertTrue(fetch.call_args.kwargs['mp'])

    def test_zero_receipt_read_error_is_explicit(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': {}})
        ns['fetch_growth_tasks'] = Mock(side_effect=RuntimeError('list failed'))
        result = ns['claim_task'](account(), 'Library_read')
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'read_error')
        self.assertIn('list failed', result['msg'])

    def test_positive_reward_receipt_is_explicit_and_already_claimed_is_not_new_credit(self):
        for data, ok in (({'credit': 25, 'energy': 0}, True),
                         ({'credit': 0, 'energy': 5}, True),
                         ({'credit': 25, 'already_claimed': True}, False)):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': data})
            ns['fetch_growth_tasks'] = Mock(return_value=[task(status='claimed')])
            result = ns['claim_task'](account(), 'Library_read')
            with self.subTest(data=data):
                self.assertEqual(result['ok'], ok)
                self.assertEqual(result.get('reward_verified'), ok)
                if not ok:
                    self.assertEqual(result['credit'], 0)

    def test_terminal_http_claim_never_web_fallback(self):
        for status in (401, 403, 429):
            ns = load_tasks()
            ns['_accounts'].urlopen.side_effect = urllib.error.HTTPError('https://offline.invalid',
                status, 'terminal', {}, io.BytesIO(b'{}'))
            result = ns['claim_task'](account(), 'Library_read')
            self.assertFalse(result['ok'])
            self.assertEqual(ns['_accounts'].urlopen.call_count, 1)


class TerminalRunnerTests(unittest.TestCase):
    def test_initial_terminal_discovery_blocks_other_view_writes(self):
        for kind in ('http', 'business'):
            ns = load_tasks()
            ns['_accounts'].urlopen.side_effect = [
                urllib.error.HTTPError('https://offline.invalid', 429, 'terminal', {}, io.BytesIO(b'{}'))
                if kind == 'http' else response({'code': 11140, 'msg': 'restricted'}),
                response({'code': 0, 'data': {'tasks': [
                    {'task_code': 'Library_read', 'accept_status': 'not_accepted'}]}})]
            ns.update(accept_tasks=Mock(return_value={'ok': True}), report_events=Mock(),
                      claim_task=Mock(), run_miniprogram_tasks=Mock(return_value={'ok': True}))
            result = ns['run_growth_tasks'](account(), gap=0)
            with self.subTest(kind=kind):
                self.assertFalse(result['ok'])
                self.assertTrue(result.get('terminal'), result)
                for name in ('accept_tasks', 'report_events', 'claim_task', 'run_miniprogram_tasks'):
                    ns[name].assert_not_called()

    def test_accept_terminal_stops_origin_and_family_boundaries_preserving_earnings(self):
        for entry in ('run_growth_tasks', 'run_miniprogram_tasks'):
            for status in (401, 403, 429, 11140, 14003, 6004, 6005, 10001):
                ns = load_tasks()
                rows = ([task('first_buddy', 'completed')] if entry == 'run_growth_tasks' else [])
                if rows:
                    rows[0]['task_type'] = 'auto'
                rows += [task('Library_read' if entry == 'run_growth_tasks' else 'Sequential_Tasks_1', mp=False),
                         task('skill_1' if entry == 'run_growth_tasks' else 'Sequential_Tasks_2', mp=True),
                         task('Sequential_Tasks_3', 'accepted', mp=True)]
                ns['discover_growth_tasks'] = Mock(return_value={'ok': True, 'tasks': rows, 'errors': []})
                ns['fetch_growth_tasks'] = Mock(return_value=rows)
                ns['claim_task'] = Mock(return_value={'ok': True, 'reward_verified': True, 'credit': 7})
                ns['report_events'] = Mock()
                ns['report_mp_events'] = Mock()
                net = ns['_accounts'].urlopen
                net.return_value = response({'code': status, 'msg': 'terminal accept'})
                result = ns[entry](account(), gap=0, **({'tasks': rows} if entry == 'run_miniprogram_tasks' else {}))
                with self.subTest(entry=entry, status=status):
                    self.assertEqual(net.call_count, 1, 'continued origin writes')
                    self.assertFalse(result['ok'])
                    self.assertTrue(result.get('terminal'), result)
                    self.assertEqual(result['earned_credit'], 7 if entry == 'run_growth_tasks' else 0)
                    self.assertEqual(ns['claim_task'].call_count, 1 if entry == 'run_growth_tasks' else 0)
                    ns['report_events'].assert_not_called()
                    ns['report_mp_events'].assert_not_called()

    def test_mp_terminal_result_reaches_outer_runner_without_balance_refresh(self):
        ns = load_tasks()
        ns['discover_growth_tasks'] = Mock(return_value={'ok': True,
            'tasks': [task('Sequential_Tasks_1', 'completed', mp=True)], 'errors': []})
        ns['_accounts'].urlopen.return_value = response({'code': 429, 'msg': 'stop now'})
        acc = account()
        result = ns['run_growth_tasks'](acc, gap=0)
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('terminal'), result)
        acc.fetch_credits.assert_not_called()

    def test_post_accept_terminal_read_stops_other_origin(self):
        ns = load_tasks()
        rows = [task(mp=False), task('skill_1', mp=True), task('Sequential_Tasks_1', 'accepted', mp=True)]
        ns['discover_growth_tasks'] = Mock(return_value={'ok': True, 'tasks': rows, 'errors': []})
        ns['accept_tasks'] = Mock(return_value={'ok': True, 'accepted': ['Library_read'], 'failed': []})
        ns['_accounts'].urlopen.side_effect = urllib.error.HTTPError(
            'https://offline.invalid', 403, 'terminal', {}, io.BytesIO(b'{}'))
        ns['report_events'] = Mock()
        ns['report_mp_events'] = Mock()
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('terminal'), result)
        self.assertEqual(ns['accept_tasks'].call_count, 1)
        ns['report_events'].assert_not_called()
        ns['report_mp_events'].assert_not_called()


    def test_terminal_at_report_claim_or_progress_blocks_remaining_account_writes(self):
        for entry in ('run_growth_tasks', 'run_miniprogram_tasks'):
            for stage in ('report', 'claim', 'claim_web', 'claim_readback', 'progress'):
                for kind in ('http', 'business'):
                    ns = load_tasks()
                    code = 'Library_read' if entry == 'run_growth_tasks' else 'Sequential_Tasks_1'
                    rows = [task(code, 'completed' if stage.startswith('claim') else 'accepted'),
                            task('skill_1' if entry == 'run_growth_tasks' else 'Sequential_Tasks_2',
                                 'completed', mp=True)]
                    ns['discover_growth_tasks'] = Mock(return_value={'ok': True, 'tasks': rows, 'errors': []})
                    failure = (urllib.error.HTTPError('https://offline.invalid', 429, 'terminal', {},
                        io.BytesIO(b'{}')) if kind == 'http' else
                        response({'code': 11140, 'msg': 'terminal diagnostic'}))
                    sequence = []
                    if stage == 'progress':
                        sequence.append(response({'code': 0}))
                    if stage == 'claim_web':
                        sequence.append(urllib.error.HTTPError('https://offline.invalid', 400, 'fallback', {},
                            io.BytesIO(b'{}')))
                    if stage == 'claim_readback':
                        sequence.append(response({'code': 0, 'data': {'credit': 0}}))
                    sequence.append(failure)
                    net = ns['_accounts'].urlopen
                    net.side_effect = sequence + [response({'code': 0, 'data': {'credit': 9}})] * 10
                    result = ns[entry](account(), gap=0,
                        **({'tasks': rows} if entry == 'run_miniprogram_tasks' else {}))
                    with self.subTest(entry=entry, stage=stage, kind=kind):
                        self.assertEqual(net.call_count, len(sequence), 'continued writes after terminal stage')
                        self.assertFalse(result['ok'], result)
                        self.assertTrue(result.get('terminal'), result)
                        self.assertEqual(result['earned_credit'], 0)
                        self.assertIn('429' if kind == 'http' else '11140', str(result['logs']))


class ProgressOutcomeTests(unittest.TestCase):
    def test_progress_error_reaches_runner_and_real_api_dashboard(self):
        import sys
        import subprocess
        from unittest.mock import patch
        for auto in (False, True):
            ns = load_tasks()
            rows = [task('skill_1', 'completed'), task(status='accepted'),
                    task('Sequential_Tasks_1', 'accepted', mp=True)]
            if auto:
                rows[1]['task_type'] = 'auto'
            ns['discover_growth_tasks'] = Mock(return_value={'ok': True, 'tasks': rows, 'errors': []})
            ns.update(build_event=Mock(return_value={}), report_events=Mock(return_value=True),
                report_mp_events=Mock(return_value=True),
                fetch_growth_tasks=Mock(side_effect=RuntimeError('progress evidence unavailable')),
                claim_task=Mock(return_value={'ok': True, 'reward_verified': True, 'credit': 7}))
            module = SimpleNamespace(run_growth_tasks=ns['run_growth_tasks'])
            proxy = proxy_code(dict(POOL=SimpleNamespace(get=lambda _: account()),
                invalidate_tasks_cache=Mock(), restriction_hold_remaining=lambda _: 0,
                add_log_entry=Mock(), time=SimpleNamespace(sleep=lambda _: None)))
            with patch.dict(sys.modules, {'wb_tasks': module}):
                result = proxy['_route_tasks_run'](SimpleNamespace(_json_panel=lambda status, data: data),
                    {'uid': 'offline'})
            with self.subTest(auto=auto):
                self.assertFalse(result['ok'], result)
                self.assertIn('progress evidence unavailable', str(result['logs']))
                self.assertEqual(result['credit_added'], 0 if auto else 7)
                ns['report_mp_events'].assert_not_called()
            source = (ROOT / 'dashboard.html').read_text()
            helper = 'async function runGrowthTasks(btn, uidOverride){' + source.split(
                'async function runGrowthTasks(btn, uidOverride){', 1)[1].split('\nasync function ', 1)[0]
            js = "const assert=require('node:assert/strict'); const payload=" + json.dumps(result) + ";" + """
                const messages=[]; const document={getElementById:()=>null};
                const taskUidForRequest=()=> 'offline', postJSON=async()=>payload;
                const loadAccounts=async()=>{},loadGrowthTasks=async()=>{},toast=(...a)=>messages.push(a);
                """ + helper + """
                (async()=>{assert.equal(await runGrowthTasks(null),false);
                  assert.equal(messages.length,1);assert.equal(messages[0][1],'bad');
                })().catch(e=>{console.error(e);process.exit(1)});
                """
            executed = subprocess.run(['node', '-e', js], capture_output=True, text=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_nonterminal_progress_read_failure_cannot_be_reported_as_runner_success(self):
        ns = load_tasks()
        ns['discover_growth_tasks'] = Mock(return_value={'ok': True,
            'tasks': [task(status='accepted')], 'errors': []})
        ns.update(build_event=Mock(return_value={}), report_events=Mock(return_value=True),
            fetch_growth_tasks=Mock(side_effect=RuntimeError('progress unavailable')), claim_task=Mock())
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertFalse(result['ok'], result)
        self.assertFalse(result.get('terminal', True))
        self.assertEqual(result['errors'], ['progress unavailable'])
        ns['claim_task'].assert_not_called()

    def test_progress_failure_numeric_contract_is_preserved(self):
        ns = load_tasks()
        ns.update(build_event=Mock(return_value={}), report_events=Mock(return_value=True),
                  fetch_growth_tasks=Mock(side_effect=RuntimeError('progress unavailable')))
        outcome = ns['_progress_one'](account(), task(status='accepted'), [], 0)
        self.assertEqual(outcome, 0)
        self.assertEqual(7 + outcome, 7)
        self.assertFalse(getattr(outcome, 'ok', True))
        self.assertIn('progress unavailable', getattr(outcome, 'error', ''))


class RunnerTests(unittest.TestCase):
    def test_mp_only_library_origin_reaches_accept_progress_and_claim(self):
        ns = load_tasks()
        state = {'status': 'not_accepted'}
        calls = []
        def fetch(acc, mp=False):
            calls.append(('fetch', mp))
            return [task(status=state['status'], mp=True)] if mp else []
        def accept(acc, codes, **kwargs):
            calls.append(('accept', kwargs.get('mp', False)))
            state['status'] = 'accepted'
            return {'ok': True, 'accepted': codes, 'failed': []}
        def report(acc, events):
            calls.append(('report',))
            state['status'] = 'completed'
            return True
        def claim(acc, code, **kwargs):
            calls.append(('claim', kwargs.get('mp', False)))
            return {'ok': True, 'reward_verified': True, 'credit': 7}
        ns.update(fetch_growth_tasks=fetch, accept_tasks=accept, report_events=report,
                  claim_task=claim, build_event=lambda *a, **kw: {'existing': 'library event'})
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertEqual(result['earned_credit'], 7)
        self.assertIn(('accept', True), calls)
        self.assertIn(('claim', True), calls)
        self.assertEqual(calls.count(('report',)), 1)

    def test_ok_true_unverified_never_earns_or_logs_claim_success(self):
        for entry in ('_progress_one', 'run_miniprogram_tasks', 'run_night_growth'):
            ns = load_tasks()
            code = {'_progress_one': 'Library_read', 'run_miniprogram_tasks': 'Sequential_Tasks_1',
                    'run_night_growth': 'black_cat'}[entry]
            t = task(code, 'completed')
            t['current'] = 1
            ns['fetch_growth_tasks'] = Mock(return_value=[t])
            ns['claim_task'] = Mock(return_value={'ok': True, 'credit': 100,
                'reward_verified': False, 'state': 'unknown', 'msg': '未确认新增奖励'})
            logs = []
            if entry == '_progress_one':
                earned = ns[entry](account(), t, logs, 0)
            else:
                result = ns[entry](account())
                earned, logs = result['earned_credit'], result['logs']
            with self.subTest(entry=entry):
                self.assertEqual(earned, 0)
                self.assertNotIn('✓', '\n'.join(logs))
                self.assertNotIn('领奖成功', '\n'.join(logs))

    def test_growth_runner_does_not_retry_accept_outside_bounded_accept_helper(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(side_effect=lambda acc, mp=False:
            [] if mp else [task()])
        accept = ns['accept_tasks'] = Mock(return_value={'ok': False, 'accepted': [], 'failed': ['Library_read']})
        ns['run_growth_tasks'](account(), gap=0)
        self.assertEqual(accept.call_count, 1)

    def test_progress_read_failure_is_explicit_and_does_not_claim(self):
        ns = load_tasks()
        ns.update(build_event=Mock(return_value={}), report_events=Mock(return_value=True),
                  fetch_growth_tasks=Mock(side_effect=RuntimeError('list read failed')),
                  claim_task=Mock())
        logs = []
        self.assertEqual(ns['_progress_one'](account(), task(status='accepted'), logs, 0), 0)
        self.assertIn('list read failed', '\n'.join(logs))
        ns['claim_task'].assert_not_called()


def proxy_code(ns):
    tree = ast.parse((ROOT / 'wb_proxy.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {
                '_route_tasks_claim', '_route_tasks_run', '_get_tasks', 'growth_snapshot'}:
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'wb_proxy.py', 'exec'), ns)
    return ns


class CallerTests(unittest.TestCase):
    def test_manual_claim_discovers_mp_origin_and_preserves_unverified_state(self):
        import sys
        from unittest.mock import patch
        t = task(mp=True)
        claim = Mock(return_value={'ok': True, 'reward_verified': False,
            'state': 'claimed', 'credit': 0, 'msg': '任务已领取；未确认本次新增奖励'})
        module = SimpleNamespace(claim_task=claim,
            discover_growth_tasks=Mock(return_value={'ok': True, 'tasks': [t], 'errors': []}))
        ns = proxy_code(dict(POOL=SimpleNamespace(get=lambda _: account()),
            invalidate_tasks_cache=Mock(), restriction_hold_remaining=lambda _: 0))
        handler = SimpleNamespace(_json_panel=lambda status, data: data)
        with patch.dict(sys.modules, {'wb_tasks': module}):
            result = ns['_route_tasks_claim'](handler, {'uid': 'offline', 'code': 'Library_read'})
        self.assertTrue(claim.call_args.kwargs.get('mp'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'claimed')
        self.assertFalse(result['reward_verified'])

    def test_bulk_route_propagates_runner_read_failure(self):
        import sys
        from unittest.mock import patch
        ns = proxy_code(dict(POOL=SimpleNamespace(get=lambda _: account()),
            invalidate_tasks_cache=Mock(), restriction_hold_remaining=lambda _: 0,
            add_log_entry=Mock(), time=SimpleNamespace(sleep=lambda _: None)))
        module = SimpleNamespace(run_growth_tasks=Mock(return_value={
            'ok': False, 'earned_credit': 0, 'logs': ['read failed']}))
        with patch.dict(sys.modules, {'wb_tasks': module}):
            result = ns['_route_tasks_run'](SimpleNamespace(_json_panel=lambda status, data: data),
                {'uid': 'offline'})
        self.assertFalse(result['ok'])

    def test_snapshot_uses_merged_discovery_and_exposes_failure(self):
        import sys
        from unittest.mock import patch
        discovery = {'ok': False, 'tasks': [task(mp=True)], 'errors': ['default read failed']}
        module = SimpleNamespace(discover_growth_tasks=Mock(return_value=discovery),
            fetch_growth_summary=Mock(return_value={'energy': 0}))
        ns = proxy_code(dict(TASKS_CACHE_TTL=0,
            _both=lambda tasks, summary, acc: (tasks(acc), summary(acc))))
        with patch.dict(sys.modules, {'wb_tasks': module}):
            result = ns['growth_snapshot'](account())
        self.assertEqual(result[0]['tasks'][0]['mp'], True)
        self.assertFalse(result[0]['ok'])

    def test_manual_routes_block_task_inactive_banned_enterprise_and_realm_hold(self):
        import sys
        from unittest.mock import patch
        for entry in ('_route_tasks_claim', '_route_tasks_run'):
            for kind in ('inactive', 'banned', 'enterprise', 'hold'):
                acc = account(task_active=lambda: kind != 'inactive',
                    account_banned=kind == 'banned', is_enterprise=lambda: kind == 'enterprise')
                module = SimpleNamespace(claim_task=Mock(return_value={'ok': True, 'reward_verified': True}),
                    run_growth_tasks=Mock(return_value={'ok': True, 'earned_credit': 0}),
                    discover_growth_tasks=Mock(return_value={'ok': True, 'tasks': [task()], 'errors': []}))
                ns = proxy_code(dict(POOL=SimpleNamespace(get=lambda _: acc),
                    restriction_hold_remaining=lambda _: 30 if kind == 'hold' else 0,
                    invalidate_tasks_cache=Mock(), add_log_entry=Mock(),
                    time=SimpleNamespace(sleep=lambda _: None)))
                with self.subTest(entry=entry, kind=kind), patch.dict(sys.modules, {'wb_tasks': module}):
                    result = ns[entry](SimpleNamespace(_json_panel=lambda status, data: data),
                        {'uid': 'offline', 'code': 'Library_read'})
                    self.assertFalse(result['ok'])
                    module.claim_task.assert_not_called()
                    module.run_growth_tasks.assert_not_called()
                    module.discover_growth_tasks.assert_not_called()

    def test_failed_discovery_is_not_cached(self):
        import sys
        import threading
        from unittest.mock import patch
        discover = Mock(return_value={'ok': False, 'tasks': [], 'errors': ['failed']})
        module = SimpleNamespace(discover_growth_tasks=discover, fetch_growth_summary=Mock(return_value={}))
        ns = proxy_code(dict(TASKS_CACHE_TTL=20, _tasks_cache={}, _tasks_cache_lock=threading.Lock(),
            time=SimpleNamespace(time=lambda: 1), _both=lambda tasks, summary, acc: (tasks(acc), summary(acc))))
        with patch.dict(sys.modules, {'wb_tasks': module}):
            ns['growth_snapshot'](account())
            ns['growth_snapshot'](account())
        self.assertEqual(discover.call_count, 2)

    def test_dashboard_claim_runs_real_helper_and_warns_on_legacy_ok(self):
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node required; run in wb2hub-eval:node')
        source = (ROOT / 'dashboard.html').read_text()
        helper = 'async function claimGrowthTask(btn){' + source.split('async function claimGrowthTask(btn){', 1)[1].split('\nasync function ', 1)[0]
        js = """const assert=require('node:assert/strict');
        let CURRENT_GROWTH_UID='offline'; let notifications=[];
        let payload={ok:true,reward_verified:false,state:'claimed',msg:'未确认新增奖励'};
        const postJSON=async()=>payload, toast=(...args)=>notifications.push(args);
        const loadGrowthTasks=async()=>{}, loadAccounts=async()=>{};
        """ + helper + """
        (async()=>{
          const button={dataset:{code:'Library_read'},disabled:false};
          await claimGrowthTask(button);
          assert.deepEqual(notifications.pop(),['未确认新增奖励','warn']);
          assert.equal(button.disabled,false);
          payload={ok:true,reward_verified:true,credit:7,msg:'奖励回执 +7'};
          await claimGrowthTask(button);
          assert.deepEqual(notifications.pop(),['奖励回执 +7','ok']);
        })().catch(e=>{console.error(e);process.exit(1)});
        """
        result = subprocess.run(['node', '-e', js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_claim_uses_verification_and_origin(self):
        source = (ROOT / 'dashboard.html').read_text()
        claim = source.split('async function claimGrowthTask(btn){', 1)[1].split('\nasync function ', 1)[0]
        self.assertIn('reward_verified', claim)
        self.assertIn('r.msg', claim)
        self.assertIn('data-mp=', source)


class EdgeTests(unittest.TestCase):
    def test_code_zero_without_per_item_results_can_retry_unchanged_once(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': {}})
        ns['fetch_growth_tasks'] = Mock(side_effect=[[task()], [task(status='accepted')]])
        self.assertTrue(ns['accept_tasks'](account(), ['Library_read'])['ok'])
        self.assertEqual(ns['_accounts'].urlopen.call_count, 2)

    def test_web_attempt_limit_is_per_invocation_even_across_chunks(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.return_value = receipt('a', 'b')
        ns['fetch_growth_tasks'] = Mock(return_value=[task('a'), task('b')])
        result = ns['accept_tasks'](account(), ['a', 'b'], chunk=1)
        self.assertFalse(result['ok'])
        writes = [c.args[0] for c in ns['_accounts'].urlopen.call_args_list]
        self.assertEqual(sum(r.full_url.startswith('https://web.invalid') for r in writes), 1)

    def test_unknown_acceptance_never_reports_events(self):
        ns = load_tasks()
        ns.update(build_event=Mock(return_value={}), report_events=Mock(return_value=True),
                  fetch_growth_tasks=Mock(return_value=[]), claim_task=Mock())
        ns['_progress_one'](account(), task(status='unknown'), [], 0)
        ns['report_events'].assert_not_called()

    def test_miniprogram_runner_preserves_discovered_default_origin(self):
        ns = load_tasks()
        ns['claim_task'] = Mock(return_value={'ok': False})
        ns['run_miniprogram_tasks'](account(), tasks=[task('Sequential_Tasks_1', 'completed', mp=False)])
        self.assertFalse(ns['claim_task'].call_args.kwargs.get('mp'))

    def test_failed_night_list_returns_error_without_writes(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(side_effect=RuntimeError('night list failed'))
        ns['report_events'] = Mock()
        result = ns['run_night_growth'](account())
        self.assertFalse(result['ok'])
        self.assertIn('night list failed', str(result['logs']))
        ns['report_events'].assert_not_called()

    def test_terminal_business_receipt_never_falls_back(self):
        for status in (401, 403, 429):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = response({'code': status})
            ns['fetch_growth_tasks'] = Mock(return_value=[task()])
            self.assertFalse(ns['accept_tasks'](account(), ['Library_read'])['ok'])
            self.assertEqual(ns['_accounts'].urlopen.call_count, 1)

    def test_terminal_first_batch_stops_later_writes(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = [
            urllib.error.HTTPError('https://offline.invalid', 429, 'terminal', {}, io.BytesIO(b'{}')),
            receipt('b'), receipt('b')]
        ns['fetch_growth_tasks'] = Mock(return_value=[task('b')])
        self.assertFalse(ns['accept_tasks'](account(), ['a', 'b'], chunk=1)['ok'])
        self.assertEqual(ns['_accounts'].urlopen.call_count, 1)

    def test_unknown_or_missing_readback_never_web_retries(self):
        for rows in ([], [task(status='unknown')], [task('different')]):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = receipt('Library_read')
            ns['fetch_growth_tasks'] = Mock(return_value=rows)
            self.assertFalse(ns['accept_tasks'](account(), ['Library_read'])['ok'])
            self.assertEqual(ns['_accounts'].urlopen.call_count, 1)

    def test_readback_overrides_false_receipt_failure_without_retry(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': {'results': [
            {'task_code': 'Library_read', 'status': 'failed'}]}})
        ns['fetch_growth_tasks'] = Mock(return_value=[task(status='accepted')])
        self.assertTrue(ns['accept_tasks'](account(), ['Library_read'])['ok'])
        self.assertEqual(ns['_accounts'].urlopen.call_count, 1)

    def test_progress_unknown_status_and_zero_target_do_not_claim(self):
        ns = load_tasks()
        ns['claim_task'] = Mock()
        t = task(status='unknown')
        t['target'] = 0
        ns['_progress_one'](account(), t, [], 0)
        ns['claim_task'].assert_not_called()

    def test_runners_gate_inactive_and_banned_before_discovery(self):
        for entry in ('run_growth_tasks', 'run_miniprogram_tasks', 'run_night_growth'):
            for kwargs in ({'account_banned': True}, {'task_active': lambda: False}):
                ns = load_tasks()
                ns['fetch_growth_tasks'] = Mock()
                ns[entry](account(**kwargs))
                ns['fetch_growth_tasks'].assert_not_called()

    def test_cn_and_enterprise_gates_do_not_query(self):
        for entry in ('run_growth_tasks', 'run_miniprogram_tasks', 'run_night_growth'):
            for kwargs in ({'realm': 'intl'}, {'is_enterprise': lambda: True}):
                ns = load_tasks()
                ns['fetch_growth_tasks'] = Mock()
                ns[entry](account(**kwargs))
                ns['fetch_growth_tasks'].assert_not_called()

    def test_empty_successful_discovery_is_distinct_from_all_failed(self):
        for error in (False, True):
            ns = load_tasks()
            ns['fetch_growth_tasks'] = Mock(side_effect=RuntimeError('failed')) if error else Mock(return_value=[])
            result = ns['discover_growth_tasks'](account())
            self.assertEqual(result['ok'], not error)
            self.assertEqual(result['tasks'], [])
            self.assertEqual(bool(result['errors']), error)

    def test_web_claim_zero_uses_same_readback_semantics(self):
        ns = load_tasks()
        ns['_accounts'].urlopen.side_effect = [
            urllib.error.HTTPError('https://offline.invalid', 400, 'bad request', {}, io.BytesIO(b'{}')),
            response({'code': 0, 'data': {'credit': 0, 'energy': 0}})]
        ns['fetch_growth_tasks'] = Mock(return_value=[task(status='claimed')])
        result = ns['claim_task'](account(), 'Library_read', mp=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'claimed')
        self.assertTrue(ns['fetch_growth_tasks'].call_args.kwargs['mp'])

    def test_invalid_credit_never_becomes_earned(self):
        for value in (None, True, '100', -1, float('inf')):
            ns = load_tasks()
            ns['_accounts'].urlopen.return_value = response({'code': 0, 'data': {'credit': value}})
            ns['fetch_growth_tasks'] = Mock(return_value=[task(status='claimed')])
            result = ns['claim_task'](account(), 'Library_read')
            self.assertFalse(result['ok'])
            self.assertEqual(result['credit'], 0)

    def test_sequential_task_seen_in_both_views_is_not_claimed_twice(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(return_value=[task('Sequential_Tasks_7', 'completed')])
        ns['claim_task'] = Mock(return_value={'ok': True, 'reward_verified': True, 'credit': 7})
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertEqual(result['earned_credit'], 7)
        self.assertEqual(ns['claim_task'].call_count, 1)

    def test_mp_progress_read_failure_never_claims(self):
        ns = load_tasks()
        ns.update(report_mp_events=Mock(return_value=True), claim_task=Mock(),
            fetch_growth_tasks=Mock(side_effect=RuntimeError('mp progress read failed')))
        result = ns['run_miniprogram_tasks'](account(), gap=0,
            tasks=[task('Sequential_Tasks_1', 'accepted', mp=True)])
        ns['claim_task'].assert_not_called()
        self.assertFalse(result['ok'])
        self.assertIn('mp progress read failed', str(result['logs']))

    def test_night_progress_read_failure_is_explicit(self):
        ns = load_tasks()
        ns.update(report_events=Mock(return_value=True), claim_task=Mock(),
            fetch_growth_tasks=Mock(side_effect=[[task('black_cat', 'accepted')],
                RuntimeError('night progress failed')]))
        result = ns['run_night_growth'](account())
        self.assertFalse(result['ok'])
        self.assertIn('night progress failed', str(result['logs']))
        ns['claim_task'].assert_not_called()

    def test_pc_sequential_seven_keeps_existing_playbook_progress_path(self):
        ns = load_tasks()
        state = {'done': False}
        ns['fetch_growth_tasks'] = Mock(side_effect=lambda acc, mp=False: [task(
            'Sequential_Tasks_7', 'completed' if state['done'] else 'accepted')])
        def report(acc, events):
            state['done'] = True
            return True
        ns['report_events'] = Mock(side_effect=report)
        ns['build_event'] = Mock(return_value={})
        ns['claim_task'] = Mock(return_value={'ok': True, 'reward_verified': True, 'credit': 7})
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertTrue(state['done'])
        self.assertEqual(result['earned_credit'], 7)
        self.assertEqual(ns['claim_task'].call_count, 1)

    def test_night_unverified_message_cannot_create_earned_credit_by_regex(self):
        ns = load_tasks()
        ns.update(report_events=Mock(return_value=True),
            claim_task=Mock(return_value={'ok': False, 'reward_verified': False,
                'credit': 0, 'msg': 'expected +100 积分 but unverified'}),
            fetch_growth_tasks=Mock(side_effect=[
                [task('black_cat', 'accepted')],
                [dict(task('black_cat', 'completed'), current=1)]]))
        result = ns['run_night_growth'](account())
        self.assertEqual(result['earned_credit'], 0)
        self.assertFalse(result['ok'])

    def test_buddy_prerequisite_read_failure_stays_a_safe_false(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(side_effect=RuntimeError('buddy list failed'))
        self.assertFalse(ns['ensure_buddy'](account(), gap=0))

    def test_unverified_growth_summary_does_not_claim_new_credit(self):
        ns = load_tasks()
        ns['fetch_growth_tasks'] = Mock(return_value=[task(status='completed')])
        ns['claim_task'] = Mock(return_value={'ok': True, 'reward_verified': False,
            'credit': 0, 'msg': '未确认新增奖励'})
        result = ns['run_growth_tasks'](account(), gap=0)
        self.assertEqual(result['earned_credit'], 0)
        self.assertNotIn('新增到账', '\n'.join(result['logs']))

    def test_missing_verification_even_with_positive_credit_is_not_earned(self):
        ns = load_tasks()
        ns['claim_task'] = Mock(return_value={'ok': True, 'credit': 100})
        logs = []
        self.assertEqual(ns['_progress_one'](account(), task(status='completed'), logs, 0), 0)
        self.assertNotIn('✓', '\n'.join(logs))


if __name__ == '__main__':
    unittest.main()
