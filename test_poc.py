"""python -m unittest test_poc"""
import unittest

from poc import check, match, parse_orbit, sat_index


def sat(fn, cs, name, action='GRA', grant='2010-01-01', lon=None, hemi='West', ftype='Modification', **kw):
    s1 = [{'s1_satellite_network_name': name, 's3': [{'s3_orbital_longitude': lon, 's3_hemisphere': hemi}] if lon else []}]
    r = {'file_number': fn, 'call_sign': cs, 'sat_satellite_name': name, 'last_action': action, 'grant_date': grant,
         'filing_type': ftype, 'state': 'Closed', 's1': s1}
    r.update(kw)
    return r


IDX = sat_index([
    sat('SAT-MOD-20100101-00001', 'S2400', 'Galaxy 18', lon='123'),
    sat('SAT-MOD-20200101-00002', 'S2400', 'Galaxy 18', grant='2020-01-01', lon='137'),
    sat('SAT-MOD-20050101-00003', 'S2500', 'Oldbird', grant='2005-01-01', term_end='2008-01-01'),
    sat('SAT-MOD-20050101-00004', 'S2600', 'Gonebird', action='SUR', grant='2005-01-01', last_action_date='2006-01-01'),
])


def es(pocs, grant='2015-06-01', state='Current', modes=('Earth-to-Space',)):
    ofb = [{'ofb_trans_mode': m} for m in modes]
    return {'file_number': 'SES-LIC-%d0101-00001' % (int(grant[:4]) - 1), 'last_action': 'GRA', 'grant_date': grant, 'state': state,
            'schedule_b': [{'site': [{'esaf': [{'ofb': ofb}], 'poc': pocs}]}]}


def poc(name, loc, common=''):
    return {'poc_satellite': name, 'poc_itu_name': name, 'poc_common_name': common, 'poc_orbit_location': loc, 'poc_site': '1'}


def rules(r):
    return sorted((x[0], x[1]) for x in check(r, IDX))


class ParseTest(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(parse_orbit('116.8 W.L.'), ('ok', -116.8))
        self.assertEqual(parse_orbit('111.1  W.L'), ('ok', -111.1))
        self.assertEqual(parse_orbit('101 DEG. WL'), ('ok', -101.0))
        self.assertEqual(parse_orbit('99.9� E. L.'), ('ok', 99.9))
        self.assertEqual(parse_orbit('304.5 E.L.'), ('ok', -55.5))  # 0-360 east is the FCC convention
        self.assertEqual(parse_orbit('GSO-85 W.L.'), ('ok', -85.0))

    def test_kinds(self):
        self.assertEqual(parse_orbit('')[0], 'blank')
        self.assertEqual(parse_orbit('GSO')[0], 'category')
        self.assertEqual(parse_orbit('NGSO')[0], 'category')
        self.assertEqual(parse_orbit('101E /119W')[0], 'list')
        self.assertEqual(parse_orbit('99.99')[0], 'no_hemisphere')
        self.assertEqual(parse_orbit('191.0 W.L.'), ('ok', 169.0))  # legacy 0-360 picklist notation
        self.assertEqual(parse_orbit('370 E.L.')[0], 'out_of_range')
        for cat in ('AOR', '470 KM', 'ISAT List SYSTEM'):
            self.assertEqual(parse_orbit(cat)[0], 'category', cat)
        for bad in ('ALASKA', 'TDRS-171', 'KS3/'):
            self.assertEqual(parse_orbit(bad)[0], 'bad', bad)


class MatchTest(unittest.TestCase):
    def test_order(self):
        self.assertEqual(match(poc('GALAXY 18 (S2400) moved', ''), IDX), ('S2400', 'call_sign'))
        self.assertEqual(match(poc('GALAXY 18', ''), IDX), ('S2400', 'name'))
        self.assertEqual(match(poc('Oldbird (Retired)', '', 'x'), IDX)[1], 'fuzzy')
        self.assertEqual(match(poc('Permitted Space Station List', 'GSO'), IDX), (None, 'permitted_list'))
        self.assertEqual(match(poc('Nobody', ''), IDX), (None, 'unmatched'))


class CheckTest(unittest.TestCase):
    def test_location_as_of_grant_date(self):
        self.assertEqual(rules(es([poc('GALAXY 18 (S2400)', '123 W.L.')])), [])
        self.assertEqual(rules(es([poc('GALAXY 18 (S2400)', '123 W.L.')], grant='2021-01-01')),
                         [('poc_orbit_mismatch', 'low')])  # the satellite held 123 W earlier: it moved
        self.assertEqual(rules(es([poc('GALAXY 18', '123 E.L.')])), [('poc_orbit_hemisphere_flip', 'medium')])

    def test_format_rules_carry_expected(self):
        got = list(check(es([poc('GALAXY 18 (S2400)', '123')]), IDX))
        self.assertEqual((got[0][0], got[0][5]), ('poc_orbit_no_hemisphere', '123 W.L.'))
        got = list(check(es([poc('EES W.L.', '61.5 DEG', 'ECHO-3')]), IDX))
        self.assertEqual((got[0][0], got[0][1], got[0][5]), ('poc_orbit_no_hemisphere', 'high', '61.5 W.L.'))
        self.assertEqual(rules(es([poc('Unknown', '')])), [('poc_orbit_blank', 'medium')])
        self.assertEqual(rules(es([poc('Unknown', '')], state='Closed')), [('poc_orbit_blank', 'low')])
        self.assertEqual(rules(es([poc('Permitted Space Station List', '')])), [])

    def test_empty_poc_only_for_granted_transmitters(self):
        self.assertEqual(rules(es([])), [('poc_list_empty', 'high')])
        self.assertEqual(rules(es([], modes=('Space-to-Earth',))), [])

    def test_satellite_status(self):
        self.assertEqual(rules(es([poc('Oldbird', 'NGSO')])), [('poc_sat_expired', 'medium')])
        self.assertEqual(rules(es([poc('Oldbird', 'NGSO')], grant='2007-01-01')), [])
        self.assertEqual(rules(es([poc('Gonebird (Deorbited)', 'NGSO', 'S2600')])),
                         [('poc_names_retired_sat', 'medium'), ('poc_sat_surrendered', 'low')])
        got = [x for x in check(es([poc('Gonebird (Deorbited)', 'NGSO', 'S2600')]), IDX) if x[0] == 'poc_sat_surrendered']
        self.assertTrue(got[0][6].startswith('likely benign'))
        self.assertEqual(rules(es([poc('Gonebird (Deorbited)', 'NGSO', 'S2600')], state='Closed')),
                         [('poc_sat_surrendered', 'low')])

    def test_duplicates_collapse(self):
        from poc import scan
        r = es([poc('Unknown', ''), poc('Unknown', '')])
        self.assertEqual(len(list(scan([r], IDX))), 1)


if __name__ == '__main__':
    unittest.main()
