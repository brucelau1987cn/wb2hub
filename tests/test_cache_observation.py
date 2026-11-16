"""Cache observation contract against real AST functions, streams, storage and DOM."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load_proxy():
    # Compile actual functions only: no startup, accounts, network or persistent state.
    tree = ast.parse((ROOT / 'wb_proxy.py').read_text())
    ns: dict[str, Any] = dict(json=json, os=os, re=re, time=time, uuid=uuid, POOL=None,
              CURRENT_REALM='cn', SSE_READ_BLOCK=65536, _NS_SEP='__',
              CJK_RE=re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]'),
              log=lambda *a, **kw: None, key_label=lambda k: k,
              wb_pricing=type('Pricing', (), {'current_policy_id': staticmethod(lambda m: 'test')})())
    names = {'record_usage', 'record_error', '_extract_usage', '_best_cached_tokens',
             'normalize_usage_cache_aliases', 'aggregate_stream', 'iter_sse_lines',
             'strip_data_prefix', 'repair_leaked_tool_calls', 'estimate_tokens', '_new_id',
             'cleanup_orphan_tool_calls', 'is_truncated_arguments', 'parse_dsml_tool_calls',
             'resolve_namespaced_name', 'stamp_namespace', 'apply_namespace_to_calls',
             '_unwrap_custom_input', 'stream_responses_events', '_responses_usage',
             'web_tools_active', 'sum_usage', '_sum_usage_values', 'stream_messages_events',
             '_anthropic_usage', 'anthropic_sse_frame', '_anthropic_text',
             '_anthropic_message_id', '_anthropic_stop_reason', 'anthropic_error_type', '_new_analytics_stat',
             '_analytics_row_values', '_feed_analytics', '_finalize_analytics_stat',
             '_empty_stats', '_new_series_bucket', '_feed_series_bucket',
             '_fold_usage_by_account', 'row_outcome', '_series_output_bucket'}
    nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and (n.name in names or n.name.startswith(('_cache_', '_merge_usage', '_row_cache', '_stream_')))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'wb_proxy.py'), 'exec'), ns)
    ns['_persist_usage'] = lambda row, label: ns.setdefault('persisted', []).append(dict(row))
    return ns


def frame(usage=None, text=None, finish=None):
    obj = {'choices': [{'delta': {'content': text} if text else {}, 'finish_reason': finish}]}
    if usage is not None:
        obj['usage'] = usage
    return ('data: ' + json.dumps(obj) + '\n\n').encode()


class CacheObservationTests(unittest.TestCase):
    def setUp(self):
        self.p = load_proxy()

    def test_recorded_requests_distinguish_unknown_zero_positive(self):
        for usage, observed, cached, pct in [
            (None, False, 0, None),
            ({'prompt_tokens': 100}, False, 0, None),
            ({'prompt_tokens': 100, 'cached_tokens': 0}, True, 0, 0.0),
            ({'prompt_tokens': 100, 'cached_tokens': 80}, True, 80, 80.0),
        ]:
            with self.subTest(usage=usage):
                row = self.p['record_usage']('model', usage)
                self.assertIs(row.get('cache_observed'), observed)
                self.assertEqual(row.get('cached_tokens', 0), cached)
                self.assertEqual(row.get('cache_hit_pct'), pct)
                self.assertEqual(self.p['persisted'][-1], row)
                err = self.p['record_error']('model', 502, 'test', usage=usage)
                self.assertIs(err.get('cache_observed'), observed)

    def test_stream_frames_inherit_only_missing_cache(self):
        hit = {'prompt_tokens': 100, 'total_tokens': 101,
               'prompt_tokens_details': {'cached_tokens': 80}, 'cached_tokens': 0}
        for tail, expected in [
            ({'prompt_tokens': 100, 'total_tokens': 102}, 80),
            ({'prompt_tokens': 100, 'total_tokens': 102,
              'prompt_tokens_details': {'cached_tokens': 0}}, 0),
            ({'prompt_tokens': 100, 'total_tokens': 102, 'cached_tokens': 0}, 80),
            ({'prompt_tokens_details': {'cached_tokens': 0}}, 0),
        ]:
            with self.subTest(tail=tail):
                rows = [frame(hit), frame(tail), frame(finish='stop'), b'data: [DONE]\n\n']
                out = self.p['aggregate_stream'](rows, 'm', 'id')
                fields = self.p['_extract_usage'](out['usage'])
                self.assertTrue(fields['cache_observed'])
                self.assertEqual(fields['cached_tokens'], expected)
                holder = {}
                list(self.p['stream_responses_events'](rows, 'm', holder=holder))
                self.assertEqual(self.p['_extract_usage'](holder['usage'])['cached_tokens'], expected)

    def test_authoritative_zero_keeps_source_for_subsequent_frames(self):
        usage = {'prompt_tokens_details': {'cached_tokens': 80}}
        usage = self.p['_merge_usage_frame'](usage, {
            'prompt_tokens_details': {'cached_tokens': 0}, 'cached_tokens': 0})
        usage = self.p['_merge_usage_frame'](usage, {'cache_read_input_tokens': 0})
        self.assertEqual(self.p['_cache_observation'](usage)[1], ('prompt_tokens_details', 'cached_tokens'))
        usage = self.p['_merge_usage_frame']({'input_tokens_details': {'cached_tokens': 80}}, {
            'input_tokens_details': {'cached_tokens': 0}, 'cached_tokens': 0})
        self.assertEqual(self.p['_cache_observation'](usage)[1], ('input_tokens_details', 'cached_tokens'))

    def test_estimated_usage_does_not_invent_known_miss(self):
        out = self.p['aggregate_stream']([frame(text='hello'), frame(finish='stop')], 'm', 'id')
        self.assertFalse(self.p['_extract_usage'](out['usage'])['cache_observed'])

    def test_estimated_tokens_preserve_prior_cache_observation(self):
        rows = [frame({'cached_tokens': 4}, text='hello'), frame(finish='stop')]
        out = self.p['aggregate_stream'](rows, 'm', 'id')
        self.assertEqual(self.p['_extract_usage'](out['usage'])['cached_tokens'], 4)
        self.assertTrue(self.p['_extract_usage'](out['usage'])['cache_observed'])

    def test_alias_validation_and_first_positive_precedence(self):
        for bad in [False, True, -1, 'bad', float('nan'), float('inf'), -float('inf'), {}, []]:
            with self.subTest(bad=bad):
                fields = self.p['_extract_usage']({'prompt_tokens': 100, 'cached_tokens': bad})
                self.assertFalse(fields['cache_observed'])
                self.assertEqual(fields['cached_tokens'], 0)
        for bad in [[], True, 'bad']:
            self.assertEqual(self.p['_extract_usage'](bad), {})
        self.assertEqual(self.p['_best_cached_tokens']({'cached_tokens': '0'}), 0)
        for key in ['prompt_tokens_details', 'input_tokens_details', 'completion_tokens_details']:
            usage = {key: ['bad'], 'cached_tokens': 4}
            self.p['normalize_usage_cache_aliases'](usage)
            self.assertEqual(self.p['_extract_usage'](usage)['cached_tokens'], 4)
        usage = {'prompt_tokens_details': {'cached_tokens': 0}, 'cache_read_input_tokens': 42}
        self.assertEqual(self.p['_best_cached_tokens'](usage), 42)
        self.assertEqual(self.p['_best_cached_tokens']({'prompt_tokens_details': [], 'cached_tokens': 4}), 4)

    def test_legacy_rows_and_aggregates_exclude_unknown_denominators(self):
        rows = [
            {'prompt_tokens': 900, 'cached_tokens': 0, 'cache_hit_pct': 0},
            {'prompt_tokens': 100, 'cached_tokens': 0, 'cache_observed': True},
            {'prompt_tokens': 100, 'cached_tokens': 80},
        ]
        stat = self.p['_new_analytics_stat']()
        series = self.p['_new_series_bucket'](0)
        accounts = {}
        for row in rows:
            self.p['_feed_analytics'](stat, self.p['_analytics_row_values'](row), False, 0)
            self.p['_feed_series_bucket'](series, row)
            self.p['_fold_usage_by_account'](row, accounts)
        final = self.p['_finalize_analytics_stat'](stat)
        self.assertEqual(final['cache_hit_pct'], 40.0)
        for bucket in [final, series, accounts['(unattributed)']]:
            self.assertEqual(bucket.get('cache_observed_requests'), 2)
            self.assertEqual(bucket.get('cache_unknown_requests'), 1)
            self.assertEqual(bucket.get('cache_observed_prompt_tokens'), 200)
        unknown = self.p['_new_analytics_stat']()
        self.p['_feed_analytics'](unknown, self.p['_analytics_row_values'](rows[0]), False, 0)
        self.assertIsNone(self.p['_finalize_analytics_stat'](unknown)['cache_hit_pct'])
        self.assertIsNone(self.p['_finalize_analytics_stat'](self.p['_new_analytics_stat']())['cache_hit_pct'])

    def test_dashboard_dom_renders_unknown_and_zero_separately(self):
        html = (ROOT / 'dashboard.html').read_text()
        block = html.split('  // KPI 5: 缓存命中率', 1)[1].split('  // KPI 6:', 1)[0]
        helper = ''
        if 'function cacheObservationRate(' in html:
            helper = html.split('function cacheObservationRate(', 1)[1].split('\nfunction ', 1)[0]
            helper = 'function cacheObservationRate(' + helper
        js = helper + """
        const assert=require('node:assert/strict');
        const nodes = {kpiCacheRate:{textContent:''},kpiCacheDetail:{textContent:''}};
        const document={getElementById:id=>nodes[id]};
        const fmtCompact=String;
        function render(currentSum){ BLOCK }
        render({cache_hit_pct:null, cached_tokens:0, cache_observed_requests:0, cache_unknown_requests:3});
        assert.equal(nodes.kpiCacheRate.textContent,'—');
        assert.match(nodes.kpiCacheDetail.textContent,/未知.*3/);
        render({cache_hit_pct:0, cached_tokens:0, cache_observed_prompt_tokens:100, cache_observed_requests:1, cache_unknown_requests:0});
        assert.equal(nodes.kpiCacheRate.textContent,'0.0%');
        render({cached_tokens:80, cache_observed_prompt_tokens:200, cache_observed_requests:2, cache_unknown_requests:1});
        assert.equal(nodes.kpiCacheRate.textContent,'40.0%');
        console.log('cache DOM evidence OK');
        """.replace('BLOCK', block)
        result = subprocess.run(['node', '-e', js], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tool_round_sum_requires_observation_in_every_part(self):
        hit = {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 80}}
        miss = {'prompt_tokens': 100, 'cached_tokens': 0}
        unknown = {'prompt_tokens': 100}
        total = self.p['sum_usage'](None, hit)
        total = self.p['sum_usage'](total, miss)
        fields = self.p['_extract_usage'](total)
        self.assertTrue(fields['cache_observed'])
        self.assertEqual(fields['cached_tokens'], 80)
        total = self.p['sum_usage'](total, unknown)
        self.assertFalse(self.p['_extract_usage'](total)['cache_observed'])

    def test_anthropic_stream_preserves_cache_evidence(self):
        hit = {'prompt_tokens': 100, 'total_tokens': 101, 'cached_tokens': 80}
        for tail, expected in [({'total_tokens': 102}, 80),
                               ({'total_tokens': 102, 'cached_tokens': 0}, 0)]:
            holder = {}
            rows = [frame(hit), frame(tail), frame(finish='stop'), b'data: [DONE]\n\n']
            list(self.p['stream_messages_events'](rows, 'm', holder=holder))
            self.assertEqual(self.p['_extract_usage'](holder['usage'])['cached_tokens'], expected)
        self.assertEqual(self.p['_responses_usage']({'prompt_tokens': 100})['input_tokens_details']['cached_tokens'], 0)
        self.assertFalse(self.p['_responses_usage']({'prompt_tokens': 100})['cache_observed'])

    def test_matrix_dom_cache_cells_use_observed_prompt_denominator(self):
        html = (ROOT / 'dashboard.html').read_text()
        source = 'function cacheObservationRate(' + html.split('function cacheObservationRate(', 1)[1].split('\nasync function loadAnalytics', 1)[0]
        js = """
        const assert=require('node:assert/strict');
        const nodes={perfMatrix:{innerHTML:''},matrixModelCount:{textContent:''}};
        const document={getElementById:id=>nodes[id]};
        const MATRIX_MODELS=new Set(); const matrixAcctFilter='',matrixModelFilter='';
        const fmt=String,ms=String,esc=String,fmtCost=String;
        const pct=v=>v.toFixed(1)+'%'; const tripleTokens=()=>'';
        const PRICING_ON=false;const applyTableSort=()=>{};
        """ + source + """
        for(const [prompt,known,cached,expected] of [[900,0,0,'—'],[100,100,0,'0.0%'],[1100,200,80,'40.0%']]){
          const stat={requests:1,prompt_tokens:prompt,cache_observed_prompt_tokens:known,cached_tokens:cached,total_tokens:prompt};
          renderPerfMatrix({by_model:{test:stat},...stat},{});
          assert.ok(nodes.perfMatrix.innerHTML.includes('<td data-label="缓存命中">'+expected+'</td>'), nodes.perfMatrix.innerHTML);
        }
        """
        result = subprocess.run(['node', '-e', js], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sparkline_skips_unknown_points(self):
        html = (ROOT / 'dashboard.html').read_text()
        draw = 'function drawSpark(' + html.split('function drawSpark(', 1)[1].split('\nfunction drawSparks', 1)[0]
        js = """
        const assert=require('node:assert/strict');
        let coords=[];
        const ctx=new Proxy({}, {get:(t,k)=> (...args)=> { if(k==='moveTo'||k==='lineTo') coords.push(args); }});
        const $=()=>({clientWidth:120,getContext:()=>ctx});
        const window={devicePixelRatio:1}; const seriesColor=()=> '#fff';
        """ + draw + """
        drawSpark('cache',[null,null], 'blue');
        assert.equal(coords.length,0,'unknown must not draw a zero miss');
        drawSpark('cache',[80,null,0], 'blue');
        assert.ok(coords.length>0);
        assert.ok(coords.every(p=>p.every(Number.isFinite)));
        """
        result = subprocess.run(['node', '-e', js], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
