"""Ein Archivierungslauf.

Reihenfolge:
  1. Sicherheitsprüfung: zu viele neu deaktivierte User → Abbruch ohne jede Aktion
  2. Wieder aktivierte User erkennen (werden nie gelöscht)
  3. Löschen: exportiert + älter als DELETE_AFTER_DAYS + weiterhin inaktiv + nichts Unexportiertes
     (Bibliotheken, die nach dem Export noch verändert wurden, z.B. von Kollegen mit
     Schreibrecht, werden neu exportiert, die Frist beginnt dann neu)
  4. Exportieren: neu deaktivierte User und fehlgeschlagene Versuche
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import traceback
from datetime import datetime, timedelta, timezone

from . import export, notify
from .export import human
from .config import Config
from .seafile import Repo, SeafileAPI, SeafileDB, User
from .state import State, now

log = logging.getLogger(__name__)


class Report:
    SECTIONS = {
        "exported": "Exportiert",
        "planned": "Würde exportieren (Probelauf)",
        "failed": "Fehlgeschlagen (wird im nächsten Lauf erneut versucht)",
        "encrypted": "Verschlüsselte Bibliotheken, nicht exportiert (bitte manuell klären)",
        "outdated": "Nach dem Export verändert, wird neu exportiert (Löschfrist beginnt neu)",
        "blocked": "Löschung blockiert",
        "deleted": "In Seafile gelöscht",
        "would_delete": "Würde in Seafile löschen (Probelauf)",
        "postponed": "Löschung auf spätere Läufe verschoben",
        "reactivated": "Wieder aktiviert (werden nicht gelöscht)",
        "errors": "Fehler",
    }

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.started = datetime.now().astimezone()
        self.items: dict[str, list[str]] = {k: [] for k in self.SECTIONS}
        # Summen je Abschnitt: [User, Bibliotheken, Bytes, Bibliotheken mit unbekannter Größe]
        self.totals: dict[str, list[int]] = {}
        self.skipped = False  # übersprungen, weil noch ein Lauf aktiv war

    def tally(self, section: str, repos: list, sizes: list[int | None]) -> None:
        t = self.totals.setdefault(section, [0, 0, 0, 0])
        t[0] += 1
        t[1] += len(repos)
        t[2] += sum(s or 0 for s in sizes)
        t[3] += sum(1 for s in sizes if s is None)

    def add(self, section: str, text: str) -> None:
        self.items[section].append(text)
        log.info("%s: %s", self.SECTIONS[section], text.splitlines()[0])

    @property
    def ok(self) -> bool:
        return not self.items["failed"] and not self.items["errors"]

    @property
    def has_events(self) -> bool:
        return any(self.items.values())

    def subject(self) -> str:
        parts = []
        for key, label in (("exported", "exportiert"), ("planned", "geplant"), ("deleted", "gelöscht"),
                           ("failed", "fehlgeschlagen"), ("blocked", "blockiert")):
            if self.items[key]:
                parts.append(f"{len(self.items[key])} {label}")
        prefix = "[PROBELAUF] " if self.dry_run else ""
        if self.items["errors"]:
            prefix += "[FEHLER] "
        if not parts:
            parts = ["Lauf mit Fehlern"] if self.items["errors"] else ["keine Änderungen"]
        return f"{prefix}Seafile-Archivierung: " + ", ".join(parts)

    def render(self) -> str:
        lines = [f"Seafile-Archivierung, Lauf vom {self.started:%d.%m.%Y %H:%M}"]
        if self.dry_run:
            lines.append("PROBELAUF: Es wurde nichts exportiert, gelöscht oder protokolliert.")
        for key, title in self.SECTIONS.items():
            if self.items[key]:
                lines += ["", f"{title}:"]
                for item in self.items[key]:
                    first, *rest = item.splitlines()
                    lines.append(f"  - {first}")
                    lines += [f"    {r}" for r in rest]
                if key in self.totals:
                    users, repos, size, unknown = self.totals[key]
                    line = f"  Summe: {users} User, {repos} Bibliotheken, {human(size)}"
                    if unknown:
                        line += f" (Größe von {unknown} Bibliothek(en) unbekannt)"
                    lines.append(line)
        if not self.has_events:
            lines += ["", "Keine Änderungen."]
        return "\n".join(lines) + "\n"


def _user_line(user: User) -> str:
    extra = user.contact_email if user.contact_email and user.contact_email != user.label else None
    return f"{user.label} ({extra})" if extra else user.label


def _is_excluded(cfg: Config, user: User) -> bool:
    keys = {k.lower() for k in (user.email, user.uid, user.contact_email) if k}
    return bool(keys & cfg.exclude_users)


class Archiver:
    def __init__(self, cfg: Config, report: Report):
        self.cfg = cfg
        self.report = report
        self.db = SeafileDB(cfg)
        self.state = State(cfg.state_db)
        self.api = SeafileAPI(cfg) if cfg.seafile_url and cfg.seafile_api_token else None

    def close(self) -> None:
        self.db.close()
        self.state.close()

    # ------------------------------------------------------------------ Ablauf
    def run(self) -> None:
        known = self.state.users()
        new, retry = [], []
        for user in self.db.inactive_users():
            if self.cfg.only_ldap_users and not user.uid:
                continue
            if _is_excluded(self.cfg, user):
                continue
            st = known.get(user.email)
            if st is None or st["status"] == "reactivated":
                new.append(user)
            elif st["status"] == "failed":
                retry.append(user)

        if len(new) > self.cfg.max_users_per_run:
            self.report.add(
                "errors",
                f"{len(new)} neu deaktivierte User, erlaubt sind {self.cfg.max_users_per_run} pro Lauf "
                "(MAX_USERS_PER_RUN). Abbruch ohne Export und ohne Löschung.\n"
                "Bitte prüfen, ob die LDAP-Synchronisierung korrekt arbeitet. Betroffen: "
                + ", ".join(u.label for u in new[:20]) + (" …" if len(new) > 20 else "")
                + "\nErststart oder bewusst viele Austritte? Liste prüfen mit: "
                  f"seafile-archiver run --dry-run --max-users {len(new)}",
            )
            return

        self._check_reactivated(known)
        if self.cfg.delete_after_days is not None:
            retry += self._cleanup()
        for user in new:
            self._archive(user, retry=False)
        seen = {u.email for u in new}
        for user in retry:
            if user.email not in seen:
                seen.add(user.email)
                self._archive(user, retry=True)

    # ---------------------------------------------------------- Reaktivierung
    def _check_reactivated(self, known) -> None:
        for email, st in known.items():
            if st["status"] not in ("exported", "failed"):
                continue
            user = self.db.get_user(email)
            label = st["uid"] or st["contact_email"] or email
            if user is None:
                self.report.add("deleted", f"{label}: wurde außerhalb der Archivierung in Seafile gelöscht")
                if not self.cfg.dry_run:
                    self.state.upsert_user(email, status="deleted", deleted_at=now())
            elif user.is_active:
                self.report.add("reactivated", f"{label}: Archiv bleibt unter {st['archive_dir']} liegen")
                if not self.cfg.dry_run:
                    self.state.upsert_user(email, status="reactivated", blocked_reason=None)

    # ----------------------------------------------------------------- Export
    def _archive(self, user: User, retry: bool) -> None:
        cfg = self.cfg
        st = self.state.users().get(user.email)
        repos = self.db.owned_repos(user.email)
        done = self.state.repos_of(user.email) if retry else {}
        encrypted = [r for r in repos if r.encrypted]
        todo = [r for r in repos if not r.encrypted
                and not (r.repo_id in done and done[r.repo_id]["status"] == "exported")]

        if retry and st and st["archive_dir"] and os.path.isdir(st["archive_dir"]):
            user_dir = st["archive_dir"]
        else:
            user_dir = export.choose_user_dir(cfg, user)

        if cfg.dry_run:
            lines = [f"{_user_line(user)} → {user_dir}"]
            lines += [f"{r.name} ({human(r.size)})" + self._shared_note(r) for r in todo]
            if not repos:
                lines.append("(keine eigenen Bibliotheken)")
            self.report.add("planned", "\n".join(lines))
            self.report.tally("planned", todo, [r.size for r in todo])
            for r in encrypted:
                self.report.add("encrypted", f"{user.label}: {r.name} ({r.repo_id})")
            return

        if not retry:
            self.state.clear_repos(user.email)  # z.B. Reste eines früheren Exports vor einer Reaktivierung
        else:
            current = {r.repo_id for r in repos}
            for repo_id in done.keys() - current:  # inzwischen gelöscht oder übertragen
                self.state.delete_repo(repo_id)
        self.state.upsert_user(user.email, uid=user.uid, contact_email=user.contact_email,
                               archive_dir=user_dir, status="failed", blocked_reason=None)

        ok_space, needed, free = export.free_space_ok(cfg, todo)
        if not ok_space:
            self.report.add("failed", f"{user.label}: zu wenig Platz im Archiv "
                                      f"(benötigt ~{human(needed)}, frei {human(free)})")
            return

        os.makedirs(user_dir, exist_ok=True)
        results: dict[str, export.RepoResult] = {}
        replaced: set[str] = set()
        if todo:
            staging = os.path.join(cfg.staging_dir, f"{os.path.basename(user_dir)}-{datetime.now():%Y%m%d%H%M%S}")
            os.makedirs(staging)
            try:
                for repo in todo:
                    log.info("Exportiere %s: %s (%s)", user.label, repo.name, human(repo.size))
                    tmp = os.path.join(staging, repo.repo_id)
                    res = export.export_repo(cfg, repo, tmp)
                    results[repo.repo_id] = res
                    export.append_log(user_dir, res)
                    if res.ok:
                        previous = done[repo.repo_id]["target"] if repo.repo_id in done else None
                        if previous:
                            replaced.add(previous)
                        export.place(tmp, res, user_dir, previous)
                    else:
                        shutil.rmtree(tmp, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        for res in results.values():
            self.state.upsert_repo(
                res.repo.repo_id, user.email, name=res.repo.name,
                status="exported" if res.ok else "failed",
                target=res.target, files=res.files, bytes=res.bytes, commit_id=res.commit,
                shared_with=res.repo.shared_with, message="; ".join(res.problems) or None,
                renamed=res.renamed if res.ok else None,
            )
        for r in encrypted:
            self.state.upsert_repo(r.repo_id, user.email, name=r.name, status="encrypted",
                                   shared_with=r.shared_with,
                                   message="Verschlüsselt, ohne Passwort nicht exportierbar")

        entries = self._manifest_entries(user.email)
        export.write_manifest(user_dir, user, entries)
        export.write_renamed(user_dir, entries)
        if cfg.write_checksums:
            export.update_checksums(user_dir, [r for r in results.values() if r.ok], replaced)
        export.chown_tree(user_dir, cfg.archive_uid, cfg.archive_gid)

        failed = [res for res in results.values() if not res.ok]
        self.state.upsert_user(user.email, status="failed" if failed else "exported",
                               exported_at=None if failed else now())

        ok_lines = [f"{res.repo.name}" + (f" → {res.target}" if res.target != export.safe_name(res.repo.name) else "")
                    + f": {res.files} {'Datei' if res.files == 1 else 'Dateien'}, "
                    f"{human(res.bytes)}" + self._shared_note(res.repo)
                    + (f"  [{len(res.renamed)} {'Name' if len(res.renamed) == 1 else 'Namen'} angepasst, "
                       f"siehe {export.RENAMED}]" if res.renamed else "")
                    for res in results.values() if res.ok]
        if ok_lines or (not failed and not encrypted):
            fallback = "(alle Bibliotheken bereits exportiert)" if repos else "(keine eigenen Bibliotheken)"
            lines = [f"{_user_line(user)} → {user_dir}"] + (ok_lines or [fallback])
            self.report.add("exported", "\n".join(lines))
            ok = [res for res in results.values() if res.ok]
            self.report.tally("exported", ok, [res.bytes for res in ok])
        for res in failed:
            self.report.add("failed", f"{user.label}: {res.repo.name} ({res.repo.repo_id})\n"
                                      + "\n".join(res.problems))
        if not retry:
            for r in encrypted:
                self.report.add("encrypted", f"{user.label}: {r.name} ({r.repo_id})" + self._shared_note(r))

    @staticmethod
    def _shared_note(repo: Repo) -> str:
        return f"  [geteilt mit: {', '.join(repo.shared_with)}]" if repo.shared_with else ""

    def _manifest_entries(self, email: str) -> list[dict]:
        entries = []
        for r in self.state.repos_of(email).values():
            entries.append({
                "name": r["name"],
                "repo_id": r["repo_id"],
                "status": r["status"],
                "folder": r["target"],
                "files": r["files"],
                "bytes": r["bytes"],
                "commit": r["commit_id"],
                "shared_with": json.loads(r["shared_with"]) if r["shared_with"] else [],
                "renamed": json.loads(r["renamed"]) if r["renamed"] else [],
                "message": r["message"],
                "exported_at": r["updated_at"] if r["status"] == "exported" else None,
            })
        return entries

    # --------------------------------------------------------------- Löschen
    def _cleanup(self) -> list[User]:
        """Löscht fällige User. Gibt User zurück, deren Bibliotheken neu exportiert werden müssen."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.delete_after_days)
        re_export: list[User] = []
        deleted = postponed = 0
        due = [(email, st) for email, st in self.state.users().items()
               if st["status"] == "exported" and st["exported_at"]
               and datetime.fromisoformat(st["exported_at"]) <= cutoff]
        due.sort(key=lambda item: item[1]["exported_at"])  # älteste Exporte zuerst
        for email, st in due:
            user = self.db.get_user(email)
            if user is None or user.is_active:
                continue  # bereits in _check_reactivated behandelt

            exported = self.state.repos_of(email)
            current = self.db.owned_repos(email)
            blockers, outdated = [], []
            for r in current:
                row = exported.get(r.repo_id)
                if row is None:
                    blockers.append(f"{r.name}: nach dem Export hinzugekommen (übertragen?)")
                elif row["status"] == "encrypted":
                    blockers.append(f"{r.name}: verschlüsselt, bitte übertragen oder manuell löschen")
                elif row["status"] != "exported":
                    blockers.append(f"{r.name}: Export fehlgeschlagen")
                elif r.head_commit and row["commit_id"] and not r.head_commit.startswith(row["commit_id"]):
                    outdated.append(r)  # Archiv zuerst aktualisieren, Freigaben blockieren danach
                elif r.shared_with and self.cfg.block_delete_if_shared:
                    blockers.append(f"{r.name}: geteilt mit {', '.join(r.shared_with)}, "
                                    "bitte übertragen oder Freigaben entfernen")

            for group in self.db.owned_groups(email):
                blockers.append(f"Besitzer der Gruppe „{group}“, bitte Gruppe übertragen "
                                "(Systemverwaltung → Gruppen → Menü der Gruppe → Übertragen)")

            if outdated:
                for r in outdated:
                    self.report.add("outdated", f"{user.label}: {r.name}" + self._shared_note(r))
                    if not self.cfg.dry_run:
                        self.state.upsert_repo(r.repo_id, email, name=r.name, status="failed",
                                               message="Nach dem Export verändert")
                if not self.cfg.dry_run:
                    self.state.upsert_user(email, status="failed", exported_at=None)
                    re_export.append(user)
                continue

            if blockers:
                reason = "\n".join(blockers)
                if reason != st["blocked_reason"]:  # nur bei Änderung melden, nicht jede Nacht
                    self.report.add("blocked", f"{user.label}\n{reason}")
                    if not self.cfg.dry_run:
                        self.state.upsert_user(email, blocked_reason=reason)
                continue

            if deleted >= self.cfg.max_deletions_per_run:
                postponed += 1
                continue
            deleted += 1
            names = ", ".join(r.name for r in current) or "keine Bibliotheken"
            if self.cfg.dry_run:
                self.report.add("would_delete", f"{user.label} ({names})")
                continue
            self.api.delete_user(email)
            if self.db.get_user(email) is not None:
                raise RuntimeError(f"User {email} existiert nach dem Löschen noch")
            self.state.upsert_user(email, status="deleted", deleted_at=now(), blocked_reason=None)
            self.report.add("deleted", f"{user.label} ({names}), Archiv: {st['archive_dir']}")
        if postponed:
            self.report.add("postponed", f"{postponed} weitere fällige User, maximal "
                                         f"{self.cfg.max_deletions_per_run} pro Lauf (MAX_DELETIONS_PER_RUN)")
        return re_export


def run_once(cfg: Config) -> Report:
    report = Report(cfg.dry_run)
    os.makedirs(cfg.state_dir, exist_ok=True)
    lock = open(os.path.join(cfg.state_dir, "archiver.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Kein Fehler: ein langer Export läuft noch. Er pingt Healthchecks selbst, wenn er fertig ist;
        # hängt er, schlägt Healthchecks über die ausbleibende Meldung an.
        log.info("Ein vorheriger Lauf ist noch aktiv, dieser Lauf wird übersprungen")
        report.skipped = True
        return report

    notify.ping(cfg, "/start")
    shutil.rmtree(cfg.staging_dir, ignore_errors=True)  # Reste abgebrochener Läufe
    archiver = None
    run_id = None
    try:
        archiver = Archiver(cfg, report)
        run_id = archiver.state.start_run(cfg.dry_run)
        archiver.run()
    except Exception:
        log.exception("Lauf abgebrochen")
        report.add("errors", "Lauf abgebrochen:\n" + traceback.format_exc().strip())
    finally:
        shutil.rmtree(cfg.staging_dir, ignore_errors=True)
        if archiver:
            if run_id is not None:
                archiver.state.finish_run(run_id, report.ok, report.subject())
            archiver.close()

    text = report.render()
    print(text, flush=True)
    if report.has_events:
        try:
            notify.send_mail(cfg, report.subject(), text)
        except Exception as e:
            log.error("Mailversand fehlgeschlagen: %s", e)
            report.add("errors", f"Mailversand fehlgeschlagen: {e}")
            text = report.render()
    notify.ping(cfg, "" if report.ok else "/fail", text)
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    return report
