"""python -m unittest test_rules"""
import datetime
import unittest

from rules import check

TODAY = datetime.date(2026, 10, 22)


def rules_of(record, pending=()):
    base = {'subsystem': 'SES', 'file_number': 'SES-LIC-20110909-01055', 'state': 'Current',
            'date_filed': '2011-09-09 19:00:00', 'grant_date': '2011-10-01', 'term_begin': '2011-10-01',
            'term_end': '2026-10-01', 'call_sign': 'E110001', 'applicant_frn': '0001234567', 'applicant_name': 'Acme'}
    base.update(record)
    return sorted(x['rule'] for x in check(base, set(pending), today=TODAY))


class RulesTest(unittest.TestCase):
    def test_expired_current_license(self):
        self.assertEqual(rules_of({}), ['current_term_expired'])
        self.assertEqual(rules_of({}, [('SES', 'E110001')]), ['current_term_expired_renewal_pending'])
        self.assertEqual(rules_of({'term_end': '2041-10-01'}), [])

    def test_no_term_only_where_terms_exist(self):
        self.assertEqual(rules_of({'term_end': ''}), ['current_no_term'])
        self.assertEqual(rules_of({'term_end': '', 'subsystem': 'ITC', 'file_number': 'ITC-214-20110909-01055'}), [])

    def test_placeholder_dates_are_flagged_and_not_reasoned_from(self):
        got = rules_of({'date_filed': '8888-08-08 19:00:00', 'term_end': '2041-10-01'})
        self.assertEqual(got, ['placeholder_date'])  # no file-number or ordering noise from 8888
        self.assertEqual(rules_of({'grant_date': '1908-03-03', 'term_end': '2041-10-01'}), ['placeholder_date'])

    def test_ordering_and_identity(self):
        self.assertIn('term_end_before_term_begin', rules_of({'term_begin': '2027-01-01', 'term_end': '2026-12-01'}))
        self.assertIn('grant_before_filing', rules_of({'grant_date': '2011-01-01', 'term_end': '2041-10-01'}))
        self.assertIn('subsystem_prefix_mismatch', rules_of({'file_number': 'ITC-214-20110909-01055'}))
        self.assertIn('file_number_bad_date', rules_of({'file_number': 'SES-LIC-20111399-01055'}))
        self.assertIn('granted_but_pending', rules_of({'state': 'Pending Review'}))
        self.assertNotIn('granted_but_pending', rules_of({'state': 'Pending Consummation'}))
        self.assertEqual(rules_of({'file_number': 'SES-STA-INTR2025-02713', 'term_end': '2041-10-01'}), [])

    def test_row_level_splits(self):
        def one(record, rule):
            base = {'subsystem': 'SES', 'file_number': 'SES-STA-20201015-01132', 'state': 'Closed',
                    'call_sign': 'E1', 'term_end': '2041-01-01', 'applicant_name': 'Acme'}
            base.update(record)
            return [x for x in check(base, set(), today=TODAY) if x['rule'] == rule][0]
        wrap = one({'term_begin': '2020-12-09', 'term_end': '2020-01-09'}, 'term_end_before_term_begin')
        self.assertEqual((wrap['conf'], wrap['expected']), ('high', 'term_end=2021-01-09'))
        con = one({'grant_date': '2020-09-01', 'last_action_date': '2020-09-01', 'last_action': 'CON'},
                  'last_action_before_filing')
        self.assertTrue(con['detail'].startswith('likely benign'))
        stale = one({'grant_date': '2020-01-01', 'last_action_date': '2020-11-01', 'last_action': 'GRA'},
                    'grant_before_filing')
        self.assertEqual((stale['conf'], stale['expected']), ('medium', '2020-11-01'))
        ph = one({'date_filed': '8888-08-08 19:00:00'}, 'placeholder_date')
        self.assertEqual(ph['expected'], '2020-10-15')
        modern = one({'date_filed': '2021-02-01 14:51:59'}, 'filed_date_differs_from_file_number')
        self.assertTrue(modern['detail'].startswith('likely benign'))
        legacy = one({'date_filed': '2021-02-01 19:00:00'}, 'filed_date_differs_from_file_number')
        self.assertEqual(legacy['conf'], 'medium')

    def test_missing_dates_frn_and_disposition(self):
        def one(record, rule):
            base = {'subsystem': 'SES', 'file_number': 'SES-MOD-20191107-01463', 'state': 'Current',
                    'date_filed': '2019-11-07 10:00:00', 'call_sign': 'E1', 'term_end': '2041-01-01',
                    'applicant_frn': '0001234567', 'applicant_name': 'Acme', 'last_action': 'GRA', 'grant_date': '2020-01-02',
                    'last_action_date': '2020-01-02'}
            base.update(record)
            got = [x for x in check(base, set(), today=TODAY) if x['rule'] == rule]
            return got[0] if got else None
        self.assertIsNone(one({}, 'date_filed_missing'))
        self.assertEqual(one({'date_filed': ''}, 'date_filed_missing')['expected'], '2019-11-07')
        g = one({'grant_date': ''}, 'granted_no_grant_date')
        self.assertEqual((g['conf'], g['expected']), ('medium', '2020-01-02'))
        recent = one({'file_number': 'SES-MOD-20261015-02850', 'date_filed': '2026-10-15 10:00:00', 'grant_date': '',
                      'last_action_date': '', 'state': 'Pending Review'}, 'granted_no_grant_date')
        self.assertEqual(recent['conf'], 'low')
        self.assertIn('API lag', recent['detail'])
        self.assertEqual(one({'last_action_date': ''}, 'last_action_without_date')['expected'], '2020-01-02')
        self.assertIsNone(one({}, 'frn_missing_on_current'))
        self.assertEqual(one({'applicant_frn': ''}, 'frn_missing_on_current')['conf'], 'medium')
        self.assertEqual(one({'applicant_frn': '12345'}, 'frn_missing_on_current')['conf'], 'medium')
        old = one({'applicant_frn': '', 'file_number': 'SES-LIC-19960514-01190', 'date_filed': '1996-05-14',
                   'grant_date': '1996-08-01', 'last_action_date': '1996-08-01'}, 'frn_missing_on_current')
        self.assertTrue(old['detail'].startswith('likely benign'))
        fcn = one({'subsystem': 'FCN', 'file_number': 'FCN-NEW-20090301-00001', 'date_filed': '2009-03-01',
                   'state': 'Closed', 'last_action': '', 'grant_date': '', 'last_action_date': ''},
                  'closed_without_disposition')
        self.assertEqual(fcn['conf'], 'low')
        scl = one({'subsystem': 'SCL', 'file_number': 'SCL-LIC-20000301-00001', 'date_filed': '2000-03-01',
                   'state': 'Closed', 'last_action': '', 'grant_date': '2000-05-01'}, 'closed_without_disposition')
        self.assertEqual((scl['conf'], scl['expected']), ('medium', 'GRA (grant_date 2000-05-01)'))

    def test_expected_frn_from_same_call_sign(self):
        from rules import Context
        a = {'subsystem': 'SES', 'file_number': 'SES-REG-20181127-06788', 'state': 'Current', 'call_sign': 'E1',
             'applicant_frn': '', 'date_filed': '2018-11-27', 'last_action': 'GRA', 'grant_date': '2019-01-01',
             'last_action_date': '2019-01-01', 'term_end': '2034-01-01'}
        b = dict(a, file_number='SES-ASG-20150101-00001', applicant_frn='0009876543', state='Closed')
        c = dict(a, file_number='SES-ASG-20100101-00001', applicant_frn='0001111111', state='Closed')
        x = [x for x in check(a, Context([a, b, c]), today=TODAY) if x['rule'] == 'frn_missing_on_current'][0]
        self.assertEqual((x['expected'], x['before_file']), ('0009876543', 'SES-ASG-20150101-00001'))

    def test_pn_dates_name_type_code_and_duplicates(self):
        from rules import Context
        base = {'subsystem': 'SES', 'file_number': 'SES-RWL-20261012-02759', 'state': 'Pending Review',
                'date_filed': '2026-10-12 10:00:00', 'call_sign': 'E1', 'filing_type': 'Renewal',
                'applicant_frn': '0001234567', 'applicant_name': 'Acme'}

        def one(record, rule, ctx=None):
            r = dict(base, **record)
            got = [x for x in check(r, ctx or set(), today=TODAY) if x['rule'] == rule]
            return got[0] if got else None
        self.assertEqual(one({'accepted_pn_date': '2012-01-11'}, 'accepted_pn_before_filing')['conf'], 'high')
        self.assertEqual(one({'accepted_pn_date': '2026-09-01'}, 'accepted_pn_before_filing')['conf'], 'medium')
        self.assertIsNone(one({'accepted_pn_date': '2026-10-11'}, 'accepted_pn_before_filing'))
        g = {'state': 'Current', 'last_action': 'GRA', 'grant_date': '2026-05-19', 'last_action_date': '2026-05-19'}
        self.assertEqual(one(dict(g, action_pn_date='2024-07-10'), 'action_pn_before_grant')['conf'], 'medium')
        self.assertIsNone(one(dict(g, action_pn_date='2026-05-15'), 'action_pn_before_grant'))
        itc = one(dict(g, action_pn_date='2026-05-05', subsystem='ITC', file_number='ITC-214-20260101-00001'),
                  'action_pn_before_grant')
        self.assertTrue(itc['detail'].startswith('likely benign'))
        self.assertEqual(one({'applicant_name': ' '}, 'applicant_name_missing')['conf'], 'low')  # 10 days old
        prev = dict(base, file_number='SES-LIC-20110101-00001', filing_type='License', applicant_name='Acme Corp')
        nxt = dict(base, file_number='SES-MOD-20180101-00001', filing_type='Modification', applicant_name='ACME CORP')
        old = dict(base, file_number='SES-MOD-20150101-00001', applicant_name='', date_filed='2015-01-01')
        ctx = Context([prev, nxt, old])
        x = [x for x in check(old, ctx, today=TODAY) if x['rule'] == 'applicant_name_missing'][0]
        self.assertEqual((x['conf'], x['expected']), ('medium', 'Acme Corp'))
        lics = [dict(base, file_number='SES-LIC-201101%02d-%05d' % (i, i), filing_type='License', call_sign='E%d' % i)
                for i in range(1, 12)] + [dict(base, file_number='SES-LIC-20000210-00207', call_sign='E0',
                                               filing_type='Registration of a receive-only earth station')]
        ctx = Context(lics)
        bad = [x for x in check(lics[-1], ctx, today=TODAY) if x['rule'] == 'type_code_mismatch']
        self.assertEqual(bad[0]['expected'], 'License')
        self.assertEqual([x for x in check(lics[0], ctx, today=TODAY) if x['rule'] == 'type_code_mismatch'], [])
        a = dict(base, file_number='SES-T/C-20040615-00906')
        b = dict(base, file_number='SES-T/C-20040615-00907')
        stub = {'subsystem': 'SAT', 'file_number': 'SAT-LIC-19850101-00001', 'call_sign': ''}
        ctx = Context([a, b, stub, dict(stub, file_number='SAT-LIC-19850101-00002')])
        da = [x for x in check(a, ctx, today=TODAY) if x['rule'] == 'duplicate_filing'][0]
        db = [x for x in check(b, ctx, today=TODAY) if x['rule'] == 'duplicate_filing'][0]
        self.assertEqual((da['before_file'], db['before_file']), (b['file_number'], a['file_number']))
        self.assertEqual([x for x in check(stub, ctx, today=TODAY) if x['rule'] == 'duplicate_filing'], [])


if __name__ == '__main__':
    unittest.main()
