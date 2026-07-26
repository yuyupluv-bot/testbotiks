import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Release0141Tests(unittest.TestCase):
    def test_removed_aggregate_notice_service_and_calls(self):
        self.assertFalse((ROOT / "bot/unclaimed_notice_service.py").exists())
        for folder in ("bot", "common"):
            for path in (ROOT / folder).rglob("*.py"):
                if path.name == "db_migrate.py":
                    continue
                text = path.read_text("utf-8")
                self.assertNotIn("unclaimed_notice", text, str(path))
                self.assertNotIn("🚕 Есть ", text, str(path))

    def test_legacy_queue_is_cancelled_at_startup(self):
        migration = (ROOT / "common/db_migrate.py").read_text("utf-8")
        self.assertIn("removed aggregate notice (0141)", migration)
        self.assertIn("WHERE text LIKE '🚕 Есть %'", migration)
        self.assertIn("'unclaimed_notice_outbox_id'", migration)

    def test_required_workers_and_safe_event_parser_remain(self):
        main = (ROOT / "bot/main.py").read_text("utf-8")
        self.assertIn("def _event_message(event) -> dict:", main)
        for call in (
            "outbox_service.start_worker()",
            "passenger_queue.start_worker()",
            "booking_service.start_reminder_worker()",
            "maintenance_service.start_worker()",
        ):
            self.assertIn(call, main)

    def test_all_application_python_parses(self):
        paths = list((ROOT / "bot").rglob("*.py")) + list((ROOT / "common").rglob("*.py"))
        self.assertEqual(47, len(paths))
        for path in paths:
            ast.parse(path.read_text("utf-8"), filename=str(path))

    def test_actuality_is_asked_only_on_driver_opportunity(self):
        queue = (ROOT / "bot/passenger_queue.py").read_text("utf-8")
        handlers = (ROOT / "bot/handlers.py").read_text("utf-8")
        models = (ROOT / "common/models.py").read_text("utf-8")
        self.assertIn("has_eligible_busy_driver_for_order", queue)
        self.assertIn("request_actuality_for_order", queue)
        self.assertNotIn('timers.schedule("pqueue_actual"', queue)
        self.assertIn("order.actuality_confirmed = True", queue)
        self.assertIn("if order.actuality_confirmed:", handlers)
        self.assertIn("actuality_confirmed = Column(Boolean", models)

    def test_missed_offer_removes_driver_from_line(self):
        service = (ROOT / "bot/order_service.py").read_text("utf-8")
        keyboards = (ROOT / "bot/keyboards.py").read_text("utf-8")
        self.assertIn("queue_service.leave_queue(session, driver)", service)
        self.assertIn("driver.is_on_line = False", service)
        self.assertIn("Вы сняты с линии", service)
        self.assertIn("kb.missed_offer_timeout_keyboard()", service)
        self.assertIn("def missed_offer_timeout_keyboard", keyboards)
        self.assertIn("⬅️ Вернуться в главное меню", keyboards)

    def test_booking_starts_with_type_picker(self):
        handlers = (ROOT / "bot/handlers.py").read_text("utf-8")
        start = handlers.split("def passenger_booking_start", 1)[1].split(
            "def passenger_booking_fill", 1
        )[0]
        self.assertIn("return passenger_booking_fill(session, user)", start)
        self.assertNotIn("booking_rules_keyboard", start)

    def test_taken_booking_message_has_line_breaks(self):
        handlers = (ROOT / "bot/handlers.py").read_text("utf-8")
        self.assertIn('f"Рейтинг: {format_rating(user)}.\\n"', handlers)
        self.assertIn('f"Автомобиль: {user.car_full}.\\n"', handlers)
        self.assertIn('f"Он будет на месте в {when}.\\n"', handlers)

    def test_chat_card_is_finalized_without_buttons(self):
        outbox = (ROOT / "bot/outbox_service.py").read_text("utf-8")
        orders = (ROOT / "bot/order_service.py").read_text("utf-8")
        self.assertIn('final_text = final_text.rstrip() + "\\n\\n" + text', outbox)
        self.assertIn('empty_keyboard = \'{"buttons":[],"one_time":true}\'', outbox)
        self.assertIn("_claim_finalize_batch()", outbox)
        self.assertIn("Заявка закреплена за водителем:", orders)

    def test_pashiya_and_kusya_free_lines_never_mix(self):
        parallel = (ROOT / "bot/parallel_orders.py").read_text("utf-8")
        queue = (ROOT / "bot/queue_service.py").read_text("utf-8")
        passenger_queue = (ROOT / "bot/passenger_queue.py").read_text("utf-8")
        order_service = (ROOT / "bot/order_service.py").read_text("utf-8")
        self.assertIn("def free_line_city", parallel)
        self.assertIn('("пашия", "пашии")', parallel)
        self.assertIn('("кусья", "кусьи")', parallel)
        self.assertNotIn('line_scope="all"', passenger_queue)
        self.assertIn('parallel_orders.free_line_city(order) or "Горнозаводск"', order_service)
        self.assertIn('({pickup, "горнозаводск"} if pickup else {"горнозаводск"})', queue)

    def test_fresh_database_price_seed_has_required_values(self):
        migration = (
            ROOT / "migrations/versions/0006_price_sections.py"
        ).read_text("utf-8")
        self.assertIn('sa.column("is_active", sa.Boolean)', migration)
        self.assertIn('sa.column("updated_at", sa.DateTime(timezone=True))', migration)
        self.assertEqual(migration.count('"is_active": True'), 4)
        self.assertEqual(migration.count('"updated_at": seeded_at'), 4)


if __name__ == "__main__":
    unittest.main()
