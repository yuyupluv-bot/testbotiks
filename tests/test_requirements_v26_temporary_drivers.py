import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TemporaryDriversV26Tests(unittest.TestCase):
    def source(self, path):
        return (ROOT / path).read_text("utf-8")

    def test_changed_python_parses(self):
        for path in (
            "bot/keyboards.py", "bot/handlers.py", "bot/vk_client.py",
            "bot/temporary_driver_service.py", "bot/main.py", "common/models.py",
            "migrations/versions/0034_temporary_driver_until.py",
        ):
            ast.parse(self.source(path), filename=path)

    def test_admin_menu_layout(self):
        src = self.source("bot/keyboards.py")
        menu = src.split("def admin_menu", 1)[1].split("def admin_remove_role_keyboard", 1)[0]
        self.assertNotIn("Сообщения бота", menu)
        self.assertNotIn("Прайс / Направления", menu)
        self.assertIn('_btn("➕ Водителя", GREEN', menu)
        self.assertIn('_btn("➕ Диспетчера", GREEN', menu)
        self.assertIn('_btn("🕐 Суточники", BLUE', menu)

    def test_vk_ru_and_vk_com_links_are_supported(self):
        src = self.source("bot/vk_client.py")
        self.assertIn("vk\\.(?:com|ru)", src)

    def test_temporary_driver_input_parser(self):
        tree = ast.parse(self.source("bot/handlers.py"))
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in ("_parse_temporary_driver_request", "_days_word")
        ]
        namespace = {"re": re}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "handlers.py", "exec"), namespace)
        parse = namespace["_parse_temporary_driver_request"]
        self.assertEqual(("https://vk.ru/id123", 1), parse("https://vk.ru/id123 1 день"))
        self.assertEqual(("@driver_name", 3), parse("@driver_name 3 дня"))
        self.assertEqual(("123456", 12), parse("123456 12 дней"))
        self.assertIsNone(parse("https://vk.ru/id123"))
        self.assertIsNone(parse("id123 0 дней"))
        self.assertIsNone(parse("id123 366 дней"))

    def test_expired_role_waits_for_driver_work(self):
        src = self.source("bot/temporary_driver_service.py")
        self.assertIn("Order.offered_driver_id == driver.id", src)
        self.assertIn('Order.status.in_(("assigned", "arrived", "in_progress"))', src)
        self.assertIn("Order.parallel_driver_id == driver.id", src)
        self.assertIn("driver.revoke_role(ROLE_DRIVER)", src)
        self.assertIn("driver.role = ROLE_PASSENGER", src)
        handlers = self.source("bot/handlers.py")
        self.assertIn("temporary_driver_service.expire_driver_if_due(session, user)", handlers)

    def test_migration_and_raw_guard_exist(self):
        self.assertIn("temporary_driver_until = Column(DateTime(timezone=True))", self.source("common/models.py"))
        self.assertIn("temporary_driver_until TIMESTAMPTZ", self.source("common/db_migrate.py"))
        self.assertIn('down_revision = "0033_away_order_notice"', self.source("migrations/versions/0034_temporary_driver_until.py"))


if __name__ == "__main__":
    unittest.main()
