import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS = (ROOT / "bot/keyboards.py").read_text("utf-8")
HANDLERS = (ROOT / "bot/handlers.py").read_text("utf-8")


class PendingOrdersV23Tests(unittest.TestCase):
    def test_all_changed_python_parses(self):
        ast.parse(KEYBOARDS)
        ast.parse(HANDLERS)

    def test_offline_menu_layout(self):
        menu = KEYBOARDS.split("def driver_menu", 1)[1].split("def missed_offer_timeout_keyboard", 1)[0]
        self.assertIn('_btn("✅ Выбрать линию", GREEN, {"cmd": "choose_line"}),\n                _btn("👀 Кто на линии"', menu)
        self.assertIn('_btn("🏷 Прайс", WHITE, {"cmd": "price"}),\n            _btn("⏳ Ожидающие", BLUE, {"cmd": "pending_orders"})', menu)

    def test_away_and_post_ride_buttons(self):
        away = KEYBOARDS.split("def driver_away_menu", 1)[1].split("def booking_only_cancel_keyboard", 1)[0]
        self.assertIn('_btn("📋 Очередь", WHITE, {"cmd": "queue"}),\n            _btn("⏳ Ожидающие"', away)
        post = KEYBOARDS.split("def post_ride_line_keyboard", 1)[1].split("def pending_orders_keyboard", 1)[0]
        self.assertIn('{"cmd": "driver_away"}', post)

    def test_pending_list_always_has_main_menu_exit(self):
        keyboard = KEYBOARDS.split("def pending_orders_keyboard", 1)[1].split("def order_offer_keyboard", 1)[0]
        self.assertIn('rows.append([_btn("⬅️ Вернуться в главное меню", WHITE, {"cmd": "start"})])', keyboard)

    def test_only_unassigned_unoffered_orders_are_listed(self):
        query = HANDLERS.split("def _pending_orders_query", 1)[1].split("def show_pending_orders", 1)[0]
        self.assertIn("Order.driver_id.is_(None)", query)
        self.assertIn("Order.parallel_driver_id.is_(None)", query)
        self.assertIn("Order.offered_driver_id.is_(None)", query)

    def test_pending_take_uses_normal_assignment_flow(self):
        fn = HANDLERS.split("def driver_take_pending", 1)[1].split("def _commission_for_order", 1)[0]
        self.assertIn(".with_for_update()", fn)
        self.assertIn('order.status = "assigned"', fn)
        self.assertIn("queue_service.mark_assigned", fn)
        self.assertIn("delivery_service.request_price", fn)
        self.assertIn("_show_eta_menu", fn)


if __name__ == "__main__":
    unittest.main()
