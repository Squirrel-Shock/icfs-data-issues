"""python -m unittest test_walk"""
import unittest

from walk import dish_expected, dish_fault, jumps, outside_state


def v(sites, spectrum=((3700, 4200),), eirp=60.0, dish=7.0):
    return {'sites': list(sites), 'spectrum': [list(x) for x in spectrum], 'eirp': eirp, 'dish': dish}


class WalkTest(unittest.TestCase):
    def test_jumps(self):
        a, b = v([(38.0, -77.0)]), v([(39.0, -77.0)], spectrum=((11700, 12200),), eirp=80.0, dish=0.7)
        self.assertEqual(sorted(jumps(a, b)), ['dish_jump', 'eirp_jump', 'relocated', 'spectrum_replaced'])

    def test_dish_decimal_slip(self):
        bad = {'d': 75.0, 'gain': 36.0, 'ghz': 12.0, 'major': None, 'model': 'VISIOSAT'}
        good = {'d': 0.75, 'gain': 36.0, 'ghz': 12.0, 'major': None, 'model': 'VISIOSAT'}
        self.assertTrue(dish_fault(bad))
        self.assertEqual(dish_fault(good), '')
        self.assertEqual(dish_expected(bad), '0.75 m')

    def test_state_attribution(self):
        self.assertTrue(outside_state({'state': 'CA', 'pt': (38.1, -22.2)}))
        self.assertFalse(outside_state({'state': 'CA', 'pt': (38.1, -122.2)}))
        self.assertIsNone(outside_state({'state': '', 'pt': (38.1, -122.2)}))


if __name__ == '__main__':
    unittest.main()
