import errno
import hashlib
from types import SimpleNamespace as NS

import pytest

from seafile_archiver import export
from seafile_archiver.seafile import Repo

REPO = Repo("r1", "Lib", False, None, None, "c1")


@pytest.mark.parametrize("name, expected", [
    ("normal.txt", "normal.txt"),
    ("a:b?.txt", "a_b_.txt"),
    ("a/b\\c", "a_b_c"),
    ("punkt.", "punkt_"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("..", "__"),
    ("", "_"),
])
def test_portable_name(name, expected):
    assert export.portable_name(name) == expected


def test_portable_name_kuerzt_auf_bytes_und_behaelt_endung():
    name = export.portable_name("ä" * 300 + ".txt")
    assert len(name.encode()) <= 200
    assert name.endswith(".txt")


def walker():
    res = export.RepoResult(repo=REPO)
    return export._Walker(REPO, None, None, res), res


def test_doppelpunkt_wird_nie_unveraendert_versucht(tmp_path):
    w, res = walker()
    tried = []

    def make(path):
        tried.append(path.rsplit("/", 1)[1])

    name, _ = w._create(str(tmp_path), "a:b.txt", make, "", "")
    assert (name, tried) == ("a_b.txt", ["a_b.txt"])
    assert res.renamed == [("a:b.txt", "a_b.txt")]


def test_kollision_wird_nummeriert(tmp_path):
    (tmp_path / "x.txt").write_text("alt")
    w, res = walker()
    name, out = w._create(str(tmp_path), "x.txt", lambda p: open(p, "xb"), "", "")
    out.close()
    assert name == "x (2).txt"
    assert (tmp_path / "x.txt").read_text() == "alt"


def test_volle_platte_ist_kein_namensproblem(tmp_path):
    w, res = walker()

    def make(path):
        raise OSError(errno.ENOSPC, "voll")

    with pytest.raises(OSError) as e:
        w._create(str(tmp_path), "x.txt", make, "", "")
    assert e.value.errno == errno.ENOSPC
    assert res.renamed == []


def fake_seafobj(monkeypatch, block_id, data, size):
    dent = NS(name="d.txt", id="f" * 40, mtime=1000)
    root = NS(get_files_list=lambda: [dent], get_subdirs_list=lambda: [])
    managers = (
        NS(load_commit=lambda *a: NS(commit_id="c1", root_id="1" * 40, ctime=1000)),
        NS(load_seafdir=lambda *a: root, load_seafile=lambda *a: NS(blocks=[block_id], size=size)),
        NS(load_block=lambda *a: data),
    )
    monkeypatch.setattr(export, "_seafobj", lambda cfg: managers)


def test_export_schreibt_datei_und_pruefsumme(tmp_path, monkeypatch):
    fake_seafobj(monkeypatch, hashlib.sha1(b"hallo").hexdigest(), b"hallo", 5)
    res = export.export_repo(None, REPO, str(tmp_path / "out"))
    assert res.ok
    assert (tmp_path / "out" / "d.txt").read_bytes() == b"hallo"
    assert res.checksums == [("d.txt", hashlib.sha256(b"hallo").hexdigest())]


@pytest.mark.parametrize("block_id, size", [
    ("0" * 40, 5),                                  # beschädigter Block
    (hashlib.sha1(b"hallo").hexdigest(), 6),        # Datei zu kurz
])
def test_beschaedigte_daten_lassen_export_scheitern(tmp_path, monkeypatch, block_id, size):
    fake_seafobj(monkeypatch, block_id, b"hallo", size)
    assert not export.export_repo(None, REPO, str(tmp_path / "out")).ok


def test_verschluesselt_laut_commit_scheitert(tmp_path, monkeypatch):
    commit = NS(commit_id="c1", encrypted="true")
    monkeypatch.setattr(export, "_seafobj", lambda cfg: (NS(load_commit=lambda *a: commit), None, None))
    assert not export.export_repo(None, REPO, str(tmp_path / "out")).ok
