import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS = (ROOT / "bot/keyboards.py").read_text("utf-8")
HANDLERS = (ROOT / "bot/handlers.py").read_text("utf-8")


class DriverInfoMenuV24Tests(unittest.TestCase):
    def test_changed_modules_parse(self):
        ast.parse(KEYBOARDS)
        ast.parse(HANDLERS)

    def test_initial_menu_has_info_and_price_before_pending(self):
        offline = KEYBOARDS.split("else:\n        rows = [", 1)[1].split("return keyboard(rows)", 1)[0]
        self.assertIn('{"cmd": "driver_info"}', offline)
        self.assertNotIn('_btn("🚫 Ложные вызовы", WHITE, {"cmd": "fake_calls"})', offline)
        self.assertIn('_btn("🏷 Прайс", WHITE, {"cmd": "price"}),\n            _btn("⏳ Ожидающие", BLUE', offline)

    def test_online_menu_has_no_pending_button(self):
        online = KEYBOARDS.split("if on_line:", 1)[1].split("else:\n        rows = [", 1)[0]
        self.assertNotIn('"pending_orders"', online)

    def test_info_submenu_contains_requested_actions_and_back(self):
        submenu = KEYBOARDS.split("def driver_info_menu", 1)[1].split("def missed_offer_timeout_keyboard", 1)[0]
        for command in ("fake_calls", "earnings", "reviews", "start"):
            self.assertIn(f'{{"cmd": "{command}"}}', submenu)

    def test_info_command_is_routed(self):
        driver = HANDLERS.split("def handle_driver", 1)[1].split("# State-driven text input", 1)[0]
        self.assertIn('if cmd == "driver_info":', driver)
        self.assertIn("return show_driver_info(session, user)", driver)


if __name__ == "__main__":
    unittest.main()
