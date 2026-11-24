"""Ablauf und vor allem die Löschlogik, mit echtem Protokoll und nachgebautem Seafile."""
import dataclasses
import os
from datetime import datetime, timedelta, timezone

import pytest

from seafile_archiver import export, run
from seafile_archiver.config import Config, ConfigError
from seafile_archiver.seafile import Repo, User


class FakeSeafile:
    """Datenbank und Admin-API in einem."""

    def __init__(self):
        self.users, self.repos, self.groups, self.deleted = {}, {}, {}, []
        self.delete_works = True

    def add(self, name, *repos, active=False, uid=True):
        email = f"{name}@auth.local"
        self.users[email] = User(email, name if uid else None, f"{name}@example.com", name, active)
        self.repos[email] = list(repos)
        return email

    def close(self): pass
    def inactive_users(self): return [u for u in self.users.values() if not u.is_active]
    def get_user(self, email): return self.users.get(email)
    def owned_repos(self, email): return list(self.repos.get(email, []))
    def owned_groups(self, email): return self.groups.get(email, [])

    def delete_user(self, email):
        self.deleted.append(email)
        if self.delete_works:
            del self.users[email]


def lib(name="Lib", commit="c1", **kw):
    return Repo(f"id-{name}", name, kw.pop("encrypted", False), 10, 1, commit, **kw)


@pytest.fixture
def sf(monkeypatch):
    fake = FakeSeafile()
    monkeypatch.setattr(run, "SeafileDB", lambda cfg: fake)
    monkeypatch.setattr(run, "SeafileAPI", lambda cfg: fake)

    def export_repo(cfg, repo, dest):
        res = export.RepoResult(repo=repo, commit=repo.head_commit)
        if repo.name == "Kaputt":
            res.problems.append("Block fehlt")
        else:
            os.makedirs(dest)
        return res

    monkeypatch.setattr(export, "export_repo", export_repo)
    return fake


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    (tmp_path / "archiv").mkdir()
    for k, v in {"SEAFILE_MYSQL_DB_PASSWORD": "x", "DRY_RUN": "false", "SEAFILE_URL": "http://sf",
                 "SEAFILE_API_TOKEN": "t", "DELETE_AFTER_DAYS": "30",
                 "ARCHIVE_DIR": str(tmp_path / "archiv"), "STATE_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(k, v)
    return Config.from_env()


def go(cfg, **kw):
    report = run.run_once(dataclasses.replace(cfg, **kw))
    assert not report.items["errors"], report.items["errors"]
    return report


def age(cfg, days=31):
    state = run.State(cfg.state_db)
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    state.db.execute("UPDATE users SET exported_at = ? WHERE exported_at IS NOT NULL", (ts,))
    state.db.commit()
    state.close()


def status(cfg, email):
    state = run.State(cfg.state_db)
    try:
        return state.users()[email]["status"]
    finally:
        state.close()


def test_exportieren_und_nach_frist_loeschen(sf, cfg):
    bob = sf.add("bob", lib())
    go(cfg)
    assert status(cfg, bob) == "exported"
    assert os.path.isdir(os.path.join(cfg.archive_dir, "bob", "Lib"))
    go(cfg)
    age(cfg, 29)
    go(cfg)
    assert sf.deleted == []
    age(cfg, 31)
    go(cfg)
    assert sf.deleted == [bob]
    assert status(cfg, bob) == "deleted"


def test_ohne_frist_wird_nie_geloescht(sf, cfg):
    sf.add("bob", lib())
    go(cfg, delete_after_days=None)
    age(cfg, 9999)
    go(cfg, delete_after_days=None)
    assert sf.deleted == []


def test_probelauf_aendert_nichts(sf, cfg):
    bob = sf.add("bob", lib())
    assert go(cfg, dry_run=True).items["planned"]
    assert os.listdir(cfg.archive_dir) == []
    go(cfg)
    age(cfg)
    assert go(cfg, dry_run=True).items["would_delete"]
    assert sf.deleted == []
    assert status(cfg, bob) == "exported"


def test_aktive_user_bleiben_unberuehrt(sf, cfg):
    sf.add("bob", lib(), active=True)
    assert not go(cfg).has_events


def test_reaktivierter_user_wird_nicht_geloescht(sf, cfg):
    bob = sf.add("bob", lib())
    go(cfg)
    age(cfg)
    sf.users[bob].is_active = True
    go(cfg)
    assert sf.deleted == []
    assert status(cfg, bob) == "reactivated"
    # erneut deaktiviert: neuer Export, die Frist beginnt neu
    sf.users[bob].is_active = False
    go(cfg)
    go(cfg)
    assert sf.deleted == []
    assert status(cfg, bob) == "exported"


@pytest.mark.parametrize("change", ["geteilt", "verschluesselt", "neu", "gruppe"])
def test_blockaden(sf, cfg, change):
    repos = [lib()]
    if change == "geteilt":
        repos = [lib(shared_with=["alice"])]
    if change == "verschluesselt":
        repos.append(lib("Tresor", encrypted=True))
    bob = sf.add("bob", *repos)
    if change == "gruppe":
        sf.groups[bob] = ["Vertrieb"]
    go(cfg)
    if change == "neu":
        sf.repos[bob].append(lib("Neu"))
    age(cfg)
    assert go(cfg).items["blocked"]
    assert not go(cfg).items["blocked"], "nur bei Änderung melden"
    assert sf.deleted == []


def test_geteilt_blockiert_nicht_wenn_abgeschaltet(sf, cfg):
    bob = sf.add("bob", lib(shared_with=["alice"]))
    go(cfg)
    age(cfg)
    go(cfg, block_delete_if_shared=False)
    assert sf.deleted == [bob]


def test_fehlgeschlagener_export_verhindert_loeschen(sf, cfg):
    bob = sf.add("bob", lib(), lib("Kaputt"))
    assert not go(cfg).ok
    age(cfg)
    go(cfg)
    assert sf.deleted == []
    assert status(cfg, bob) == "failed"


def test_veraenderte_bibliothek_wird_neu_exportiert(sf, cfg):
    bob = sf.add("bob", lib(commit="c1"))
    go(cfg)
    age(cfg)
    sf.repos[bob] = [lib(commit="c2")]
    report = go(cfg)
    assert report.items["outdated"] and report.items["exported"]
    go(cfg)
    assert sf.deleted == [], "Frist muss neu beginnen"
    age(cfg)
    go(cfg)
    assert sf.deleted == [bob]


def test_zu_viele_neue_user_bricht_alles_ab(sf, cfg):
    sf.add("alt", lib("Alt"))
    go(cfg)
    age(cfg)
    for i in range(6):
        sf.add(f"u{i}", lib(f"L{i}"))
    report = run.run_once(cfg)
    assert report.items["errors"]
    assert sf.deleted == []
    assert os.listdir(cfg.archive_dir) == ["alt"]


def test_hoechstzahl_loeschungen(sf, cfg):
    for i in range(4):
        sf.add(f"u{i}", lib(f"L{i}"))
    go(cfg)
    age(cfg)
    assert go(cfg, max_deletions_per_run=3).items["postponed"]
    assert len(sf.deleted) == 3


def test_ausgeschlossen_und_ohne_ldap(sf, cfg):
    sf.add("bob", lib())
    sf.add("lokal", lib("L2"), uid=False)
    assert not go(cfg, exclude_users=frozenset({"bob"})).has_events
    go(cfg, exclude_users=frozenset({"bob"}), only_ldap_users=False)
    assert os.listdir(cfg.archive_dir) == ["lokal@example.com"]


def test_loeschen_ohne_wirkung_ist_ein_fehler(sf, cfg):
    bob = sf.add("bob", lib())
    go(cfg)
    age(cfg)
    sf.delete_works = False
    assert run.run_once(cfg).items["errors"]
    assert status(cfg, bob) == "exported"


def test_zu_wenig_platz(sf, cfg):
    bob = sf.add("bob", lib())
    report = go(cfg, free_space_factor=1e30)
    assert report.items["failed"]
    assert status(cfg, bob) == "failed"


def test_frist_braucht_api(cfg, monkeypatch):
    monkeypatch.delenv("SEAFILE_API_TOKEN")
    with pytest.raises(ConfigError):
        Config.from_env()
