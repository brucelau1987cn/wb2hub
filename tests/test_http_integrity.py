"""Real loopback HTTP tests; synthetic upstream only, no accounts or external calls."""
import http.client
import io
import json
import pathlib
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import wb_proxy as proxy


def data(delta=None, finish=None):
    return ('data: '+json.dumps({'choices':[{'delta':delta or {},'finish_reason':finish}]})+'\n\n').encode()


class OfflineHandler(proxy.Handler):
    def _key_id(self): return 'offline-test'
    def log_message(self, *args): pass
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0'))
        if n: self.rfile.read(n)
        self._body_consumed = True
        if self.path == '/responses-incomplete':
            upstream=io.BytesIO(data({'content':'partial'}, 'length')+b'data: [DONE]\n\n')
            return self._responses_stream_response(upstream, 'offline-model', set(), {}, '',
                    SimpleNamespace(uid='offline'), time.time(), realm='intl')
        if self.path == '/chat-tool':
            raw=data({'tool_calls':[{'id':'test-call','function':{'name':'run','arguments':'{'}}]})
            raw+=data({'tool_calls':[{'id':'test-call','function':{'name':'run','arguments':'}'}}]},'tool_calls')
            raw+=b'data: [DONE]\n\n'
        else:
            raw=data({'content':'partial'})
        return self._chat_nonstream_response(io.BytesIO(raw), 'offline-model', '',
                    SimpleNamespace(uid='offline'), time.time(), realm='intl')


class HTTPIntegrityTest(unittest.TestCase):
    def test_real_http_error_tool_and_terminal_event(self):
        with patch.object(proxy,'record_error'),patch.object(proxy,'record_upstream_usage'), \
             patch.object(proxy.wb_settings,'egress_backoff_enabled',return_value=False):
            server=ThreadingHTTPServer(('127.0.0.1',0),OfflineHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def request(path):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                    conn.request('POST',path,body=b'{}',headers={'Content-Type':'application/json'})
                    resp=conn.getresponse();status=resp.status;body=resp.read();conn.close()
                    return status,body
                status,body=request('/chat-eof')
                self.assertEqual(status,502)
                self.assertIn(b'incomplete',body)
                status,body=request('/chat-tool')
                self.assertEqual(status,200)
                call=json.loads(body)['choices'][0]['message']['tool_calls'][0]
                self.assertEqual(call['function'],{'name':'run','arguments':'{}'})
                status,body=request('/responses-incomplete')
                self.assertEqual(status,200)
                self.assertIn(b'event: response.incomplete',body)
                self.assertNotIn(b'event: response.completed',body)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=='__main__':unittest.main(verbosity=2)
