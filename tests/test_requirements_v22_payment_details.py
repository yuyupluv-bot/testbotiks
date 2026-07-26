import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot/handlers.py"


def load_payment_text():
    tree = ast.parse(HANDLERS.read_text("utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_payment_details_text"
    )
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {"re": re, "User": object}
    exec(compile(module, str(HANDLERS), "exec"), namespace)
    return namespace["_payment_details_text"]


class PaymentDetailsV22Tests(unittest.TestCase):
    def test_bank_is_rendered_once_and_duplicate_recipient_is_hidden(self):
        render = load_payment_text()
        user = SimpleNamespace(
            payment_type="phone",
            payment_phone="89990001122",
            payment_bank="Банк Сбербанк",
            payment_card=None,
            payment_recipient="Сбербанк",
        )
        text = render(user)
        self.assertEqual("телефон 89990001122, банк Сбербанк", text)
        self.assertEqual(1, text.count("Сбербанк"))

    def test_payment_edit_is_committed_only_at_final_step(self):
        source = HANDLERS.read_text("utf-8")
        block = source.split("def driver_payment_method", 1)[1].split("def driver_payment_recipient", 1)[0]
        self.assertNotIn("user.payment_type =", block)
        self.assertNotIn("user.payment_phone =", block)
        self.assertNotIn("user.payment_card =", block)
        self.assertNotIn("user.payment_bank =", block)
        final = source.split("def driver_payment_recipient", 1)[1].split("def driver_payment_cancel", 1)[0]
        self.assertIn('user.payment_type = "phone"', final)
        self.assertIn('user.payment_type = "card"', final)
        self.assertIn("user.payment_recipient =", final)

    def test_cancel_discards_draft_without_filling_requisites(self):
        source = HANDLERS.read_text("utf-8")
        handler = source.split("def handle_driver", 1)[1].split("def show_who_on_line", 1)[0]
        self.assertIn('if cmd == "cancel_flow" and state in (', handler)
        self.assertIn("return driver_payment_cancel(session, user)", handler)
        cancel = source.split("def driver_payment_cancel", 1)[1].split("def _payment_details_ready", 1)[0]
        self.assertIn("Сохранённые реквизиты не изменены", cancel)
        self.assertNotIn("user.payment_", cancel)
        self.assertIn("States.D_SETTINGS", cancel)

    def test_send_confirmation_includes_paid_waiting(self):
        source = HANDLERS.read_text("utf-8")
        fn = source.split("def driver_send_payment_details", 1)[1].split("def driver_waiting_start", 1)[0]
        self.assertIn("waiting_service.snapshot(session, order)", fn)
        self.assertIn('confirmation = "Реквизиты отправлены пассажиру."', fn)
        self.assertIn("Начислено за платное ожидание", fn)
        self.assertIn("waiting_minutes", fn)
        self.assertIn("waiting_cost:.0f", fn)
        self.assertIn("if waiting_cost > 0", fn)


if __name__ == "__main__":
    unittest.main()
