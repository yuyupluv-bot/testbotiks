import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bot/fake_calls_service.py"


def load_helpers():
    tree = ast.parse(SOURCE.read_text("utf-8"))
    wanted = {"profile_link", "profile_mention", "payment_contact_text"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "re": re,
        "Session": object,
        "FakeCall": object,
        "User": object,
        "msg": lambda *args, **kwargs: "",
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes,
        type_ignores=[],
    )
    module = ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class DriverMentionV17Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_helpers()
        cls.driver = SimpleNamespace(vk_id=123456, full_name="Иван Иванов")
        cls.fake_call = SimpleNamespace(amount=100)

    def test_clickable_name_format(self):
        self.assertEqual("[id123456|Иван Иванов]", self.ns["profile_mention"](self.driver))

    def test_broken_escaped_template_falls_back_to_clickable_name(self):
        self.ns["msg"] = lambda *args, **kwargs: (
            r"Свяжитесь с водителем для оплаты штрафа \{amount:.0f\} ₽:\{driver_link\}"
        )
        text = self.ns["payment_contact_text"](None, self.fake_call, self.driver)
        self.assertEqual(
            "Свяжитесь с водителем для оплаты штрафа: [id123456|Иван Иванов]",
            text,
        )

    def test_default_template_contains_driver_mention(self):
        settings = (ROOT / "common/settings_service.py").read_text("utf-8")
        self.assertIn(
            '"msg_fake_call_pay_info": "Свяжитесь с водителем для оплаты штрафа: {driver_mention}"',
            settings,
        )
        migration = (ROOT / "migrations/versions/0032_fake_call_driver_mention.py").read_text("utf-8")
        self.assertIn('down_revision = "0031_actuality_confirmation"', migration)


if __name__ == "__main__":
    unittest.main()
