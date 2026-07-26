"""Seed the database with initial data.

Run AFTER applying migrations:
    alembic upgrade head
    python -m scripts.seed

Creates:
- the admin user from ADMIN_LOGIN / ADMIN_PASSWORD_HASH
- a default city
- a handful of sample streets
- default settings rows
"""
from __future__ import annotations

from common.config import config
from common.database import session_scope
from common.logger import get_logger
from common import bot_messages_service as bm
from common.models import AdminUser, City, Street
from common.settings_service import ensure_defaults, set_setting

log = get_logger("scripts.seed")

SAMPLE_STREETS = [
    "ул. Ленина", "ул. Советская", "ул. Мира", "ул. Победы",
    "ул. Гагарина", "ул. Пушкина", "пр. Маркса", "ул. Кирова",
    "ул. Молодёжная", "ул. Центральная", "ул. Школьная", "ул. Заводская",
]


def run() -> None:
    with session_scope() as s:
        # Admin user
        login = config.ADMIN_LOGIN
        existing = s.query(AdminUser).filter(AdminUser.login == login).one_or_none()
        if existing:
            existing.password_hash = config.ADMIN_PASSWORD_HASH
            log.info("Updated admin '%s' password hash", login)
        else:
            s.add(AdminUser(login=login, password_hash=config.ADMIN_PASSWORD_HASH))
            log.info("Created admin '%s'", login)

        # Default city
        city = s.query(City).filter(City.name == "Главный город").one_or_none()
        if not city:
            city = City(name="Главный город")
            s.add(city)
            s.flush()
            log.info("Created default city id=%s", city.id)

        # Sample streets
        if s.query(Street).count() == 0:
            for name in SAMPLE_STREETS:
                s.add(Street(name=name, city_id=city.id))
            log.info("Inserted %d sample streets", len(SAMPLE_STREETS))

        # Default settings
        ensure_defaults(s)
        set_setting(s, "city_id_default", str(city.id))
        log.info("Ensured default settings")

        # Default editable bot messages (requirement 9)
        bm.ensure_defaults(s)
        log.info("Ensured default bot messages")

    print("✅ Seed complete.")


if __name__ == "__main__":
    run()
