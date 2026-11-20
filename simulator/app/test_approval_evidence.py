"""Evidence parser regression tests; simulated fixtures are not simulator results."""
import unittest
from smoke import validate


class ApprovalEvidence(unittest.TestCase):
    def setUp(self):
        old, new = 'a' * 64, 'b' * 64
        self.record = dict(runID='test', code=0, uuid='44ee6470-9787-4427-80aa-c352ddaa8dc1',
                           byteCount=32, expectedSHA256=old, receivedSHA256=old,
                           approval=dict(beforeSHA256=old, previewSHA256=new, rejectedSHA256=old,
                                         approvedSHA256=new, rejectedWrites=0, approvedWrites=1))

    def test_matching(self):
        validate(self.record, 'test')

    def test_invalid_branches(self):
        for field, value in [('rejectedSHA256', 'b' * 64), ('approvedSHA256', 'a' * 64),
                             ('rejectedWrites', 1), ('approvedWrites', 0), ('approvedWrites', True),
                             ('previewSHA256', 'a' * 64)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(dict(self.record, approval={**self.record['approval'], field: value}), 'test')

    def test_old_results_do_not_pass(self):
        del self.record['approval']
        with self.assertRaises(ValueError):
            validate(self.record, 'test')
