"""Zugriff auf Seafile.

Gelesen wird direkt aus der Datenbank (nur SELECT). Gelöscht wird
ausschließlich über die Admin-API, damit Seahub selbst aufräumt
(Freigaben, Tokens, Gruppenmitgliedschaften, Papierkorb).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

import pymysql
import requests

from .config import Config


@dataclass
class User:
    email: str                 # interne Seafile-ID, bei LDAP-Usern <hash>@auth.local
    uid: str | None            # LDAP-Login (LDAP_LOGIN_ATTR), z.B. "bob"
    contact_email: str | None
    name: str | None
    is_active: bool

    @property
    def label(self) -> str:
        """Lesbare Bezeichnung für Berichte."""
        return self.uid or self.contact_email or self.email


@dataclass
class Repo:
    repo_id: str
    name: str
    encrypted: bool
    size: int | None           # asynchron berechnet, kann fehlen
    file_count: int | None     # dito
    head_commit: str | None    # aktueller Stand (Branch master)
    version: int = 1           # Repo-Format, nötig zum Lesen der Commit-Objekte
    shared_with: list[str] = field(default_factory=list)


class SeafileDB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = pymysql.connect(
            host=cfg.db_host,
            port=cfg.db_port,
            user=cfg.db_user,
            password=cfg.db_password,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def close(self) -> None:
        self.conn.close()

    def _query(self, sql: str, args=None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def _users(self, where: str, args) -> list[User]:
        c, h = self.cfg.ccnet_db, self.cfg.seahub_db
        rows = self._query(
            f"""
            SELECT e.email, e.is_active, s.uid, p.contact_email, p.nickname
            FROM {c}.EmailUser e
            LEFT JOIN {h}.social_auth_usersocialauth s
                   ON s.username = e.email AND s.provider = %s
            LEFT JOIN {h}.profile_profile p ON p.user = e.email
            WHERE {where}
            """,
            (self.cfg.ldap_provider, *args),
        )
        return [
            User(
                email=r["email"],
                uid=r["uid"],
                contact_email=r["contact_email"],
                name=r["nickname"] or None,
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def inactive_users(self) -> list[User]:
        return self._users("e.is_active = 0", ())

    def get_user(self, email: str) -> User | None:
        users = self._users("e.email = %s", (email,))
        return users[0] if users else None

    def any_head(self) -> tuple[str, int, str] | None:
        """Irgendeine Bibliothek mit Head-Commit, für die Storage-Prüfung."""
        s = self.cfg.seafile_db
        rows = self._query(
            f"""
            SELECT b.repo_id, i.version, b.commit_id
            FROM {s}.Branch b JOIN {s}.RepoInfo i ON i.repo_id = b.repo_id
            WHERE b.name = 'master' AND i.is_encrypted = 0
            LIMIT 1
            """
        )
        return (rows[0]["repo_id"], rows[0]["version"] or 1, rows[0]["commit_id"]) if rows else None

    def owned_groups(self, email: str) -> list[str]:
        """Gruppen, deren Besitzer der User ist. Nach dem Löschen des Users wären sie verwaist
        (keine neuen Freigaben, Übertragung scheitert), siehe forum.seafile.com/t/14484."""
        rows = self._query(
            f"SELECT group_name FROM {self.cfg.ccnet_db}.`Group` WHERE creator_name = %s ORDER BY group_name",
            (email,),
        )
        return [r["group_name"] for r in rows]

    def owned_repos(self, email: str) -> list[Repo]:
        """Eigene Bibliotheken des Users, ohne virtuelle Repos (Unterordner-Freigaben)."""
        s = self.cfg.seafile_db
        rows = self._query(
            f"""
            SELECT o.repo_id, i.name, i.is_encrypted, i.version, rs.size, fc.file_count, b.commit_id
            FROM {s}.RepoOwner o
            LEFT JOIN {s}.RepoInfo i       ON i.repo_id = o.repo_id
            LEFT JOIN {s}.RepoSize rs      ON rs.repo_id = o.repo_id
            LEFT JOIN {s}.RepoFileCount fc ON fc.repo_id = o.repo_id
            LEFT JOIN {s}.VirtualRepo v    ON v.repo_id = o.repo_id
            LEFT JOIN {s}.Branch b         ON b.repo_id = o.repo_id AND b.name = 'master'
            WHERE o.owner_id = %s AND v.repo_id IS NULL
            ORDER BY i.name
            """,
            (email,),
        )
        repos = {
            r["repo_id"]: Repo(
                repo_id=r["repo_id"],
                name=r["name"] or r["repo_id"],
                encrypted=bool(r["is_encrypted"]),
                size=r["size"],
                file_count=r["file_count"],
                head_commit=r["commit_id"],
                version=r["version"] if r["version"] is not None else 1,
            )
            for r in rows
        }
        if repos:
            for repo_id, target in self._shares(list(repos)):
                repos[repo_id].shared_with.append(target)
        return list(repos.values())

    def _shares(self, repo_ids: list[str]) -> list[tuple[str, str]]:
        """Freigaben der Bibliotheken als (repo_id, Empfänger).

        Freigaben von Unterordnern hängen an einer virtuellen Bibliothek mit eigener ID
        (VirtualRepo) und werden der Ursprungsbibliothek zugeordnet.
        """
        s, c, h = self.cfg.seafile_db, self.cfg.ccnet_db, self.cfg.seahub_db
        marks = ",".join(["%s"] * len(repo_ids))
        origin: dict[str, tuple[str, str | None]] = {rid: (rid, None) for rid in repo_ids}
        for v in self._query(
            f"SELECT repo_id, origin_repo, path FROM {s}.VirtualRepo WHERE origin_repo IN ({marks})",
            repo_ids,
        ):
            origin[v["repo_id"]] = (v["origin_repo"], v["path"])

        ids = list(origin)
        marks = ",".join(["%s"] * len(ids))
        users = self._query(
            f"""
            SELECT sr.repo_id, COALESCE(p.contact_email, sr.to_email) AS target
            FROM {s}.SharedRepo sr
            LEFT JOIN {h}.profile_profile p ON p.user = sr.to_email
            WHERE sr.repo_id IN ({marks})
            """,
            ids,
        )
        groups = self._query(
            f"""
            SELECT rg.repo_id, CONCAT('Gruppe ', COALESCE(g.group_name, rg.group_id)) AS target
            FROM {s}.RepoGroup rg
            LEFT JOIN {c}.`Group` g ON g.group_id = rg.group_id
            WHERE rg.repo_id IN ({marks})
            """,
            ids,
        )
        result = []
        for r in users + groups:
            repo_id, path = origin[r["repo_id"]]
            result.append((repo_id, r["target"] + (f" (Ordner {path})" if path else "")))
        return result


class SeafileAPI:
    def __init__(self, cfg: Config):
        self.base = cfg.seafile_url
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {cfg.seafile_api_token}"
        self.session.headers["Accept"] = "application/json"

    def delete_user(self, email: str) -> None:
        """Löscht den User. Seahub entfernt dabei alle Bibliotheken, die ihm gehören."""
        r = self.session.delete(
            f"{self.base}/api/v2.1/admin/users/{quote(email, safe='@')}/", timeout=120
        )
        if r.status_code >= 400:
            raise RuntimeError(f"DELETE user {email}: HTTP {r.status_code} {r.text[:200]}")
