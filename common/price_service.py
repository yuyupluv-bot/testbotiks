"""«Прайс» → «Самые популярные направления» (admin-editable, requirement).

Storage model: ``price_sections`` table (see common.models.PriceSection).

Layout:
    popular_destinations              (root; header text shown on «Прайс»)
    ├── long_distance                 «Дальние расстояния»
    ├── dachas                        «Дачи»
    └── extra_services                «Доп услуги»

Every row's ``title`` is the button caption and ``content`` is the text shown
when the button is pressed (the root's ``content`` is the menu header text).
Both are editable from the VK-bot admin flow and from the web admin panel;
this module is the single place both surfaces talk to, mirroring how
``bot_messages_service`` is used for the rest of the bot's texts.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from common.models import PriceSection

ROOT_KEY = "popular_destinations"
CHILD_KEYS = ["city_pashiya_kusya", "long_distance", "dachas", "extra_services"]

# section_key -> (default title / button caption, default content, parent_key)
DEFAULTS: dict[str, tuple[str, str, str | None]] = {
    ROOT_KEY: (
        "\U0001F3F7 Самые популярные направления",
        "\U0001F3F7 Самые популярные направления\n\nВыберите интересующий раздел, чтобы увидеть подробности:",
        None,
    ),
    "city_pashiya_kusya": (
        "Город, Пашия, Кусья",
        "",
        ROOT_KEY,
    ),
    "long_distance": (
        "\U0001F6E3 Дальние расстояния",
        "\U0001F6E3 Дальние поездки за городом:\n"
        "• Тариф за км за городом: 25 \u20BD/км\n"
        "• Минимальная стоимость поездки: 500 \u20BD\n\n"
        "Точная стоимость озвучивается водителем при подтверждении заказа.",
        ROOT_KEY,
    ),
    "dachas": (
        "\U0001F3E1 Дачи",
        "\U0001F3E1 Поездки на дачные участки:\n"
        "• Стоимость зависит от удалённости дачного массива\n"
        "• Возможна доплата за грунтовую дорогу/бездорожье\n\n"
        "Точную стоимость уточняйте у водителя при оформлении заказа.",
        ROOT_KEY,
    ),
    "extra_services": (
        "\u2795 Доп услуги",
        "\u2795 Дополнительные услуги (справочно):\n"
        "• \U0001F9F3 Багаж — от 50 \u20BD\n"
        "• \U0001F43E Животные — от 50 \u20BD\n"
        "• \U0001F9D2 Дети — без доплаты\n"
        "• \u21AA\uFE0F Заезд не по пути — по договорённости с водителем\n"
        "• \u23F3 С ожиданием — согласно тарифу платного ожидания\n\n"
        "Итоговую стоимость называет водитель.",
        ROOT_KEY,
    ),
}

FALLBACK_TEXT = "Информация временно недоступна."


def all_keys() -> list[str]:
    """Every known key, root first, in a stable order."""
    return [ROOT_KEY, *CHILD_KEYS]


def children_keys() -> list[str]:
    return list(CHILD_KEYS)


def title_for(key: str) -> str:
    entry = DEFAULTS.get(key)
    return entry[0] if entry else key


def changed_content_lines(old_content: str, new_content: str) -> list[str]:
    """Return only lines newly added or changed in the new price text."""
    old_lines = {line.strip() for line in (old_content or "").splitlines() if line.strip()}
    return [
        line.strip()
        for line in (new_content or "").splitlines()
        if line.strip() and line.strip() not in old_lines
    ]


def ensure_defaults(session: Session) -> None:
    """Create any missing default sections in the DB (idempotent)."""
    existing = {s.section_key for s in session.query(PriceSection).all()}
    for order, key in enumerate(all_keys()):
        if key in existing:
            continue
        title, content, parent = DEFAULTS[key]
        session.add(
            PriceSection(
                section_key=key,
                parent_key=parent,
                title=title,
                content=content,
                sort_order=order,
            )
        )
    session.flush()


def _row(session: Session, key: str) -> PriceSection | None:
    return session.query(PriceSection).filter(PriceSection.section_key == key).one_or_none()


def get_section(session: Session, key: str) -> PriceSection | None:
    ensure_defaults(session)
    return _row(session, key)


def get_children(session: Session, parent_key: str = ROOT_KEY) -> list[PriceSection]:
    """Active child sections, in admin-defined order (falls back to CHILD_KEYS order)."""
    ensure_defaults(session)
    rows = (
        session.query(PriceSection)
        .filter(PriceSection.parent_key == parent_key, PriceSection.is_active.is_(True))
        .order_by(PriceSection.sort_order, PriceSection.id)
        .all()
    )
    return rows


def get_title(session: Session, key: str) -> str:
    row = get_section(session, key)
    if row and row.title:
        return row.title
    return title_for(key)


def get_content(session: Session, key: str) -> tuple[str, str | None]:
    """Return (text, file_id). Missing/blank content falls back to a stub."""
    row = get_section(session, key)
    if row is None or not row.content:
        return FALLBACK_TEXT, (row.file_id if row else None)
    return row.content, row.file_id


def set_section(
    session: Session,
    key: str,
    title: str | None = None,
    content: str | None = None,
    file_id: str | None = None,
    update_file: bool = False,
    image_url: str | None = None,
    update_image_url: bool = False,
) -> PriceSection:
    """Upsert a section. Only the fields explicitly passed are changed."""
    row = _row(session, key)
    if row is None:
        default_title, default_content, default_parent = DEFAULTS.get(key, (key, "", None))
        row = PriceSection(
            section_key=key,
            parent_key=default_parent,
            title=title if title is not None else default_title,
            content=content if content is not None else default_content,
        )
        session.add(row)
    else:
        if title is not None:
            row.title = title
        if content is not None:
            row.content = content
    if update_file:
        row.file_id = file_id
    if update_image_url:
        row.image_url = image_url
    session.flush()
    return row
