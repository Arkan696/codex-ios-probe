import unittest
from live_relay import _extract, RelayError


class Mismatch(unittest.TestCase):
    def test_classify_without_echo_or_accepting(self):
        nonce = 'private-marker'
        fmt = 'Meeting: Thursday at 14:00\nReference: private-marker'
        for candidate, flags in [(fmt+'\n','1 1 1'),(fmt.replace('Thursday','Friday'),'0 1 0'),(fmt.replace(nonce,'other'),'1 0 0'),('private-content\n'+fmt,'1 1 0'),(None,None)]:
            with self.subTest(flags=flags), self.assertRaises(RelayError) as err:
                _extract({'choices':[{'message':{'role':'assistant','content':candidate},'finish_reason':'stop'}]}, nonce, 'unused-credential')
            label = 'content=nontext' if flags is None else 'target_line={} reference_line={} whitespace_only={}'.format(*flags.split())
            self.assertEqual(str(err.exception),'Live relay: candidate mismatch: '+label)
