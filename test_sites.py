"""python -m unittest test_sites"""
import unittest

from sites import (call_sign_problem, ident_row, identity_steps, multi_subsystem_suspects, norm_name,
                   shared_placeholder_rows, site_problems)


def site(lat='39 6 10 N', lon='76 59 0 W', **kw):
    la, lo = lat.split(), lon.split()
    s = {'site_site_id': '1', 'site_site_city': 'SILVER SPRING', 'site_area_of_operation_code': '',
         'site_lat_deg': la[0], 'site_lat_min': la[1], 'site_lat_sec': la[2], 'site_lat_hemi': la[3] if len(la) > 3 else '',
         'site_long_deg': lo[0], 'site_long_min': lo[1], 'site_long_sec': lo[2], 'site_long_hemi': lo[3] if len(lo) > 3 else ''}
    s.update(kw)
    return s


def rules(s, state='MD', sibling=None):
    return [(p[0], p[1]) for p in site_problems(s, state, sibling)]


class SitesTest(unittest.TestCase):
    def test_good_site_is_clean(self):
        self.assertEqual(rules(site()), [])

    def test_missing_zero_and_mobile(self):
        blank = {k: '' for k in site() if k.startswith(('site_lat', 'site_long'))}
        self.assertEqual(rules(site(**blank))[0][0], 'coords_missing')
        self.assertEqual(rules(site('0 0 0 N', '0 0 0 W'))[0][0], 'coords_zero')
        self.assertEqual(rules(site('0 0 0 N', '0 0 0 W', site_site_city='CONUS'))[0], ('coords_absent_mobile_site', 'low'))
        got = site_problems(site('0 0 0 N', '0 0 0 W'), 'MD', ('SES-LIC-1', 39.1, -77.0))[0]
        self.assertIn('(39.100000, -77.000000)', got[3])  # sibling coordinate offered as expected

    def test_hemisphere_errors(self):
        got = site_problems(site(lon='76 59 0 E'), 'MD', None)
        self.assertEqual((got[0][0], got[0][1]), ('longitude_hemisphere', 'high'))
        self.assertIn('-76.983333', got[0][3])
        self.assertEqual(rules(site(lat='39 6 10 S')), [('latitude_hemisphere', 'high')])
        self.assertEqual(rules(site('1 1 1 N', '2 2 2 E')), [('longitude_hemisphere', 'medium')])
        self.assertEqual(rules(site('14 16 44 N', '170 41 21 W'), 'AS'), [('latitude_hemisphere', 'high')])
        self.assertEqual(rules(site('13 28 0 N', '144 45 0 E'), 'GU'), [])       # east is right for Guam
        self.assertEqual(rules(site('52 43 0 N', '174 6 0 E'), 'AK'), [])        # Aleutians across 180
        self.assertEqual(rules(site('35 46 22', '80 10 50 W'), 'NC'),
                         [('coords_hemisphere_missing', 'high')])

    def test_ranges_and_state_box(self):
        got = site_problems(site(lat='39 6 60 N'), 'MD', None)
        self.assertEqual(got[0][0], 'coords_minutes_seconds_60')
        self.assertIn('39 07 00.0 N', got[0][3])
        self.assertEqual(rules(site(lon='190 0 0 W')), [('coords_out_of_range', 'high')])
        self.assertEqual(rules(site('32 25 55 N', '99 46 52 W'), 'MI'), [('coords_outside_state', 'high')])

    def test_call_signs(self):
        self.assertIsNone(call_sign_problem('SES', 'E110001'))
        self.assertIsNone(call_sign_problem('SES', 'KA288'))
        self.assertEqual(call_sign_problem('SES', 'E250021 )')[:3], ('call_sign_stray_characters', 'high', 'E250021'))
        self.assertEqual(call_sign_problem('SES', 'EO30306')[2], 'E030306')
        self.assertEqual(call_sign_problem('SES', 'NEW0565')[0], 'call_sign_placeholder')
        self.assertEqual(call_sign_problem('SAT', 'E140097')[0], 'call_sign_foreign_format')
        self.assertEqual(call_sign_problem('SES', 'E180139A')[2], '')  # companion call sign, not a typo
        self.assertEqual(call_sign_problem('SAT', '2382', {'S2382'})[2], 'S2382')
        self.assertEqual(call_sign_problem('SAT', '(AMSC-1)', {'AMSC-1'})[:3], ('call_sign_stray_characters', 'high', 'AMSC-1'))
        self.assertEqual(rules(site('19 17 0 N', '166 38 0 E', site_site_city='Wake Island'), 'HI'), [])
        self.assertEqual(rules(site('27 30 0 N', '90 30 0 W', site_site_city='GREEN CANYON 249'), 'LA'),
                         [('coords_outside_state', 'low')])
        self.assertEqual(rules(site('0 0 0 N', '0 0 0 W', site_site_city='MOBILE'), 'AL')[0][0], 'coords_zero')
        self.assertEqual(multi_subsystem_suspects('WB36', {'SES': 126, 'SAT': 1})[0], {'SAT'})
        self.assertEqual(multi_subsystem_suspects('NEW', {'IHF': 3, 'SAT': 2})[0], {'SAT'})

    def test_identity(self):
        self.assertEqual(norm_name('DISH Network L.L.C.'), norm_name('Dish Network, LLC'))
        self.assertEqual(norm_name('ALASCOM, INC. C/O AT&T CORP.'), norm_name('Alascom Inc'))
        h = [{'fn': 'A', 'type': 'License', 'frn': '0001', 'name': 'Acme Inc'},
             {'fn': 'B', 'type': 'Renewal', 'frn': '0002', 'name': 'Other LLC'},
             {'fn': 'C', 'type': 'Assignment', 'frn': '0003', 'name': 'Third LLC'},
             {'fn': 'D', 'type': 'Renewal', 'frn': '0003', 'name': 'Third Co'},
             {'fn': 'E', 'type': 'Modification', 'frn': '0004', 'name': 'Third Company'},
             {'fn': 'F', 'type': 'Modification', 'frn': '0004', 'name': 'Fourth'}]
        got = [(rule, b['fn'], c['fn']) for rule, _, b, c in identity_steps(h)]
        self.assertEqual(got, [('frn_and_name_changed', 'A', 'B'), ('frn_changed', 'D', 'E'), ('name_changed', 'E', 'F')])

    def test_ident_row_values_follow_their_files(self):
        early = {'fn': 'IHF-RWL-20050310-00003', 'cs': 'KJES', 'type': 'Renewal', 'state': 'Closed',
                 'frn': '0001545607', 'name': 'Family Stations, Inc.'}
        bad = {'fn': 'IHF-LIC-19881012-00002', 'cs': 'KJES', 'type': 'License', 'state': 'Closed',
               'frn': '0025324443', 'name': 'Family Stations, Inc.'}
        r = dict(zip(['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type',
                      'state', 'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail'],
                     ident_row('frn_changed', 'medium', 'IHF', bad, early, 'x')))
        self.assertEqual((r['file_number'], r['after_file'], r['before_file']), (bad['fn'], bad['fn'], early['fn']))
        self.assertIn(bad['frn'], r['after_value'])
        self.assertIn(early['frn'], r['before_value'])

    def test_shared_placeholder_frn(self):
        fs = [{'fn': 'IHF-%d' % i, 'cs': 'K%d' % i, 'sub': 'IHF', 'type': 'License', 'state': 'Current',
               'frn': '0025324443', 'name': 'Licensee %s' % chr(65 + i)} for i in range(10)]
        fs.append({'fn': 'IHF-OK', 'cs': 'K0', 'sub': 'IHF', 'type': 'Renewal', 'state': 'Closed',
                   'frn': '0001112223', 'name': 'Licensee A'})
        got = shared_placeholder_rows(fs)
        self.assertEqual(len(got), 10)  # every carrier of the placeholder, not the real-FRN filing
        self.assertEqual({r[12] for r in got if r[5] == 'IHF-0'}, {'0001112223'})
        self.assertEqual({r[12] for r in got if r[5] == 'IHF-1'}, {''})
        self.assertEqual(shared_placeholder_rows(fs[:9] + fs[10:]), [])


if __name__ == '__main__':
    unittest.main()
