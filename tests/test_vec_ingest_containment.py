"""Ingest path containment (Security-Review #154152, PR review blocking #1).

Pure Path logic — runs WITHOUT sqlite-vec/pypdf/ollama (no skips): the wall
must hold in every environment, degraded or not. $HERMES_HOME/.env, auth
files, ~/.ssh and .env* names refuse even when a configured root covers them;
everything outside the configured roots refuses; symlinks resolve before the
root containment decides.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_vecspec = importlib.util.spec_from_file_location(
    "memrack_backends_vec", _ROOT / "backends" / "vec.py")
_VEC = importlib.util.module_from_spec(_vecspec)
_vecspec.loader.exec_module(_VEC)
sys.modules["memrack_backends_vec"] = _VEC


@pytest.fixture
def vb(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    b = _VEC.VecBackend()
    b._provider_config = {}
    return b


def _roots(vb, *roots, wiki=None):
    if roots:
        vb._provider_config["vec_ingest_roots"] = ",".join(str(r) for r in roots)
    if wiki:
        vb._provider_config["wiki_root"] = str(wiki)


def test_hermes_home_env_file_refused(vb, tmp_path):
    env_file = tmp_path / "hermes-home" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SECRET=1")
    _roots(vb, tmp_path)   # even a root that COVERS hermes-home must not win
    with pytest.raises(PermissionError, match=r"HERMES_HOME"):
        vb._check_ingest_path(env_file)


def test_hermes_home_nested_refused(vb, tmp_path):
    _roots(vb, tmp_path)
    with pytest.raises(PermissionError, match=r"HERMES_HOME"):
        vb._check_ingest_path(tmp_path / "hermes-home" / "auth.json")


def test_ssh_dir_refused(vb, tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir(parents=True)
    monkeypatch.setattr(_VEC.Path, "home", lambda: home)
    _roots(vb, tmp_path)
    with pytest.raises(PermissionError, match=r"\.ssh"):
        vb._check_ingest_path(home / ".ssh" / "id_rsa")


def test_env_name_refused_anywhere(vb, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _roots(vb, tmp_path)
    with pytest.raises(PermissionError, match=r"\.env"):
        vb._check_ingest_path(project / ".env.local")


def test_outside_roots_refused(vb, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _roots(vb, root)
    with pytest.raises(PermissionError, match=r"outside the configured ingest roots"):
        vb._check_ingest_path(tmp_path / "elsewhere" / "notes.md")


def test_inside_root_allowed(vb, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    f = root / "notes.md"
    f.write_text("ok")
    _roots(vb, root)
    assert vb._check_ingest_path(f) == f.resolve()


def test_symlink_escape_refused(vb, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    secret = tmp_path / "outside" / "secret.md"        # real file OUTSIDE any root
    # make the escape unambiguous: the root list covers ONLY docs
    secret.parent.mkdir(parents=True)
    secret.write_text("secret")
    link = root / "innocent.md"
    link.symlink_to(secret)
    _roots(vb, root)
    with pytest.raises(PermissionError):
        vb._check_ingest_path(link)


def test_comma_string_and_wiki_root_config(vb, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _roots(vb, wiki=wiki)
    assert vb._check_ingest_path(wiki / "page.md") == (wiki / "page.md").resolve()
    vb._provider_config["vec_ingest_roots"] = str(docs)
    assert vb._check_ingest_path(docs / "x.md") == (docs / "x.md").resolve()
    assert [r.name for r in vb._ingest_roots()] == ["docs", "wiki"]


def test_default_root_is_home(vb, tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir(parents=True)
    monkeypatch.setattr(_VEC.Path, "home", lambda: home)
    assert vb._ingest_roots() == [home.resolve()]
    assert vb._check_ingest_path(home / "proj" / "file.md")   # allowed, wall-free


def test_tool_path_walls_fire_before_missing_file(vb, tmp_path):
    """The refusal must happen BEFORE the reader — a refused path never probes
    existence, so a prompt-injected path gets the permission error, not a
    FileNotFoundError leak of what exists."""
    secret = tmp_path / "hermes-home" / ".env.nonexistent"
    with pytest.raises(PermissionError):
        vb._check_ingest_path(secret)


def test_read_source_refuses_before_file_checks(vb, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _roots(vb, root)
    with pytest.raises(PermissionError, match=r"HERMES_HOME"):
        vb._read_source(tmp_path / "hermes-home" / "anything.md")
