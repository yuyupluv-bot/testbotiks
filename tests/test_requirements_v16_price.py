import ast
import math
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot/handlers.py"


def load_price_helpers():
    tree = ast.parse(HANDLERS.read_text("utf-8"))
    wanted = {"PRICE_MIN", "PRICE_MAX"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
            "_parse_price", "_price_in_range", "_price_range_message"
        }:
            nodes.append(node)
    namespace = {"re": re, "math": math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(HANDLERS), "exec"), namespace)
    return namespace


class PriceInputV16Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_price_helpers()

    def test_common_vk_amount_formats(self):
        parse = self.helpers["_parse_price"]
        for raw in ("170", "170 ₽", "170р", "170 руб.", " 170 ", "1\u00a0700"):
            self.assertEqual(1700.0 if "1" in raw and "700" in raw else 170.0, parse(raw), raw)

    def test_bounds_are_100_to_50000(self):
        valid = self.helpers["_price_in_range"]
        self.assertFalse(valid(99.99))
        self.assertTrue(valid(100))
        self.assertTrue(valid(170))
        self.assertTrue(valid(50_000))
        self.assertFalse(valid(50_000.01))
        self.assertIn("50 000", self.helpers["_price_range_message"]())

    def test_delivery_reuses_shared_validation(self):
        delivery = (ROOT / "bot/delivery_service.py").read_text("utf-8")
        self.assertIn("_parse_price, _price_in_range, _price_range_message", delivery)
        self.assertNotIn("price <= 30000", delivery)


if __name__ == "__main__":
    unittest.main()
