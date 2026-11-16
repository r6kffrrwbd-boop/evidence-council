import unittest

from shop import cart_total


class CartTotalTests(unittest.TestCase):
    def test_single_line(self):
        self.assertEqual(cart_total([("mug", 2)])["total"], 2400)

    def test_two_lines(self):
        self.assertEqual(cart_total([("mug", 1), ("tee", 1)])["total"], 3700)

    def test_zero_quantity_rejected(self):
        with self.assertRaises(ValueError):
            cart_total([("mug", 0)])

    def test_unknown_product_rejected(self):
        with self.assertRaises(KeyError):
            cart_total([("unicorn", 1)])


if __name__ == "__main__":
    unittest.main()
