"""seafile-archiver: Bibliotheken deaktivierter Seafile-User archivieren.

  serve   läuft dauerhaft und startet zu den Zeiten in SCHEDULE (Standard im Container)
  run     ein einzelner Lauf (--dry-run / --live überschreiben DRY_RUN)
  status  zeigt das Protokoll
  check   prüft Konfiguration, Datenbank, Storage-Zugriff, Archivverzeichnis und API
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta

from .config import Config, ConfigError
from .export import check_storage, human
from .run import run_once
from .seafile import SeafileDB
from .state import State

log = logging.getLogger("seafile_archiver")


def next_run(schedule: tuple[str, ...], now: datetime) -> datetime:
    candidates = []
    for t in schedule:
        hh, mm = map(int, t.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidates.append(at if at > now else at + timedelta(days=1))
    return min(candidates)


def cmd_serve(cfg: Config) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not cfg.schedule:
        log.info("SCHEDULE ist leer: kein eigener Zeitplan, Läufe extern starten (z.B. Ofelia: seafile-archiver run)")
        while True:
            time.sleep(3600)
    log.info("Zeitplan: täglich %s, %s", ", ".join(cfg.schedule), "PROBELAUF" if cfg.dry_run else "LIVE")
    if cfg.run_on_start:
        run_once(cfg)
    while True:
        at = next_run(cfg.schedule, datetime.now())
        log.info("Nächster Lauf: %s", at.strftime("%d.%m.%Y %H:%M"))
        while (wait := (at - datetime.now()).total_seconds()) > 0:
            time.sleep(min(wait, 60))  # kurze Schritte, damit Zeitsprünge (Suspend, Uhrstellung) nicht stören
        run_once(cfg)


def _local(ts: str | None) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%d.%m.%Y %H:%M") if ts else "-"


def cmd_status(cfg: Config) -> int:
    if not os.path.exists(cfg.state_db):
        print("Noch kein Protokoll vorhanden.")
        return 0
    state = State(cfg.state_db)
    users = state.users()
    if not users:
        print("Noch keine User archiviert.")
    for email, u in sorted(users.items(), key=lambda kv: kv[1]["first_seen_at"]):
        label = u["uid"] or u["contact_email"] or email
        print(f"{label:<20} {u['status']:<12} exportiert {_local(u['exported_at']):<16} "
              f"gelöscht {_local(u['deleted_at']):<16} {u['archive_dir'] or ''}")
        for r in state.repos_of(email).values():
            print(f"    {r['name']:<30} {r['status']:<10} {r['files'] or 0:>7} Dateien {human(r['bytes']):>10}"
                  + (f"  {r['message']}" if r["message"] else ""))
        if u["blocked_reason"]:
            print("    Löschung blockiert: " + u["blocked_reason"].replace("\n", "; "))
    print("\nLetzte Läufe:")
    for r in state.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10"):
        flag = "Probelauf" if r["dry_run"] else "live"
        result = "ok" if r["ok"] else "FEHLER" if r["ok"] is not None else "läuft/abgebrochen"
        print(f"  {_local(r['started_at'])}  {flag:<9} {result:<6} {r['summary'] or ''}")
    return 0


def cmd_check(cfg: Config) -> int:
    ok = True

    def result(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'OK' if good else 'FEHLER'}] {name}" + (f": {detail}" if detail else ""))

    print("Konfiguration:")
    for f in dataclasses.fields(cfg):
        value = getattr(cfg, f.name)
        if any(s in f.name for s in ("password", "token")) and value:
            value = "***"
        print(f"  {f.name} = {value}")
    print()
    try:
        db = SeafileDB(cfg)
        inactive = db.inactive_users()
        result("Datenbank", True, f"{len(inactive)} deaktivierte User")
        head = db.any_head()
        db.close()
        if head:
            try:
                backend = check_storage(cfg, *head)
                result("Seafile-Storage", True, f"Backend {backend}, Commit von {head[0]} gelesen")
            except Exception as e:
                result("Seafile-Storage", False, f"{type(e).__name__}: {e}")
        else:
            print("[--] Seafile-Storage: keine Bibliothek zum Testen gefunden")
    except Exception as e:
        result("Datenbank", False, str(e))
    result("Seafile-Konfiguration", os.path.isfile(os.path.join(cfg.seafile_conf_dir, "seafile.conf")),
           cfg.seafile_conf_dir)
    writable = os.path.isdir(cfg.archive_dir) and os.access(cfg.archive_dir, os.W_OK)
    if writable:
        st = os.statvfs(cfg.archive_dir)
        result("Archivverzeichnis", True, f"{cfg.archive_dir}, frei {human(st.f_bavail * st.f_frsize)}")
    else:
        result("Archivverzeichnis", False, f"{cfg.archive_dir} fehlt oder ist nicht beschreibbar")
    result("Zustandsverzeichnis", os.access(cfg.state_dir, os.W_OK), cfg.state_dir)
    if cfg.seafile_url and cfg.seafile_api_token:
        import requests
        try:
            r = requests.get(f"{cfg.seafile_url}/api/v2.1/admin/users/?per_page=1",
                             headers={"Authorization": f"Token {cfg.seafile_api_token}"}, timeout=15)
            result("Seafile-Admin-API", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as e:
            result("Seafile-Admin-API", False, str(e))
    else:
        print("[--] Seafile-Admin-API nicht konfiguriert (nur nötig für DELETE_AFTER_DAYS)")
    print("\nModus:", "PROBELAUF (DRY_RUN=true)" if cfg.dry_run else "LIVE")
    return 0 if ok else 1


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().handlers[0].addFilter(
        # seafobj loggt jede geladene Storage-Konfiguration als INFO über den Root-Logger
        lambda record: record.levelno > logging.INFO or "/seafobj/" not in record.pathname
    )
    parser = argparse.ArgumentParser(prog="seafile-archiver", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p_run = sub.add_parser("run")
    mode = p_run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Probelauf erzwingen")
    mode.add_argument("--live", action="store_true", help="echten Lauf erzwingen")
    p_run.add_argument("--max-users", type=int, metavar="N",
                       help="MAX_USERS_PER_RUN für diesen Lauf überschreiben (z.B. Altbestand beim Erststart)")
    sub.add_parser("status")
    sub.add_parser("check")
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("Konfigurationsfehler: %s", e)
        return 2

    if args.cmd == "run":
        if args.dry_run or args.live:
            cfg = dataclasses.replace(cfg, dry_run=args.dry_run)
        if args.max_users is not None:
            cfg = dataclasses.replace(cfg, max_users_per_run=args.max_users)
            try:
                cfg.validate()
            except ConfigError as e:
                log.error("Konfigurationsfehler: %s", e)
                return 2
        report = run_once(cfg)
        return 0 if report.ok else 1
    return {"serve": cmd_serve, "status": cmd_status, "check": cmd_check}[args.cmd](cfg)


if __name__ == "__main__":
    sys.exit(main())
