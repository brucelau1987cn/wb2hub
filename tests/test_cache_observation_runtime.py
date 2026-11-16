"""Credential-free subprocess integration: real JSONL readers and checkpoints."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CacheObservationRuntimeTests(unittest.TestCase):
    def test_real_readers_legacy_rows_and_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix='cache-observation-') as tmp:
            env = dict(os.environ, ACCOUNTS_DIR=tmp + '/accounts',
                       WB_PROXY_USAGE_DIR=tmp + '/usage', WB_SETTINGS_DIR=tmp,
                       PYTHONDONTWRITEBYTECODE='1')
            script = r'''
import json, os, time
import wb_proxy as p
os.makedirs(os.path.dirname(p.USAGE_LOG), exist_ok=True)
now = time.time()
rows = [dict(at=now - 3, model='test', prompt_tokens=900, cached_tokens=0, cache_hit_pct=0),
        dict(at=now - 2, model='test', prompt_tokens=100, cached_tokens=0, cache_observed=True),
        dict(at=now - 1, model='test', prompt_tokens=100, cached_tokens=80)]
with open(p.USAGE_LOG, 'w') as fh:
    for row in rows: fh.write(json.dumps(row) + '\n')
def verify():
    analytics = p.compute_usage_analytics(ttl=0)
    stat = analytics['summary']['window']
    assert stat['cache_hit_pct'] == 40.0, stat
    assert stat['cache_observed_requests'] == 2, stat
    assert stat['cache_unknown_requests'] == 1, stat
    snap = p.usage_snapshot(ttl=0)
    assert snap['cache_observed_prompt_tokens'] == 200, snap
    assert snap['by_model']['test']['cache_unknown_requests'] == 1
    accts = p.usage_by_account(ttl=0)
    assert accts[0]['cache_observed_requests'] == 2, accts
    recent = p.recent_usage()
    recent = recent['rows'] if isinstance(recent, dict) else recent
    by_prompt = [r for r in recent if r['prompt_tokens'] == 900][0]
    assert by_prompt['cache_observed'] is False, by_prompt
    assert by_prompt['cache_hit_pct'] is None, by_prompt
    perf = p.perf_stats(ttl=0)
    assert perf['cache_hit_pct']['samples'] == 2, perf
    assert perf['cache_hit_pct']['avg'] == 40.0, perf
    series = p.usage_timeseries(range='custom', since=now - 100, until=now + 1, bucket_seconds=60, ttl=0)
    assert sum(b['cache_unknown_requests'] for b in series['series']) == 1, series
    assert sum(b['cache_observed_prompt_tokens'] for b in series['series']) == 200, series
verify()
assert p._USAGE_CACHE_SCHEMA > 5
minutes = {1: p._new_series_bucket(60)}
p._feed_series_bucket(minutes[1], rows[1])
packed = p._usage_cache_minutes_copy(minutes)
assert p._usage_cache_series_minutes_ok(packed)
assert p._usage_cache_minutes_state(packed) == minutes
assert not p._usage_cache_series_minutes_ok([[1] + [0] * 8])
print('real cache readers and checkpoint roundtrip OK')
# Exercise the actual HTTP streaming method, including its last_usage capture.
import io, http.client, threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

def chunk(u=None, finish=None):
    obj = {'choices': [{'delta': {'content': 'ok'} if finish is None else {}, 'finish_reason': finish}]}
    if u is not None: obj['usage'] = u
    return ('data: ' + json.dumps(obj) + '\n\n').encode()

class Handler(p.Handler):
    def _key_id(self): return 'offline'
    def log_message(self, *args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self._body_consumed = True
        hit = dict(prompt_tokens=100, total_tokens=101, prompt_tokens_details={'cached_tokens': 80}, cached_tokens=0)
        tail = dict(prompt_tokens=100, total_tokens=102)
        if 'zero' in self.path: tail['prompt_tokens_details'] = {'cached_tokens': 0}
        raw = chunk(hit) + chunk(tail) + chunk(finish='stop') + b'data: [DONE]\n\n'
        return self._chat_stream_response(io.BytesIO(raw), 'test', {}, SimpleNamespace(uid='offline'), time.time(), realm='cn')

with patch.object(p.wb_settings, 'egress_backoff_enabled', return_value=False):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, expected in [('/carry', 80), ('/zero', 0)]:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            conn.request('POST', path, body=b'{}')
            response = conn.getresponse()
            body = response.read()
            assert response.status == 200, body
            assert b'[DONE]' in body, body
            conn.close()
            with open(p.USAGE_LOG) as fh: stored = json.loads(fh.readlines()[-1])
            assert stored['cache_observed'] is True, stored
            assert stored['cached_tokens'] == expected, stored
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
print('real chat stream HTTP and stored cache rows OK')
'''
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
