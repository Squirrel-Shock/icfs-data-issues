"""python -m unittest test_swaps"""
import datetime
import unittest

from swaps import find

C = [(3650.0, 4200.0), (5850.0, 6425.0)]
KU = [(11700.0, 12200.0), (14000.0, 14500.0)]
TELEPORT = [(40.58, -74.2165)]
ELSEWHERE = [(35.0, -100.0)]


def e(fn, spectrum, sites=TELEPORT, frn='1', granted='2005-12-08', kind='Renewal'):
    d = datetime.date.fromisoformat(granted)
    return {'file': fn, 'type': kind, 'state': 'Current', 'frn': frn, 'filed': datetime.date.fromisoformat(
        '%s-%s-%s' % (fn.split('-')[2][:4], fn.split('-')[2][4:6], fn.split('-')[2][6:])), 'when': d,
        'v': {'spectrum': spectrum, 'sites': list(sites), 'eirp': 70.0, 'dish': 7.0}}


def rules(history):
    return sorted((r[1], r[2], r[4], r[5]) for r in find(history))


class SwapsTest(unittest.TestCase):
    def test_mutual_swap_adjacent_is_high_for_both(self):
        h = {'A': [e('SES-MOD-20000808-01390', C, granted='2002-04-11'), e('SES-RWL-20051102-01688', KU)],
             'B': [e('SES-MOD-20030219-00210', KU, granted='2003-06-03'), e('SES-RWL-20051102-01687', C)]}
        self.assertEqual(rules(h), [('mutual_swap', 'high', 'A', 'SES-RWL-20051102-01688'),
                                    ('mutual_swap', 'high', 'B', 'SES-RWL-20051102-01687')])
        row = [r for r in find(h) if r[4] == 'A'][0]
        self.assertTrue(row[12].startswith('3650-4200, 5850-6425 MHz'))  # expected = A's old C-band

    def test_mutual_swap_unrelated_filings_is_medium(self):
        h = {'A': [e('SES-LIC-19950101-00001', C, granted='1995-01-01'), e('SES-RWL-20051102-00100', KU)],
             'B': [e('SES-LIC-19950101-00500', KU, granted='1995-01-01'), e('SES-RWL-20060301-00900', C, granted='2006-03-01')]}
        self.assertEqual({r[:2] for r in rules(h)}, {('mutual_swap', 'medium')})

    def test_one_way_copy_adjacent_names_source(self):
        h = {'A': [e('SES-LIC-19950101-00001', C, sites=ELSEWHERE, granted='1995-01-01'), e('SES-RWL-20051102-01688', KU)],
             'B': [e('SES-RWL-20051102-01687', KU, frn='2', granted='2005-12-01')]}
        self.assertEqual(rules(h), [('one_way_copy', 'medium', 'A', 'SES-RWL-20051102-01688')])

    def test_one_way_copy_unrelated_is_low(self):
        h = {'A': [e('SES-LIC-19950101-00001', C, sites=ELSEWHERE, granted='1995-01-01'), e('SES-MOD-20051102-01688', KU)],
             'B': [e('SES-LIC-19990101-00050', KU, frn='2', granted='1999-01-01')]}
        self.assertEqual(rules(h), [('one_way_copy', 'low', 'A', 'SES-MOD-20051102-01688')])

    def test_teleport_neighbours_and_joint_moves_are_not_flagged(self):
        # A matches B, but A did not change: shared teleport band plan, nothing to report
        h = {'A': [e('SES-LIC-19950101-00001', KU, granted='1995-01-01'), e('SES-RWL-20051102-01688', KU)],
             'B': [e('SES-LIC-19990101-00050', KU, granted='1999-01-01')]}
        self.assertEqual(rules(h), [])
        # A and B moved together from the same old site: co-located move, not a copy
        h = {'A': [e('SES-LIC-19950101-00001', KU, sites=ELSEWHERE, granted='1995-01-01'), e('SES-MOD-20051102-01688', KU)],
             'B': [e('SES-LIC-19950101-00002', KU, sites=ELSEWHERE, granted='1995-01-01'), e('SES-MOD-20051102-01689', KU)]}
        self.assertEqual(rules(h), [])

    def test_same_day_chain_is_joint_move_not_swap(self):
        h = {'A': [e('SES-LIC-19950101-00001', C, granted='1995-01-01'), e('SES-MOD-20090817-01010', KU, granted='2009-10-19')],
             'B': [e('SES-MOD-20090817-01006', KU, granted='2009-10-19'), e('SES-RWL-20090930-01243', C, granted='2009-10-19')]}
        got = [r for r in find(h) if r[4] == 'B']
        self.assertEqual((got[0][1], got[0][2]), ('mutual_swap', 'low'))
        self.assertTrue(got[0][13].startswith('likely benign'))


if __name__ == '__main__':
    unittest.main()
