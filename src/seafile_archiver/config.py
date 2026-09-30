"""Konfiguration aus Umgebungsvariablen.

Die Datenbank-Variablen heißen bewusst wie beim Seafile-Container
(SEAFILE_MYSQL_DB_*), damit dieselbe .env wiederverwendet werden kann.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _str(name)
    if not value:
        return default
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: '{value}' ist kein Wahrheitswert (true/false)")


def _int(name: str, default: int | None) -> int | None:
    value = _str(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name}: '{value}' ist keine Zahl") from None


def _float(name: str, default: float) -> float:
    value = _str(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name}: '{value}' ist keine Zahl") from None


def _list(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in _str(name).split(",") if v.strip())


def _db_name(name: str, default: str) -> str:
    # Datenbanknamen landen in SQL-Statements, deshalb streng prüfen
    value = _str(name, default)
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ConfigError(f"{name}: ungültiger Datenbankname '{value}'")
    return value


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Config:
    # Seafile-Datenbank
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    ccnet_db: str
    seafile_db: str
    seahub_db: str
    ldap_provider: str

    # Seafile-Storage (gelesen über seafobj)
    seafile_data_dir: str
    seafile_conf_dir: str

    # Archiv und Zustand
    archive_dir: str
    state_dir: str
    archive_uid: int | None
    archive_gid: int | None
    write_checksums: bool
    free_space_factor: float

    # Verhalten
    dry_run: bool
    only_ldap_users: bool
    exclude_users: frozenset[str]
    max_users_per_run: int
    max_deletions_per_run: int
    delete_after_days: int | None
    block_delete_if_shared: bool

    # Seafile-API (nur für das Löschen)
    seafile_url: str
    seafile_api_token: str

    # Benachrichtigung
    healthcheck_url: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_security: str
    mail_from: str
    mail_to: tuple[str, ...]

    # Zeitplan
    schedule: tuple[str, ...]
    run_on_start: bool

    @property
    def state_db(self) -> str:
        return os.path.join(self.state_dir, "archiver.sqlite")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.archive_dir, ".staging")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            db_host=_str("SEAFILE_MYSQL_DB_HOST", "mariadb"),
            db_port=_int("SEAFILE_MYSQL_DB_PORT", 3306),
            db_user=_str("SEAFILE_MYSQL_DB_USER", "root"),
            db_password=_str("SEAFILE_MYSQL_DB_PASSWORD"),
            ccnet_db=_db_name("SEAFILE_MYSQL_DB_CCNET_DB_NAME", "ccnet_db"),
            seafile_db=_db_name("SEAFILE_MYSQL_DB_SEAFILE_DB_NAME", "seafile_db"),
            seahub_db=_db_name("SEAFILE_MYSQL_DB_SEAHUB_DB_NAME", "seahub_db"),
            ldap_provider=_str("LDAP_PROVIDER", "ldap"),
            seafile_data_dir=_str("SEAFILE_DATA_DIR", "/shared/seafile/seafile-data"),
            seafile_conf_dir=_str("SEAFILE_CONF_DIR", "/shared/seafile/conf"),
            archive_dir=_str("ARCHIVE_DIR", "/archiv"),
            state_dir=_str("STATE_DIR", "/data"),
            archive_uid=_int("ARCHIVE_UID", None),
            archive_gid=_int("ARCHIVE_GID", None),
            write_checksums=_bool("WRITE_CHECKSUMS", True),
            free_space_factor=_float("FREE_SPACE_FACTOR", 1.1),
            dry_run=_bool("DRY_RUN", True),
            only_ldap_users=_bool("ONLY_LDAP_USERS", True),
            exclude_users=frozenset(v.lower() for v in _list("EXCLUDE_USERS")),
            max_users_per_run=_int("MAX_USERS_PER_RUN", 5),
            max_deletions_per_run=_int("MAX_DELETIONS_PER_RUN", 10),
            delete_after_days=_int("DELETE_AFTER_DAYS", None),
            block_delete_if_shared=_bool("BLOCK_DELETE_IF_SHARED", True),
            seafile_url=_str("SEAFILE_URL").rstrip("/"),
            seafile_api_token=_str("SEAFILE_API_TOKEN"),
            healthcheck_url=_str("HEALTHCHECK_URL").rstrip("/"),
            smtp_host=_str("SMTP_HOST"),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=_str("SMTP_USER"),
            smtp_password=_str("SMTP_PASSWORD"),
            smtp_security=_str("SMTP_SECURITY", "starttls").lower(),
            mail_from=_str("MAIL_FROM"),
            mail_to=_list("MAIL_TO"),
            schedule=_list("SCHEDULE") if "SCHEDULE" in os.environ else ("01:30",),
            run_on_start=_bool("RUN_ON_START", False),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        errors = []
        if not self.db_password:
            errors.append("SEAFILE_MYSQL_DB_PASSWORD fehlt")
        if self.max_users_per_run < 1:
            errors.append("MAX_USERS_PER_RUN muss mindestens 1 sein")
        if self.max_deletions_per_run < 1:
            errors.append("MAX_DELETIONS_PER_RUN muss mindestens 1 sein")
        if self.delete_after_days is not None:
            if self.delete_after_days < 0:
                errors.append("DELETE_AFTER_DAYS darf nicht negativ sein")
            if not (self.seafile_url and self.seafile_api_token):
                errors.append("DELETE_AFTER_DAYS braucht SEAFILE_URL und SEAFILE_API_TOKEN")
        if self.smtp_security not in ("starttls", "ssl", "none"):
            errors.append("SMTP_SECURITY muss starttls, ssl oder none sein")
        if self.mail_to and not (self.smtp_host and self.mail_from):
            errors.append("MAIL_TO braucht SMTP_HOST und MAIL_FROM")
        for t in self.schedule:
            if not _TIME_RE.match(t):
                errors.append(f"SCHEDULE: '{t}' ist keine Uhrzeit im Format HH:MM")
        if errors:
            raise ConfigError("; ".join(errors))
