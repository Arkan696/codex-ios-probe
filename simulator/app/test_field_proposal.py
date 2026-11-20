import json
import unittest
from unittest.mock import patch
import field_proposal as f

class FieldTests(unittest.TestCase):
    def test_validation(self):
        for raw in ('{}','{"weekday":"Thursday","weekday":"Friday","time":"14:00"}', '{"weekday":"Thursday","time":"24:30"}', '{"weekday":"Thursday","time":"14:00","extra":1}', '```json\n{}\n```'):
            with self.subTest(raw=raw), self.assertRaises(f.RelayError):
                f.parse(raw)

    def test_one_call_no_source_no_reference(self):
        source = b'Meeting: Tuesday at 10:00\r\nReference: private-sample\x00\xff'
        res = {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"weekday":"Thursday","time":"14:00"}'}}]}
        with patch.object(f, '_api', return_value=res) as call:
            self.assertEqual(f.diagnose('test', source), b'Meeting: Thursday at 14:00\r\nReference: private-sample\x00\xff')
            call.assert_called_once(); payload=json.dumps(call.call_args.args[2]); self.assertNotIn('Reference',payload); self.assertNotIn('private-sample',payload); self.assertNotIn('Tuesday',payload)

    def test_wrong_time_no_preview(self):
        msg={'role':'assistant','content':'{"weekday":"Friday","time":"14:00"}'}
        with patch.object(f,'_api',return_value={'choices':[{'finish_reason':'stop','message':msg}]}), patch.object(f,'preview') as build:
            with self.assertRaisesRegex(f.RelayError,'incorrect requested change'):
                f.diagnose('test', b'')
            build.assert_not_called()
