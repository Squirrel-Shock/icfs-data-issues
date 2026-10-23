import unittest

from sat import audit


def band(lo, hi, d='Space-to-Earth (Transmit)', label='Other', svc='FSS'):
    return {'s2_lower_frequency_limit': lo, 's2_upper_frequency_limit': hi, 's2_direction_of_transmission': d,
            's2_frequency_band_mhz': label, 's2_type_of_service': svc}


def sat(fn, s2=(), s4=(), s5=(), lon='', hemi='', life='15', orbit='Geostationary (GSO)', name='SAT-A',
        cs='S1', grant='', ftype='Modification'):
    s3 = [{'s3_orbital_longitude': lon, 's3_hemisphere': hemi}] if orbit.startswith('Geo') else []
    beams = lambda sec, bs: [{sec + '_beam_name': 'B', sec + '_lower_frequency': lo, sec + '_upper_frequency': hi}
                             for lo, hi in bs]
    return {'subsystem': 'SAT', 'file_number': fn, 'call_sign': cs, 'filing_type': ftype, 'state': 'Current',
            'last_action': 'GRA' if grant else '', 'grant_date': grant,
            's1': [{'s1_satellite_network_name': name, 's1_estimated_lifetime_of_satellite': life,
                    's1_orbit_type': orbit, 's2': list(s2), 's3': s3, 's4': beams('s4', s4), 's5': beams('s5', s5)}]}


def rules(rows, fn=None):
    return sorted(r[1] for r in rows if fn is None or r[5] == fn)


class WithinFiling(unittest.TestCase):
    def test_clean(self):
        r = sat('SAT-MOD-20200101-00001', [band('3700', '4200', label='3700 - 4200')], s5=[('3700', '4200')],
                lon='101', hemi='West')
        self.assertEqual(rules(audit([r])), [])

    def test_band_problems(self):
        rows = audit([sat('SAT-MOD-20200101-00001', [band('4200', '3700'), band('0.95', '1.05'), band('9.5', '10.5'), band('', '5'),
                                                    band('3700', '3800', d=''), band('3700', '3800', d=''),
                                                    band('2200', '2300', label='2025 - 2110')],
                          lon='101', hemi='West')])
        self.assertEqual(rules(rows), ['band_direction_invalid', 'band_direction_invalid', 'band_duplicate',
                                       'band_edge_missing', 'band_label_disagrees', 'band_lower_above_upper',
                                       'band_unit_slip'])  # 9.5-10.5 MHz is HF, legitimate
        swap = [r for r in rows if r[1] == 'band_lower_above_upper'][0]
        self.assertEqual(swap[12], '3700-4200')
        self.assertEqual([r[12] for r in rows if r[1] == 'band_unit_slip'], ['950-1050'])

    def test_beams(self):
        rows = audit([sat('SAT-MOD-20200101-00001', [band('3700', '4200'), band('5925', '6425', d='Earth-to-Space (Receive)')],
                          s4=[('3700', '4200'), ('14000', '14500')], s5=[('3800', '3900')], lon='101', hemi='West')])
        self.assertEqual(rules(rows), ['beam_direction_mismatch', 'beam_outside_bands'])

    def test_orbit_and_lifetime(self):
        rows = audit([sat('A', life='0', lon='200', hemi='East'), sat('B', life=''),
                      sat('C', life='x', orbit='geostationary_ngso')])
        self.assertEqual(rules(rows, 'A'), ['gso_longitude_out_of_range', 'lifetime_implausible'])
        self.assertEqual([r[12] for r in rows if r[1] == 'gso_longitude_out_of_range'], ['160 West'])
        self.assertEqual(rules(rows, 'B'), ['gso_longitude_missing', 'lifetime_missing'])
        self.assertEqual(rules(rows, 'C'), ['lifetime_implausible', 'orbit_type_invalid'])


class History(unittest.TestCase):
    def test_jump_and_reversion(self):
        b = [band('3700', '4200')]
        rows = audit([sat('SAT-LOA-20100101-00001', b, lon='101', hemi='West', grant='2010-06-01', ftype='Launch and Operating Authority'),
                      sat('SAT-LOI-20110101-00001', [band('20000', '21000')], lon='61', hemi='West',
                          grant='2011-06-01', ftype='Letter of Intent'),
                      sat('SAT-MOD-20120101-00001', b, lon='101', hemi='West', grant='2012-06-01')])
        mid = 'SAT-LOI-20110101-00001'
        self.assertEqual(rules(rows, mid), ['gso_location_jump', 'gso_location_reverted', 'spectrum_replaced',
                                            'spectrum_reverted'])
        # a Modification may relocate and lists only the changed part of Schedule S: nothing to flag
        self.assertEqual(rules(rows, 'SAT-MOD-20120101-00001'), [])
        amd = audit([sat('SAT-LOA-20100101-00001', b, lon='101', hemi='West', grant='2010-06-01'),
                     sat('SAT-AMD-20110101-00001', [band('20000', '21000')], lon='61', hemi='West', name='SAT-B',
                         grant='2011-06-01', ftype='Amendment')])
        self.assertEqual(rules(amd), ['network_name_changed'])  # location and spectrum not flagged in an amendment
        rev = [r for r in rows if r[1] == 'gso_location_reverted'][0]
        self.assertEqual((rev[8], rev[9], rev[10], rev[11], rev[12]),
                         ('SAT-LOA-20100101-00001', '101W', mid, '61W', '101W'))

    def test_missing_longitude_expected_from_history(self):
        rows = audit([sat('SAT-LOA-20100101-00001', [band('3700', '4200')], lon='101', hemi='West', grant='2010-06-01'),
                      sat('SAT-RPL-20120101-00001', [band('3700', '4200')], grant='2012-06-01',
                          ftype='Replacement Satellite Application (no new frequency bands)')])
        miss = [r for r in rows if r[1] == 'gso_longitude_missing'][0]
        self.assertEqual(miss[12], '101W')

    def test_name_formatting_ignored(self):
        rows = audit([sat('SAT-LOA-20100101-00001', name='Vindler 2.1, 2.2, and 2.3', lon='1', hemi='East', grant='2010-01-01'),
                      sat('SAT-MOD-20110101-00001', name='VINDLER 2.1 2.2 2.3', lon='1', hemi='East', grant='2011-01-01')])
        self.assertEqual(rules(rows), [])


if __name__ == '__main__':
    unittest.main()
