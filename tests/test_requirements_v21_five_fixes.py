import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot/handlers.py"
ORDER_SERVICE = ROOT / "bot/order_service.py"
PASSENGER_QUEUE = ROOT / "bot/passenger_queue.py"
PARALLEL = ROOT / "bot/parallel_orders.py"


class FiveFixesV21Tests(unittest.TestCase):
    def test_all_changed_modules_parse(self):
        for path in (HANDLERS, ORDER_SERVICE, PASSENGER_QUEUE, PARALLEL):
            ast.parse(path.read_text("utf-8"), filename=str(path))

    def test_no_show_cannot_fall_into_car_failure(self):
        source = HANDLERS.read_text("utf-8")
        fn = source.split("def driver_cancel_active", 1)[1].split("def driver_cancel_back", 1)[0]
        no_show = fn.split('if reason == "no_show":', 1)[1].split('elif reason == "car":', 1)[0]
        self.assertIn("клиент не вышел", no_show)
        self.assertIn("return", no_show)
        self.assertNotIn("неполадка с авто", no_show)
        self.assertIn('reason not in ("no_show", "car")', fn)

    def test_unanswered_driver_card_is_preserved_on_arrival(self):
        source = ORDER_SERVICE.read_text("utf-8")
        fn = source.split("def start_free_waiting", 1)[1].split("def _extras_summary", 1)[0]
        self.assertIn("finalize_tracked_message", fn)
        self.assertIn('order.departure_prompt_outbox_id,\n            "",', fn)
        self.assertNotIn("cancel_or_delete", fn)
        card = HANDLERS.read_text("utf-8").split("def _driver_card", 1)[1].split("def _edit_departure_prompt", 1)[0]
        self.assertIn('[id{driver.vk_id}|{name}]', card)
        self.assertIn("driver.car_model", card)
        self.assertIn("driver.car_color", card)
        self.assertIn("driver.car_number", card)

    def test_live_offer_blocks_every_unrelated_action(self):
        source = HANDLERS.read_text("utf-8")
        self.assertIn('Order.offered_driver_id == driver.id', source)
        self.assertIn('_DRIVER_OFFER_ALLOWED_CMDS = {"accept", "decline", "decline_reason", "decline_back"}', source)
        global_lock = source.index("# While an ordinary request is on the driver's screen")
        global_commands = source.index("# Global commands available from anywhere")
        self.assertLess(global_lock, global_commands)
        driver_handler = source.split("def handle_driver", 1)[1].split("def show_who_on_line", 1)[0]
        self.assertIn("pending_offer = offered_order_for(session, user)", driver_handler)
        timeout = (ROOT / "bot/order_service.py").read_text("utf-8").split("def _accept_timeout", 1)[1].split("def _prearrival_notice", 1)[0]
        self.assertIn("queue_service.leave_queue(session, driver)", timeout)

    def test_passenger_name_is_the_hyperlink(self):
        source = HANDLERS.read_text("utf-8")
        queue_view = source.split("def show_queue", 1)[1].split("def _commission_for_order", 1)[0]
        self.assertIn("passenger_label = _vk_label(p)", queue_view)
        self.assertIn("с {passenger_label}, заявка", queue_view)
        self.assertNotIn("plink", queue_view)
        self.assertNotIn("https://vk.com/id", queue_view)

    def test_actuality_requires_a_live_free_driver(self):
        pqueue = PASSENGER_QUEUE.read_text("utf-8")
        request_fn = pqueue.split("def request_actuality_for_order", 1)[1].split("def confirm", 1)[0]
        self.assertIn("free_driver_available: bool = False", request_fn)
        self.assertIn("if not free_driver_available", request_fn)
        promote = pqueue.split("def try_promote", 1)[1].split("def _recovery_worker", 1)[0]
        self.assertIn("if has_driver and request_actuality_for_order", promote)
        self.assertIn("free_driver_available=True", promote)

        parallel = PARALLEL.read_text("utf-8")
        take = parallel.split("def take", 1)[1].split("def save_eta", 1)[0]
        self.assertNotIn("request_actuality_for_order", take)
        timeout = parallel.split("def _route_offer_timeout", 1)[1].split("def _release", 1)[0]
        self.assertIn("passenger_queue.try_promote(session)", timeout)
        self.assertNotIn("request_actuality_for_order", timeout)


if __name__ == "__main__":
    unittest.main()
