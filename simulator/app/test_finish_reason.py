import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import field_proposal as f

class FinishReasonTests(unittest.TestCase):
    def test_sanitized_counts_and_reasons(self):
        for res, label in [({},'unavailable finish_reason=not_applicable'), ({'choices':[]},'0 finish_reason=not_applicable'), ({'choices':[{},{}]},'2 finish_reason=not_applicable'), ({'choices':[{}]},'1 finish_reason=missing'), ({'choices':[{'finish_reason':'length'}]},'1 finish_reason=length'), ({'choices':[{'finish_reason':'private-text'}]},'1 finish_reason=other')]:
            with self.subTest(label=label), patch.object(f,'_api',return_value=res) as call, redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(f.RelayError): f.diagnose('offline', b'')
                self.assertEqual(output.getvalue().strip(),'field diagnostic: choices='+label)
                call.assert_called_once()
