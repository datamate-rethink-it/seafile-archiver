"""Healthchecks-Pings und Mail-Bericht. Beides optional."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import requests

from .config import Config

log = logging.getLogger(__name__)


def ping(cfg: Config, suffix: str = "", body: str = "") -> None:
    """suffix: "/start", "" (Erfolg) oder "/fail". Fehler beim Ping brechen den Lauf nicht ab."""
    if not cfg.healthcheck_url:
        return
    try:
        requests.post(
            cfg.healthcheck_url + suffix,
            data=body[-10000:].encode("utf-8"),  # Healthchecks speichert max. 100 kB
            timeout=15,
            headers={"User-Agent": "seafile-archiver"},
        )
    except requests.RequestException as e:
        log.warning("Healthchecks-Ping %s fehlgeschlagen: %s", suffix or "/", e)


def send_mail(cfg: Config, subject: str, body: str) -> None:
    if not cfg.mail_to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.mail_from.split("@")[-1])
    msg.set_content(body)

    if cfg.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context(), timeout=30)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    with smtp:
        if cfg.smtp_security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)
    log.info("Bericht an %s verschickt", ", ".join(cfg.mail_to))
