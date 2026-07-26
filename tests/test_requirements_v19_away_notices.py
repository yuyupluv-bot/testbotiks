import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "bot/away_order_notice_service.py"


def load_notice_text():
    tree = ast.parse(SERVICE.read_text("utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "notice_text"
    )
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(SERVICE), "exec"), namespace)
    return namespace["notice_text"]


class AwayOrderNoticeV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notice_text = staticmethod(load_notice_text())

    def test_russian_count_forms(self):
        expected = {
            1: "Есть заявка в боте, которую не взяли водители.",
            2: "Есть 2 заявки в боте, которые не взяли водители.",
            3: "Есть 3 заявки в боте, которые не взяли водители.",
            4: "Есть 4 заявки в боте, которые не взяли водители.",
            5: "Есть 5 заявок в боте, которые не взяли водители.",
            11: "Есть 11 заявок в боте, которые не взяли водители.",
            12: "Есть 12 заявок в боте, которые не взяли водители.",
            21: "Есть 21 заявка в боте, которую не взяли водители.",
            22: "Есть 22 заявки в боте, которые не взяли водители.",
        }
        for count, text in expected.items():
            with self.subTest(count=count):
                self.assertEqual(text, self.notice_text(count))

    def test_notice_is_tracked_replaced_and_deleted(self):
        source = SERVICE.read_text("utf-8")
        self.assertIn("vk.send_tracked_message", source)
        self.assertIn("outbox_service.cancel_or_delete", source)
        self.assertIn("driver.away_notice_outbox_id", source)
        self.assertIn("driver.away_notice_count", source)
        self.assertIn("POLL_SECONDS = 2.0", source)
        self.assertIn('Order.status.in_(WAITING_STATUSES)', source)
        self.assertIn('Order.driver_id.is_(None)', source)
        self.assertIn('Order.parallel_driver_id.is_(None)', source)

    def test_worker_and_immediate_status_sync_are_wired(self):
        main = (ROOT / "bot/main.py").read_text("utf-8")
        queue = (ROOT / "bot/queue_service.py").read_text("utf-8")
        models = (ROOT / "common/models.py").read_text("utf-8")
        migration = (ROOT / "migrations/versions/0033_away_order_notice.py").read_text("utf-8")
        self.assertIn("away_order_notice_service.start_worker()", main)
        self.assertIn("away_order_notice_service.sync_driver(session, driver)", queue)
        self.assertIn("away_notice_outbox_id = Column(Integer)", models)
        self.assertIn("away_notice_count = Column(Integer", models)
        self.assertIn('down_revision = "0032_fake_call_driver_mention"', migration)


if __name__ == "__main__":
    unittest.main()
