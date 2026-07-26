"""«Прайс» → «Самые популярные направления» (price_sections table).

Adds the price_sections table used by the reworked «Прайс» passenger menu:
one root row (popular_destinations) holding the menu header, plus three
child rows (long_distance, dachas, extra_services) shown as buttons.

Revision ID: 0006_price_sections
Revises: 0005_lines
"""
from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from alembic import op

revision = "0006_price_sections"
down_revision = "0005_lines"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "price_sections"):
        op.create_table(
            "price_sections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("section_key", sa.String(length=80), nullable=False, unique=True),
            sa.Column("parent_key", sa.String(length=80), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("file_id", sa.String(length=255), nullable=True),
            sa.Column("image_url", sa.String(length=500), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_price_sections_section_key", "price_sections", ["section_key"], unique=True
        )
        op.create_index(
            "ix_price_sections_parent_key", "price_sections", ["parent_key"], unique=False
        )

    price_sections = sa.table(
        "price_sections",
        sa.column("section_key", sa.String),
        sa.column("parent_key", sa.String),
        sa.column("title", sa.String),
        sa.column("content", sa.Text),
        sa.column("sort_order", sa.Integer),
        # Migration 0001 creates the current model on a brand-new database.
        # These model defaults are Python-side, not PostgreSQL server defaults,
        # so every seeded row must provide the non-null values explicitly.
        sa.column("is_active", sa.Boolean),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    count = bind.execute(sa.text("SELECT COUNT(*) FROM price_sections")).scalar() or 0
    if count == 0:
        seeded_at = dt.datetime.now(dt.timezone.utc)
        op.bulk_insert(
            price_sections,
            [
                {
                    "section_key": "popular_destinations",
                    "parent_key": None,
                    "title": "\U0001F3F7 Самые популярные направления",
                    "content": (
                        "\U0001F3F7 Самые популярные направления\n\n"
                        "Выберите интересующий раздел, чтобы увидеть подробности:"
                    ),
                    "sort_order": 0,
                    "is_active": True,
                    "updated_at": seeded_at,
                },
                {
                    "section_key": "long_distance",
                    "parent_key": "popular_destinations",
                    "title": "\U0001F6E3 Дальние расстояния",
                    "content": (
                        "\U0001F6E3 Дальние поездки за городом:\n"
                        "\u2022 Тариф за км за городом: 25 \u20BD/км\n"
                        "\u2022 Минимальная стоимость поездки: 500 \u20BD\n\n"
                        "Точная стоимость озвучивается водителем при подтверждении заказа."
                    ),
                    "sort_order": 1,
                    "is_active": True,
                    "updated_at": seeded_at,
                },
                {
                    "section_key": "dachas",
                    "parent_key": "popular_destinations",
                    "title": "\U0001F3E1 Дачи",
                    "content": (
                        "\U0001F3E1 Поездки на дачные участки:\n"
                        "\u2022 Стоимость зависит от удалённости дачного массива\n"
                        "\u2022 Возможна доплата за грунтовую дорогу/бездорожье\n\n"
                        "Точную стоимость уточняйте у водителя при оформлении заказа."
                    ),
                    "sort_order": 2,
                    "is_active": True,
                    "updated_at": seeded_at,
                },
                {
                    "section_key": "extra_services",
                    "parent_key": "popular_destinations",
                    "title": "\u2795 Доп услуги",
                    "content": (
                        "\u2795 Дополнительные услуги (справочно):\n"
                        "\u2022 \U0001F9F3 Багаж — от 50 \u20BD\n"
                        "\u2022 \U0001F43E Животные — от 50 \u20BD\n"
                        "\u2022 \U0001F9D2 Дети — без доплаты\n"
                        "\u2022 \u21AA\uFE0F Заезд не по пути — по договорённости с водителем\n"
                        "\u2022 \u23F3 С ожиданием — согласно тарифу платного ожидания\n\n"
                        "Итоговую стоимость называет водитель."
                    ),
                    "sort_order": 3,
                    "is_active": True,
                    "updated_at": seeded_at,
                },
            ],
        )

    # Editable button captions for the new menu (settings table).
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    existing_keys = {
        row[0] for row in bind.execute(sa.text("SELECT key FROM settings")).fetchall()
    }
    if "btn_price_back" not in existing_keys:
        op.bulk_insert(settings, [{"key": "btn_price_back", "value": "\u2B05\uFE0F Назад"}])


def downgrade() -> None:
    op.drop_table("price_sections")
