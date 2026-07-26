import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseV15Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_models = types.ModuleType("common.models")
        fake_models.ROLE_ADMIN = "admin"
        fake_models.ROLE_DISPATCHER = "dispatcher"
        fake_models.ROLE_DRIVER = "driver"
        fake_models.ROLE_PASSENGER = "passenger"
        sys.modules.setdefault("common.models", fake_models)
        from bot import keyboards
        cls.kb = keyboards

    @staticmethod
    def rows(raw):
        return json.loads(raw)["buttons"]

    @staticmethod
    def labels(row):
        return [button["action"]["label"] for button in row]

    def test_price_layout_normal_and_active(self):
        children = [("a", "Раздел A"), ("b", "Раздел B"), ("c", "Раздел C")]
        normal = self.rows(self.kb.price_menu_keyboard(children))
        active = self.rows(self.kb.price_menu_keyboard(children, active_order=True))
        self.assertEqual([2, 1, 1, 1], [len(row) for row in normal])
        self.assertEqual(["🧮 Примерный расчёт"], self.labels(normal[-2]))
        self.assertEqual(["⬅️ Вернуться в главное меню"], self.labels(normal[-1]))
        self.assertEqual([2, 1, 1], [len(row) for row in active])
        self.assertFalse(any("Примерный расчёт" in label for row in active for label in self.labels(row)))
        self.assertEqual(["⬅️ Вернуться к активной заявке"], self.labels(active[-1]))

    def test_price_button_placements(self):
        dispatcher = self.rows(self.kb.dispatcher_menu())
        self.assertIn(["👥 Водители", "🏷 Прайс"], [self.labels(row) for row in dispatcher])
        online = self.rows(self.kb.driver_menu(True))
        self.assertIn(["🚫 Ложные вызовы", "🏷 Прайс"], [self.labels(row) for row in online])
        offline = self.rows(self.kb.driver_menu(False))
        self.assertIn(["🏷 Прайс", "⏳ Ожидающие"], [self.labels(row) for row in offline])

    def test_active_order_price_is_above_cancel(self):
        for raw in (
            self.kb.driver_ride_keyboard("in_progress"),
            self.kb.driver_delivery_keyboard("shopping"),
        ):
            flat = [self.labels(row)[0] for row in self.rows(raw)]
            self.assertLess(flat.index("🏷 Прайс"), flat.index("❌ Отменить активную заявку"))

    def test_dispatcher_notice_and_queue_dots_are_present(self):
        handlers = (ROOT / "bot/handlers.py").read_text("utf-8")
        self.assertIn("Пассажиры сели и поехали по заявке #{order.id}", handlers)
        self.assertIn('status_dot, status_text = "🔴"', handlers)
        self.assertIn('status_dot, status_text = "🟡"', handlers)
        self.assertIn('status_dot, status_text = "🟢"', handlers)
        self.assertIn('f"{i}. {status_dot} {d.full_name', handlers)

    def test_subscription_gate_stays_removed(self):
        handlers = (ROOT / "bot/handlers.py").read_text("utf-8")
        self.assertNotIn("vk.is_group_member(vk_id)", handlers)
        self.assertNotIn("First-contact subscription screen", handlers)


if __name__ == "__main__":
    unittest.main()
