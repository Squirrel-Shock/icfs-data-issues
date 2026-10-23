"""python -m unittest test_freqs"""
import datetime
import unittest

from freqs import bands_of, compare, emission_problem, normalize_emission, summarize


def filing(bands, ftype='License', **kw):
    r = {'file_number': 'SES-LIC-20200101-00001', 'filing_type': ftype, 'last_action': 'GRA', 'state': 'Current',
         'schedule_b': [{'site': [{'site_site_id': '1', 'esaf': [{'esaf_antenna_id': 'A', 'esaf_diameter': '9',
                                                                  'ofb': bands}]}]}]}
    r.update(kw)
    return r


def band(lo, hi, mode='Space-to-Earth', ems=(), gain=None, gfreq=None):
    return {'ofb_frequency_lower': str(lo), 'ofb_frequency_upper': str(hi), 'ofb_trans_mode': mode,
            'ofb_gain': gain, 'ofb_gain_frequency': gfreq,
            'ofbp': [{'ofbp_emission': e, 'ofbp_eirp': '60'} for e in ems]}


def found(r):
    out = []
    bands_of(r, lambda rule, where, value='', expected='', conf=None, benign='', note='':
             out.append((rule, value, expected, 'benign' if benign else conf)))
    return out


class EmissionTest(unittest.TestCase):
    def test_classes(self):
        self.assertIsNone(emission_problem('168KG7DCT'))           # optional 4th/5th characters are valid
        self.assertEqual(emission_problem('36000F9'), 'emission_legacy_format')
        self.assertEqual(emission_problem('1250G7W'), 'emission_designator_malformed')  # unit letter missing
        self.assertEqual(emission_problem('36MOF3F'), 'emission_letter_o_for_zero')
        self.assertEqual(emission_problem('180kF1D'), 'emission_lowercase')
        self.assertEqual(emission_problem('45K0G7W -'), 'emission_trailing_punctuation')

    def test_expected(self):
        self.assertEqual(normalize_emission('1250G7W'), '1M25G7W')
        self.assertEqual(normalize_emission('36MOF3F'), '36M0F3F')
        self.assertEqual(normalize_emission('2M000G7W'), '2M00G7W')
        self.assertEqual(normalize_emission(')\t18M0D7W'), '18M0D7W')
        self.assertEqual(normalize_emission('NON'), 'N0N')
        self.assertEqual(normalize_emission('2K4OG1D'), '2K40G1D')  # O in the 4th bandwidth place
        self.assertEqual(normalize_emission('1HOONON'), '1H00N0N')
        self.assertEqual(normalize_emission('3m7og1f'), '3M70G1F')

    def test_antenna_without_band_never_granted_is_benign(self):
        bare = {'schedule_b': [{'site': [{'site_site_id': '6', 'esaf': [{'esaf_antenna_id': '6'}]}]}]}
        def conf(**kw):
            r = dict(filing([]), **bare)
            r.update(kw)
            return [g[3] for g in found(r) if g[0] == 'antenna_without_band']
        self.assertEqual(conf(state='Closed', last_action='WTD'), ['benign'])
        self.assertEqual(conf(state='Closed', last_action='', file_number='SES-LIC-19950101-00001'), ['low'])
        self.assertEqual(conf(), [None])


class WithinTest(unittest.TestCase):
    def test_bands(self):
        got = found(filing([band(27500, 2800, 'Earth-to-Space'), band(3.7, 4.2)]))
        self.assertIn(('band_lower_above_upper', '27500-2800', '27500-28000 (truncated upper edge? check)', None), got)
        self.assertIn(('band_out_of_range_units', '3.7-4.2', '3700-4200', None), got)
        self.assertEqual([g[0] for g in got].count('direction_against_band_plan'), 0)  # inverted band: no direction noise

    def test_wider_than_band_merges_touching_segments(self):
        got = found(filing([band(13750, 13770, 'Earth-to-Space', ['36M0G7W']), band(13770, 14000, 'Earth-to-Space', ['36M0G7W'])]))
        self.assertEqual([g[3] for g in got if g[0] == 'emission_wider_than_band'], ['benign'])
        got = found(filing([band(3700, 3710, ems=['3G75G7W'])]))
        self.assertEqual([g[3] for g in got if g[0] == 'emission_wider_than_band'], ['high'])

    def test_gain_at_its_own_frequency(self):
        # 61 dBi quoted at 12 GHz on an 11 m dish is right; the L-band row must not be flagged
        r = filing([band(1646.5, 1660.5, ems=['1M00G7W'], gain='61', gfreq='12')], esaf_diameter='11')
        self.assertFalse([g for g in found(r) if g[0] == 'gain_vs_dish_size'])


class HistoryTest(unittest.TestCase):
    def test_cband_transition_is_benign(self):
        prev = summarize(bands_of(filing([band(3700, 4200, ems=['36M0G7W'])])))
        cur = summarize(bands_of(filing([band(4000, 4200, ems=['36M0G7W'])])))
        out = []
        compare(prev, cur, lambda rule, detail, **k: out.append((rule, bool(k.get('benign')))),
                {'renewal_date': datetime.date(2023, 1, 1), 'ses_type': '', 'amended': ''})
        self.assertEqual(sorted(out), [('bands_added', True), ('bands_dropped', True)])

    def test_spelling_is_not_an_emission_change(self):
        prev = summarize(bands_of(filing([band(5925, 6425, 'Earth-to-Space', ['1M58G7D'])])))
        cur = summarize(bands_of(filing([band(5925, 6425, 'Earth-to-Space', ['1M58G7D-'])])))
        out = []
        compare(prev, cur, lambda rule, detail, **k: out.append(rule),
                {'renewal_date': datetime.date(2023, 1, 1), 'ses_type': '', 'amended': ''})
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
