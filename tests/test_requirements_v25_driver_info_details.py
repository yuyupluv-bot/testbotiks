import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLERS = (ROOT / "bot/handlers.py").read_text("utf-8")
KEYBOARDS = (ROOT / "bot/keyboards.py").read_text("utf-8")


class DriverInfoDetailsV25Tests(unittest.TestCase):
    def test_changed_modules_parse(self):
        ast.parse(HANDLERS)
        ast.parse(KEYBOARDS)

    def test_info_message_contains_saved_details(self):
        fn = HANDLERS.split("def show_driver_info", 1)[1].split("PENDING_ORDER_STATUSES", 1)[0]
        self.assertIn("user.car_full", fn)
        self.assertIn("_payment_details_ready(user)", fn)
        self.assertIn("_payment_details_text(user)", fn)
        self.assertIn("user.rating_sum", fn)
        self.assertIn("user.rating_count", fn)
        self.assertIn("🚗 Авто:", fn)
        self.assertIn("💳 Реквизиты:", fn)
        self.assertIn("⭐ Отзывы: средняя оценка", fn)

    def test_existing_actions_remain_in_info_menu(self):
        submenu = KEYBOARDS.split("def driver_info_menu", 1)[1].split("def missed_offer_timeout_keyboard", 1)[0]
        for command in ("fake_calls", "earnings", "reviews"):
            self.assertIn(f'{{"cmd": "{command}"}}', submenu)


if __name__ == "__main__":
    unittest.main()
