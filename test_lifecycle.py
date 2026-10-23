"""python -m unittest test_lifecycle"""
import datetime
import unittest

from lifecycle import F, check, single

TODAY = datetime.date(2026, 10, 22)


def f(fn, typ, state='Closed', action='GRA', gd='', tb='', te='', ld=''):
    return F({'subsystem': fn.split('-')[0], 'call_sign': 'E1', 'file_number': fn, 'filing_type': typ,
              'state': state, 'last_action': action, 'grant_date': gd, 'term_begin': tb, 'term_end': te,
              'last_action_date': ld or gd})


def rules(*group):
    return sorted((rule, x['file_number'], conf) for rule, x, y, exp, detail, conf in check(list(group), TODAY))


LIC = f('SES-LIC-20000101-00001', 'License', gd='2000-02-01', tb='2000-02-01', te='2015-02-01')


class LifecycleTest(unittest.TestCase):
    def test_clean_chain(self):
        rwl = f('SES-RWL-20141201-00001', 'Renewal', 'Current', gd='2015-01-05', tb='2015-02-01', te='2030-02-01')
        self.assertEqual(rules(LIC, rwl), [])

    def test_older_grant_still_current_and_latest_closed(self):
        lic = f('SES-LIC-20000101-00001', 'License', 'Current', gd='2000-02-01', tb='2000-02-01', te='2015-02-01')
        rwl = f('SES-RWL-20141201-00001', 'Renewal', gd='2015-01-05', tb='2015-02-01', te='2030-02-01')
        self.assertEqual(rules(lic, rwl), [('current_superseded', lic['file_number'], 'high'),
                                           ('latest_grant_not_current', rwl['file_number'], 'medium')])

    def test_surrendered_but_mod_current(self):
        rwl = f('SES-RWL-20141201-00001', 'Renewal', action='SUR', gd='2015-01-05', tb='2015-02-01',
                te='2030-02-01', ld='2020-01-01')
        mod = f('SES-MOD-20120101-00001', 'Modification', 'Current', gd='2012-03-01', tb='2000-02-01', te='2015-02-01')
        self.assertIn(('current_after_surrender', mod['file_number'], 'high'), rules(LIC, rwl, mod))

    def test_relicensed_after_surrender_is_fine(self):
        old = f('SES-LIC-20000101-00001', 'License', action='SUR', gd='2000-02-01', te='2015-02-01', ld='2005-01-01')
        new = f('SES-LIC-20100101-00001', 'License', 'Current', gd='2010-02-01', tb='2010-02-01', te='2030-02-01')
        self.assertEqual(rules(old, new), [])

    def test_renewal_gap_and_orphan(self):
        rwl = f('SES-RWL-20180101-00001', 'Renewal', 'Current', gd='2018-02-01', tb='2018-02-01', te='2033-02-01')
        self.assertEqual(rules(LIC, rwl), [('renewal_term_gap', rwl['file_number'], 'low')])
        exact = f('SES-RWL-20110101-00001', 'Renewal', gd='2026-04-22', tb='2030-02-01', te='2045-02-01')
        self.assertIn(('renewal_term_gap', exact['file_number'], 'medium'), rules(LIC, exact))
        self.assertEqual(rules(rwl), [('orphan_renewal', rwl['file_number'], 'medium')])

    def test_duplicate_renewal_flags_both(self):
        a = f('SES-RWL-20141201-00001', 'Renewal', gd='2015-01-05', tb='2015-02-01', te='2030-02-01')
        b = f('SES-RWL-20141202-00001', 'Renewal', 'Current', gd='2015-01-06', tb='2015-02-01', te='2030-02-01')
        self.assertEqual([r[0] for r in rules(LIC, a, b)], ['duplicate_renewal'] * 2)

    def test_modification_after_expiry(self):
        mod = f('SES-MOD-20160101-00001', 'Modification', 'Current', gd='2016-03-01')
        self.assertIn(('modification_after_expiry', mod['file_number'], 'medium'), rules(LIC, mod))

    def test_closed_in_term(self):
        lic = f('SES-LIC-20200101-00001', 'License', gd='2020-02-01', tb='2020-02-01', te='2035-02-01')
        self.assertEqual(rules(lic), [('closed_in_term', lic['file_number'], 'low')])

    def test_pending_on_expired_license_ignores_sta(self):
        asg = f('SES-ASG-20140101-00001', 'Assignment', 'Pending Review', action='')
        sta = f('SES-STA-20140101-00001', 'Special Temporary Authority', 'Pending Review', action='')
        self.assertEqual(rules(LIC, asg, sta), [('pending_on_expired_license', asg['file_number'], 'medium')])

    def test_bulk_stamp_grant_date_ignored(self):
        x = f('SAT-MOD-19870101-00001', 'Modification', gd='2025-06-22', ld='1987-05-01')
        self.assertEqual(x['gd'], datetime.date(1987, 5, 1))

    def test_single_filing_rules(self):
        def one(fn, typ, state, action, gd=''):
            return [x[0] for x in single(f(fn, typ, state, action, gd=gd), TODAY)]
        self.assertEqual(one('IHF-RWL-20231109-00001', 'Renewal', 'Current', 'WTD'), ['current_but_dismissed'])
        self.assertEqual(one('IHF-RWL-20231109-00001', 'Renewal', 'Current', 'GRA'), [])
        self.assertEqual(one('ITC-214-19980520-00334', 'Renewal', 'Current', 'DSC'), ['current_with_non_grant_action'])
        self.assertEqual(one('SES-MOD-20250228-00295', 'Modification', 'Current', ''), ['current_with_non_grant_action'])
        srq = single(f('ITC-214-20000101-00001', 'Renewal', 'Current', 'SRQ'), TODAY)[0]
        self.assertEqual((srq[0], srq[3]), ('current_with_non_grant_action', 'low'))
        self.assertEqual(one('SES-MOD-20250228-00295', 'Modification', 'Current', 'SUR'), [])  # surrender rule's job
        self.assertEqual(one('ITC-ASG-20140624-00188', 'Assignment', 'Current', 'GRA', '2014-08-01'),
                         ['transaction_not_consummated'])
        self.assertEqual(one('ITC-ASG-20140624-00188', 'Assignment', 'Closed', 'CON', '2014-08-01'), [])
        self.assertEqual(one('SES-MOD-20191016-01321', 'Modification', 'Pending Review', ''), ['stale_pending'])
        self.assertEqual(one('SES-MOD-20231016-01321', 'Modification', 'Pending Review', ''), [])


if __name__ == '__main__':
    unittest.main()
