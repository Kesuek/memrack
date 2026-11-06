"""memrack SDK package — sugar for backend authors (T-015).

Docking does NOT require this package: any duck-typed module exposing
export_backend() with the contract members works (registry + conformance
validate the STRUCTURE, not the inheritance). The SDK base class exists for
authors who want the fts5/lock/degrade lessons pre-built.
"""

from memrack.sdk import MemrackBackend, fts_match_tokens   # noqa: F401
from memrack.conformance import check_backend, assert_conformance   # noqa: F401

__all__ = ["MemrackBackend", "fts_match_tokens", "check_backend", "assert_conformance"]
