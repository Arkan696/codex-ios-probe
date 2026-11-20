"""Offline relay contract tests; no provider credentials or network access."""
import io
import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_relay as relay


NONCE = 'offline-run'
KEY = 'offline-token-not-a-credential'
ORIGINAL = 'Meeting: Tuesday at 10:00\nReference: offline-run'
EDIT = 'Meeting: Thursday at 14:00\nReference: offline-run'
INSTRUCTION = '把会议改到周四下午两点，其他内容不变。'


class Reply(io.BytesIO):
    def __init__(self, obj, status=200):
        super().__init__(obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.status = status
        self.read_sizes = []

    def read(self, n=-1):
        self.read_sizes.append(n)
        assert n > 0, 'unbounded read'
        return super().read(n)


class LiveRelayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        docs = self.root / 'Documents'
        docs.mkdir()
        self.src, self.dest = docs / 'live-request.json', docs / 'live-response.json'
        self.req = dict(runID=NONCE, original=ORIGINAL, instruction=INSTRUCTION)
        self.src.write_text(json.dumps(self.req))
        transport = patch.object(relay.http.client, 'HTTPSConnection', autospec=True)
        self.connect = transport.start()
        self.addCleanup(transport.stop)
        self.conn = self.connect.return_value
        self.catalog = {'data': [{'id': i} for i in ('gpt-6.2-astra', 'gpt-4o-mini-audio', 'gpt-4o-mini')]}
        self.message = dict(role='assistant', content=EDIT)
        self.choice = dict(message=self.message, finish_reason='stop')
        self.result = dict(id='resp_73', model='resolved-model-2026', choices=[self.choice])

    def serve(self, *replies):
        self.conn.getresponse.side_effect = replies
        relay.service(self.root, NONCE, KEY)

    def test_success_wire_contract_and_atomic_publication(self):
        replace = os.replace

        def publish(src, dest):
            self.assertEqual(src.parent, dest.parent)
            self.assertFalse(dest.exists())
            self.assertEqual(json.loads(src.read_bytes())['candidate'], EDIT)
            replace(src, dest)

        trace = self.root / 'tls.log'
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://wrong.invalid', 'SSLKEYLOGFILE': str(trace)}), \
                patch.object(relay.os, 'replace', side_effect=publish) as rename:
            self.serve(Reply(self.catalog), Reply(self.result))
        rename.assert_called_once()
        self.assertFalse(trace.exists())
        self.assertEqual(self.connect.call_count, 2)
        for call in self.connect.call_args_list:
            self.assertEqual(call.args, ('new.maoyu.cfd',))
            ctx = call.kwargs['context']
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        get, post = [c.args for c in self.conn.request.call_args_list]
        self.assertEqual(get[:3], ('GET', '/v1/models', None))
        self.assertEqual(post[:2], ('POST', '/v1/chat/completions'))
        payload = json.loads(post[2]); self.assertEqual(payload['model'], 'gpt-4o-mini')
        self.assertEqual(payload['messages'][1], dict(role='user', content=INSTRUCTION + '\n\n' + ORIGINAL))
        self.assertEqual(payload['messages'][0]['role'], 'system'); self.assertNotIn(EDIT, post[2].decode())
        self.assertFalse(payload['stream']); self.assertEqual(payload['max_tokens'], 512)
        self.assertTrue(all(c[3]['Authorization'] == 'Bearer ' + KEY for c in (get, post)))
        self.assertEqual(json.loads(self.dest.read_bytes()), dict(runID=NONCE, candidate=EDIT,
                         model='resolved-model-2026', responseID='resp_73'))

    def test_bad_input_never_connects(self):
        for raw in [json.dumps({**self.req, k: v}) for k, v in (
                ('runID', 'stale'), ('original', ORIGINAL + ' '), ('instruction', None), ('extra', 1))] + [
                '{}', 'null', '{', ' ' * 4097, '{"original":"ignored",' + json.dumps(self.req)[1:]]:
            self.src.write_text(raw)
            with self.assertRaisesRegex(relay.RelayError, 'invalid local request'):
                self.serve()
        self.src.write_text(json.dumps(self.req))
        with self.assertRaisesRegex(relay.RelayError, 'invalid credential'):
            relay.service(self.root, NONCE, 'bad\r\nheader')
        self.connect.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_flash_preferred_only_when_advertised(self):
        self.serve(Reply({'data': [{'id': 'deepseek-v4-flash'}, {'id': 'gpt-4o-mini'}]}), Reply(self.result))
        self.assertEqual(json.loads(self.conn.request.call_args.args[2])['model'], 'deepseek-v4-flash')

    def test_discovery_fallback_only_to_advertised_model(self):
        self.serve(Reply({'data': [{'id': 'gpt-6.2-astra'}]}), Reply(self.result))
        self.assertEqual(json.loads(self.conn.request.call_args.args[2])['model'], 'gpt-6.2-astra')
        self.conn.reset_mock()
        with self.assertRaisesRegex(relay.RelayError, 'no supported text model'):
            self.serve(Reply({'data': [{'id': 'gpt-4o-mini-tts'}]}))
        self.assertEqual(self.conn.request.call_count, 1)

    def test_http_errors_never_read_redirect_or_retry(self):
        for status in (307, 401, 429, 503):
            self.conn.reset_mock()
            bad = Reply(KEY.encode(), status)
            with self.assertRaisesRegex(relay.RelayError, f'^Live relay /v1/chat/completions: HTTP {status}$'):
                self.serve(Reply(self.catalog), bad)
            self.assertEqual(bad.read_sizes, [])
            self.assertEqual(self.conn.request.call_count, 2)
            self.assertFalse(self.dest.exists())

    def test_unacceptable_generation_never_published_or_retried(self):
        bad_text = dict(self.choice, message=dict(self.message, content=EDIT + '\n'))
        for update in (dict(choices=[]), dict(choices=[bad_text]), dict(choices=[self.choice] * 2),
                       dict(choices=[dict(self.choice, finish_reason='length')]),
                       dict(choices=[dict(self.choice, message=dict(self.message, tool_calls=[{}]))]),
                       dict(id=None), dict(model=KEY)):
            self.conn.reset_mock()
            with self.assertRaises(relay.RelayError) as caught:
                self.serve(Reply(self.catalog), Reply({**self.result, **update}))
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(self.conn.request.call_count, 2)
            self.assertFalse(self.dest.exists())

    def test_bounded_reads_and_sanitized_transport_failure(self):
        for failure in (Reply(b'x' * 32769), Reply(KEY.encode()), OSError(KEY)):
            self.conn.reset_mock()
            with self.assertRaisesRegex(relay.RelayError, '^Live relay: generation failed or malformed response$') as e:
                self.serve(Reply(self.catalog), failure)
            self.assertIsNone(e.exception.__context__)
            if isinstance(failure, Reply):
                self.assertEqual(failure.read_sizes, [32769])
            self.assertEqual(self.conn.request.call_count, 2)
            self.assertFalse(self.dest.exists())

    def test_publish_failure_preserves_previous_file_and_cleans_temp(self):
        self.dest.write_bytes(b'previous')
        with patch.object(relay.os, 'replace', side_effect=OSError(KEY)), \
                self.assertRaisesRegex(relay.RelayError, '^Live relay: could not publish response$'):
            self.serve(Reply(self.catalog), Reply(self.result))
        self.assertEqual(self.dest.read_bytes(), b'previous')
        self.assertEqual(set(self.dest.parent.iterdir()), {self.src, self.dest})
