"""Embedder endpoint routing (provider reuse, no hard wiring).

The vec embedder must resolve from the memrack provider config: default =
local Ollama exactly as shipped; `openai` style = any OpenAI-compatible
embeddings endpoint with base_url + an env-var KEY NAME (never the key itself
in config). Pure logic + fake HTTP — no real endpoints, no env leaks.
"""
import importlib.util
import io
import json
import pathlib
import sys
import urllib.error

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_vecspec = importlib.util.spec_from_file_location(
    "memrack_backends_vec", _ROOT / "backends" / "vec.py")
_VEC = importlib.util.module_from_spec(_vecspec)
_vecspec.loader.exec_module(_VEC)


def _vb(cfg=None):
    b = _VEC.VecBackend()
    b._provider_config = dict(cfg or {})
    return b


def _fake_urlopen(monkeypatch, payload=None, status=None):
    """Patch urllib.request.urlopen inside the vec module for probes AND embeds."""
    def handler(req, timeout=None):
        class R:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        if status is not None:
            url = getattr(req, "full_url", req)
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO())
        return R(json.dumps(payload or {}).encode())
    monkeypatch.setattr("urllib.request.urlopen", handler)
    return handler


def _captured(monkeypatch, payload):
    seen = {}

    def handler(req, timeout=None):
        seen["url"] = getattr(req, "full_url", req)
        seen["headers"] = dict(getattr(req, "headers", None) or {})
        data = getattr(req, "data", None)
        seen["body"] = json.loads(data.decode()) if data else None

        class R:
            def __enter__(self):
                return self
            def read(self):
                return json.dumps(payload).encode()
            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr("urllib.request.urlopen", handler)
    return seen


# ── style resolution ──

def test_default_style_is_ollama():
    assert _vb()._resolve_embedder() == ("ollama", "", "")
    assert _vb({"embedding_endpoint": ""})._resolve_embedder()[0] == "ollama"


def test_unknown_style_falls_back_to_ollama():
    assert _vb({"embedding_endpoint": "anthropic"})._resolve_embedder()[0] == "ollama"
    assert _vb({"embedding_endpoint": " OpenAI "})._resolve_embedder()[0] == "openai"


def test_style_keeps_base_and_key_env():
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1/",
              "embedding_api_key_env": "NOUS_API_KEY"})
    style, base, env = vb._resolve_embedder()
    assert (style, base, env) == ("openai", "https://x.example/v1", "NOUS_API_KEY")


# ── api key resolution ──

def test_api_key_reads_named_env_var(monkeypatch):
    monkeypatch.delenv("VEC_TEST_KEY", raising=False)
    monkeypatch.setenv("VEC_TEST_KEY", "sk-test")
    assert _vb()._api_key("VEC_TEST_KEY") == "sk-test"
    assert _vb()._api_key("") == ""            # no env configured → no key header
    monkeypatch.delenv("VEC_TEST_KEY")
    assert _vb()._api_key("VEC_TEST_KEY") == ""


# ── ollama path unchanged ──

def test_ollama_probe_unchanged(tmp_path, monkeypatch):
    vb = _vb()
    seen = _captured(monkeypatch, {"models": [{"name": "nomic-embed-text"}]})
    monkeypatch.setattr(vb, "_model", "nomic-embed-text")
    ok = vb._probe()
    assert ok is True, "gap=%r" % vb._vec_gap
    assert seen["url"].endswith("/api/tags")


def test_ollama_embed_calls_api_embed(monkeypatch):
    vb = _vb()
    seen = _captured(monkeypatch, {"embeddings": [[0.1, 0.2]]})
    assert vb._embed(["hi"]) == [[0.1, 0.2]]
    assert seen["url"].endswith("/api/embed")


# ── openai style ──

def test_openai_embed_posts_embeddings_with_bearer(monkeypatch):
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1",
              "embedding_api_key_env": "VEC_TEST_KEY"})
    monkeypatch.setenv("VEC_TEST_KEY", "sk-live")
    seen = _captured(monkeypatch, {"data": [{"embedding": [1, 2]}, {"embedding": [3, 4]}]})
    assert vb._embed(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    assert seen["url"] == "https://x.example/v1/embeddings"
    assert seen["headers"].get("Authorization") == "Bearer sk-live"
    assert seen["body"]["input"] == ["a", "b"]


def test_openai_embed_missing_vector_fails_loud(monkeypatch):
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1"})
    _captured(monkeypatch, {"data": [{}]})
    with pytest.raises(RuntimeError, match="no embedding vector"):
        vb._embed(["a"])


def test_openai_probe_without_base_url_degrades_honest():
    vb = _vb({"embedding_endpoint": "openai"})
    assert vb._probe() is False
    assert "embedding_base_url" in vb._vec_gap


def test_openai_probe_401_names_the_key_env(monkeypatch):
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1",
              "embedding_api_key_env": "VEC_TEST_KEY"})
    _fake_urlopen(monkeypatch, status=401)
    assert vb._probe() is False
    assert "401" in vb._vec_gap and "VEC_TEST_KEY" in vb._vec_gap


def test_openai_probe_200_ready_without_model_gate(monkeypatch):
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1"})
    _captured(monkeypatch, {"data": []})   # model id NOT in the list — still ready
    ok = vb._probe()
    assert ok is True, "gap=%r" % vb._vec_gap
    assert vb._vec_gap == ""


def test_openai_probe_unreachable_degrades_honest(monkeypatch):
    vb = _vb({"embedding_endpoint": "openai",
              "embedding_base_url": "https://x.example/v1"})
    def boom(req, timeout=None):
        raise OSError("no route")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert vb._probe() is False
    assert "not reachable" in vb._vec_gap
