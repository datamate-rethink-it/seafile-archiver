"""Export über seafobj und Ablage im Archivverzeichnis.

seafobj ist Seafiles eigene Python-Bibliothek zum Lesen der Objekte (Commits,
Verzeichnisse, Dateien, Blöcke) aus dem konfigurierten Storage (Disk, S3, Ceph,
Swift, OSS). Exportiert wird genau der Head-Commit aus der Datenbank. Jeder Block
wird beim Lesen geprüft (Block-ID = SHA1 des Inhalts), jede Datei auf ihre Größe.
Fehlt oder ist etwas beschädigt, schlägt der Export der Bibliothek fehl, statt
still einen anderen Stand zu liefern.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .seafile import Repo, User

log = logging.getLogger(__name__)

PROGRESS_SECONDS = int(os.environ.get("PROGRESS_SECONDS", "60"))  # Fortschritt im Log bei langen Exporten

EMPTY_ID = "0" * 40  # leeres Verzeichnis bzw. leere Datei

# Metadaten im Archivordner; Präfix "_" vermeidet Kollisionen mit Bibliotheksnamen
MANIFEST = "_manifest.json"
EXPORT_LOG = "_export.log"
CHECKSUMS = "_SHA256SUMS"
RENAMED = "_UMBENANNT.txt"


class ExportError(Exception):
    pass


@dataclass
class RepoResult:
    repo: Repo
    commit: str | None = None
    problems: list[str] = field(default_factory=list)
    target: str | None = None          # Ordner relativ zum Userordner
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    checksums: list[tuple[str, str]] = field(default_factory=list)  # (Pfad relativ zur Bibliothek, sha256)
    renamed: list[tuple[str, str]] = field(default_factory=list)    # (Originalpfad, Pfad im Archiv)

    @property
    def ok(self) -> bool:
        return not self.problems


def human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{n} B"


def _seafobj(cfg: Config):
    """Importiert seafobj erst, nachdem die Umgebung gesetzt ist (liest sie beim Import)."""
    os.environ.setdefault("SEAFILE_CONF_DIR", cfg.seafile_data_dir)
    os.environ.setdefault("SEAFILE_CENTRAL_CONF_DIR", cfg.seafile_conf_dir)
    from seafobj import block_mgr, commit_mgr, fs_mgr
    return commit_mgr, fs_mgr, block_mgr


def check_storage(cfg: Config, repo_id: str, version: int, commit_id: str) -> str:
    """Liest einen Commit, um den Storage-Zugriff zu prüfen. Gibt den Backend-Namen zurück."""
    commit_mgr, _fs, _blocks = _seafobj(cfg)
    commit = commit_mgr.load_commit(repo_id, version, commit_id)
    if commit is None:
        raise ExportError(f"Commit {commit_id} von {repo_id} nicht lesbar")
    return commit_mgr.get_backend_name()


def export_repo(cfg: Config, repo: Repo, dest: str) -> RepoResult:
    """Schreibt den aktuellen Stand der Bibliothek nach dest (darf noch nicht existieren)."""
    res = RepoResult(repo=repo)
    started = time.monotonic()
    try:
        if not repo.head_commit:
            raise ExportError("Kein Head-Commit in der Datenbank")
        commit_mgr, fs_mgr, block_mgr = _seafobj(cfg)
        commit = commit_mgr.load_commit(repo.repo_id, repo.version, repo.head_commit)
        if commit is None:
            raise ExportError(f"Head-Commit {repo.head_commit} nicht lesbar")
        if str(getattr(commit, "encrypted", "")).lower() == "true":
            raise ExportError("Bibliothek ist verschlüsselt")
        res.commit = commit.commit_id
        os.makedirs(dest)
        _Walker(repo, fs_mgr, block_mgr, res).dir(commit.root_id, dest, "", "", commit.ctime)
    except Exception as e:
        res.problems.append(f"{type(e).__name__}: {e}")
    res.seconds = time.monotonic() - started
    return res


# Fehler, die nichts mit dem Namen zu tun haben: kein Ausweichnamen-Versuch
_NOT_NAME_ERRORS = {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS}


class _Walker:
    """Schreibt einen Verzeichnisbaum.

    Namen werden zunächst unverändert übernommen. Lehnt das Ziel einen Namen ab (z.B. SMB:
    ':' '?' '*', reservierte Windows-Namen) oder kollidiert er (Ziel unterscheidet nicht
    zwischen Groß- und Kleinschreibung), wird ein angepasster Name verwendet und die
    Umbenennung festgehalten.
    """

    def __init__(self, repo: Repo, fs_mgr, block_mgr, res: RepoResult):
        self.repo = repo
        self.fs_mgr = fs_mgr
        self.block_mgr = block_mgr
        self.res = res
        self.last_progress = time.monotonic()

    def _create(self, parent: str, name: str, make, rel: str, arel: str):
        """Legt parent/name mit make() an. Gibt (verwendeter Name, Ergebnis von make) zurück."""
        first_error: OSError | None = None
        # Auf SMB/NTFS trennt ':' einen alternativen Datenstrom ab: "a:b.txt" wird zur leeren
        # Datei "a", der Inhalt landet unsichtbar im Strom. Das Schreiben "gelingt" trotzdem,
        # deshalb solche Namen nie unverändert versuchen (mit Samba nachgewiesen).
        if ":" not in name:
            try:
                return name, make(os.path.join(parent, name))
            except OSError as e:
                if e.errno in _NOT_NAME_ERRORS:
                    raise
                first_error = e
        base = portable_name(name)
        for n in range(1, 1000):
            candidate = base if n == 1 else numbered_name(base, n)
            if candidate == name:
                continue
            try:
                result = make(os.path.join(parent, candidate))
            except FileExistsError:
                continue
            except OSError as e:
                raise (first_error or e) from None
            self.res.renamed.append((f"{rel}{name}", f"{arel}{candidate}"))
            return candidate, result
        raise first_error or FileExistsError(f"Kein freier Name für '{rel}{name}'")

    def dir(self, dir_id: str, path: str, rel: str, arel: str, mtime: int | None) -> None:
        """path existiert bereits; rel = Originalpfad, arel = Pfad im Archiv (je mit '/' am Ende)."""
        if dir_id != EMPTY_ID:
            d = self.fs_mgr.load_seafdir(self.repo.repo_id, self.repo.version, dir_id)
            for dent in d.get_files_list():
                self.file(dent, path, rel, arel)
            for dent in d.get_subdirs_list():
                name, _ = self._create(path, dent.name, os.mkdir, rel, arel)
                self.dir(dent.id, os.path.join(path, name), f"{rel}{dent.name}/", f"{arel}{name}/", dent.mtime)
        if mtime:  # erst nach dem Befüllen setzen, sonst überschreibt das Schreiben der Kinder es
            os.utime(path, (mtime, mtime))

    def file(self, dent, parent: str, rel: str, arel: str) -> None:
        digest = hashlib.sha256()
        size = 0
        name, out = self._create(parent, dent.name, lambda p: open(p, "xb"), rel, arel)
        with out:
            if dent.id != EMPTY_ID:
                f = self.fs_mgr.load_seafile(self.repo.repo_id, self.repo.version, dent.id)
                for block_id in f.blocks:
                    data = self.block_mgr.load_block(self.repo.repo_id, self.repo.version, block_id)
                    if hashlib.sha1(data).hexdigest() != block_id:
                        raise ExportError(f"Block {block_id} von '{rel}{dent.name}' ist beschädigt")
                    out.write(data)
                    digest.update(data)
                    size += len(data)
                    self._progress(size)
                if size != f.size:
                    raise ExportError(f"'{rel}{dent.name}': {size} Bytes gelesen, erwartet {f.size}")
        if dent.mtime:
            os.utime(os.path.join(parent, name), (dent.mtime, dent.mtime))
        self.res.files += 1
        self.res.bytes += size
        self.res.checksums.append((f"{arel}{name}", digest.hexdigest()))
        self._progress()

    def _progress(self, current_file_bytes: int = 0) -> None:
        """Meldet sich spätestens alle PROGRESS_SECONDS, auch mitten in einer großen Datei."""
        if time.monotonic() - self.last_progress >= PROGRESS_SECONDS:
            self.last_progress = time.monotonic()
            log.info("  %s: bisher %d Dateien, %s", self.repo.name, self.res.files,
                     human(self.res.bytes + current_file_bytes))


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def portable_name(name: str, max_bytes: int = 200) -> str:
    """Name, der auch unter Windows/SMB und auf Band-Systemen gültig ist."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"[. ]+$", lambda m: "_" * len(m.group()), name)  # Punkt/Leerzeichen am Ende
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name.encode()) > max_bytes:
        root, ext = os.path.splitext(name)
        keep = max(max_bytes - len(ext.encode()), 1)
        name = root.encode()[:keep].decode(errors="ignore") + ext
    return name or "_"


def numbered_name(name: str, n: int) -> str:
    root, ext = os.path.splitext(name)
    return f"{root} ({n}){ext}"


def safe_name(name: str) -> str:
    """Ordnernamen für User und Bibliotheken: immer portabel."""
    return portable_name(name.strip())


def user_dir_name(user: User) -> str:
    """LDAP-UID, sonst die volle Kontakt-Mail (eindeutig auch über mehrere Domains)."""
    return safe_name(user.uid or user.contact_email or user.email)


def choose_user_dir(cfg: Config, user: User) -> str:
    """Ordner gleichnamig zum User; existiert er schon (z.B. noch nicht vom Band gelöscht), mit Datum."""
    base = user_dir_name(user)
    candidate = os.path.join(cfg.archive_dir, base)
    if not os.path.exists(candidate):
        return candidate
    stamp = datetime.now().strftime("%Y-%m-%d")
    candidate = os.path.join(cfg.archive_dir, f"{base}_{stamp}")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(cfg.archive_dir, f"{base}_{stamp}_{n}")
        n += 1
    return candidate


def place(exported: str, res: RepoResult, user_dir: str, previous: str | None = None) -> None:
    """Verschiebt den fertigen Export nach <userordner>/<name>.

    previous: Ordner eines früheren Exports derselben Bibliothek, wird ersetzt.
    """
    if previous and os.path.isdir(os.path.join(user_dir, previous)):
        dest = os.path.join(user_dir, previous)
        shutil.rmtree(dest)
    else:
        name = safe_name(res.repo.name)
        dest = os.path.join(user_dir, name)
        if os.path.exists(dest):
            dest = os.path.join(user_dir, f"{name}_{res.repo.repo_id[:8]}")
        if os.path.exists(dest):
            shutil.rmtree(dest)  # Rest eines früheren Versuchs derselben Bibliothek
    os.rename(exported, dest)
    res.target = os.path.relpath(dest, user_dir)


def free_space_ok(cfg: Config, repos: list[Repo]) -> tuple[bool, int, int]:
    needed = int(sum(r.size or 0 for r in repos) * cfg.free_space_factor)
    st = os.statvfs(cfg.archive_dir)
    free = st.f_bavail * st.f_frsize
    return free >= needed, needed, free


def update_checksums(user_dir: str, results: list[RepoResult], replaced: set[str]) -> None:
    """Pflegt _SHA256SUMS: Einträge ersetzter Ordner raus, neue rein (Format von sha256sum)."""
    path = os.path.join(user_dir, CHECKSUMS)
    entries: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                digest, _, rel = line.rstrip("\n").partition("  ")
                if rel and not any(rel.startswith(p + "/") for p in replaced):
                    entries[rel] = digest
    for res in results:
        for rel, digest in res.checksums:
            entries[f"{res.target}/{rel}"] = digest
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(f"{entries[rel]}  {rel}\n" for rel in sorted(entries))
    os.replace(tmp, path)


def write_manifest(user_dir: str, user: User, repos: list[dict]) -> None:
    manifest = {
        "user": {
            "uid": user.uid,
            "contact_email": user.contact_email,
            "name": user.name,
            "seafile_id": user.email,
        },
        "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "Letzter Stand jeder Bibliothek, ohne Versionshistorie und Papierkorb.",
        "libraries": repos,
    }
    with open(os.path.join(user_dir, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def write_renamed(user_dir: str, entries: list[dict]) -> None:
    """Liste aller angepassten Namen (Original → Archiv); ohne Umbenennungen keine Datei."""
    path = os.path.join(user_dir, RENAMED)
    lines = []
    for e in entries:
        for original, archived in e.get("renamed") or []:
            lines.append(f"{e['folder']}/{original}\t→\t{e['folder']}/{archived}\n")
    if not lines:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Für das Archivziel angepasste Namen (Original → Name im Archiv)\n")
        f.writelines(sorted(lines))


def append_log(user_dir: str, res: RepoResult) -> None:
    r = res.repo
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [f"{stamp} {r.name} ({r.repo_id}) Commit {res.commit or '-'}: "
             f"{res.files} Dateien, {res.bytes} Bytes, {res.seconds:.1f} s, "
             + ("OK" if res.ok else "FEHLGESCHLAGEN")]
    lines += [f"    {p}" for p in res.problems]
    lines += [f"    umbenannt: {o} → {a}" for o, a in res.renamed]
    if res.ok and r.file_count is not None and r.file_count != res.files:
        # RepoFileCount wird asynchron gepflegt und kann nachhinken, deshalb nur ein Hinweis
        lines.append(f"    Hinweis: Seafile-Statistik meldet {r.file_count} Dateien")
    with open(os.path.join(user_dir, EXPORT_LOG), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def chown_tree(path: str, uid: int | None, gid: int | None) -> None:
    if uid is None and gid is None:
        return
    u = -1 if uid is None else uid
    g = -1 if gid is None else gid
    os.chown(path, u, g)
    for root, dirs, names in os.walk(path):
        for n in dirs + names:
            os.lchown(os.path.join(root, n), u, g)
