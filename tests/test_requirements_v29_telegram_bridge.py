import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TelegramBridgeV29Tests(unittest.TestCase):
    def source(self, path):
        return (ROOT / path).read_text("utf-8")

    def test_changed_python_parses(self):
        for path in (
            "telegram_main.py", "bot/telegram_messaging.py",
            "bot/vk_client.py", "bot/order_service.py", "common/models.py",
            "common/config.py", "migrations/versions/0035_telegram_bridge.py",
        ):
            ast.parse(self.source(path), filename=path)

    def test_manual_phone_is_normalized_to_plus_79(self):
        src = self.source("telegram_main.py")
        self.assertIn('STRICT_PHONE_RE = re.compile(r"^\\+79\\d{9}$")', src)
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_manual_phone")
        namespace = {"STRICT_PHONE_RE": re.compile(r"^\+79\d{9}$"), "re": re}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "telegram_main.py", "exec"), namespace)
        parse = namespace["_manual_phone"]
        self.assertEqual("+79991234567", parse("+79991234567"))
        self.assertEqual("+79991234567", parse("89991234567"))
        self.assertEqual("+79991234567", parse("79991234567"))
        for invalid in ("+78991234567", "+7999123456", "+799912345678"):
            self.assertIsNone(parse(invalid))

    def test_contact_button_and_owner_verification(self):
        src = self.source("telegram_main.py")
        self.assertIn('"request_contact": True', src)
        self.assertIn('int(contact.get("user_id")) != int(telegram_user_id)', src)
        self.assertIn('reply_markup={"remove_keyboard": True}', src)

    def test_webhook_secret_and_subscription(self):
        src = self.source("telegram_main.py")
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", src)
        self.assertIn('body["secret_token"]', src)
        self.assertIn("setWebhook", src)

    def test_driver_receives_telegram_source_phone_and_text(self):
        src = self.source("bot/order_service.py")
        self.assertIn("🔔 Новая заявка из Telegram 📲 #{order.id}", src)
        self.assertIn("📞 Номер телефона", src)
        self.assertIn("📝 Текст заявки", src)

    def test_outbox_and_router_are_wired(self):
        sender = self.source("bot/vk_client.py")
        outbox = self.source("bot/telegram_messaging.py")
        self.assertIn("telegram_messaging", sender)
        self.assertIn("TelegramOutboxMessage", outbox)
        self.assertIn("api.telegram.org", outbox)

    def test_migration_is_additive(self):
        src = self.source("migrations/versions/0035_telegram_bridge.py")
        self.assertIn('down_revision = "0034_temporary_driver_until"', src)
        self.assertIn("telegram_outbox_messages", src)
        self.assertIn("telegram_processed_events", src)


if __name__ == "__main__":
    unittest.main()
