import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot/handlers.py"


def load_finish_prompt(captured):
    tree = ast.parse(HANDLERS.read_text("utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "driver_finish_prompt"
    )

    class Waiting:
        @staticmethod
        def snapshot(session, order):
            return 5, 30.0

    class Night:
        @staticmethod
        def amount(session):
            return 0.0

    class Delivery:
        @staticmethod
        def is_delivery(order):
            return False

    class VK:
        @staticmethod
        def send_message(vk_id, text, **kwargs):
            captured.append((vk_id, text, kwargs))

    namespace = {
        "active_order_for": lambda session, user, as_driver=False: SimpleNamespace(
            id=77,
            dispatcher_id=10,
            night_surcharge=False,
        ),
        "show_main_menu": lambda session, user: None,
        "driver_complete_delivery": lambda session, user, order: None,
        "delivery_service": Delivery(),
        "waiting_service": Waiting(),
        "night_tariff": Night(),
        "vk": VK(),
        "set_state": lambda *args, **kwargs: None,
        "States": SimpleNamespace(D_FINISH_PRICE="driver_finish_price"),
        "Session": object,
        "User": object,
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(HANDLERS), "exec"), namespace)
    return namespace["driver_finish_prompt"]


class DispatcherWaitingV18Tests(unittest.TestCase):
    def test_dispatcher_prompt_contains_paid_waiting(self):
        captured = []
        finish_prompt = load_finish_prompt(captured)
        finish_prompt(None, SimpleNamespace(vk_id=123))
        self.assertEqual(1, len(captured))
        text = captured[0][1]
        self.assertIn("Это заявка от диспетчера", text)
        self.assertIn("Начислено за платное ожидание: 5 мин, 30 ₽", text)
        self.assertIn("Бот НЕ добавляет эти суммы автоматически", text)
        self.assertIn("Введите итоговую стоимость поездки", text)
        self.assertNotIn("без доплат", text)

    def test_old_dispatcher_exclusion_was_removed(self):
        source = HANDLERS.read_text("utf-8")
        section = source.split("def driver_finish_prompt", 1)[1].split("PRICE_MIN", 1)[0]
        self.assertNotIn("без доплат за дополнительные", section)
        self.assertIn('prompt_parts.append("🎧 Это заявка от диспетчера.")', section)


if __name__ == "__main__":
    unittest.main()
