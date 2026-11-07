"""Declarative config surface for the memrack desktop panel (gear → Plugins → memrack).

Pure data by contract: loaded BY PATH (never imported as a package), may import only
``plugins.memory.config_schema``. Storage = ``flat_json`` — the desktop PUT writes
``<hermes_home>/memrack/config.json`` (atomic 0600, partial saves touch only submitted
keys). The provider reads BOTH stores: ``config.yaml`` ``plugins.memrack`` first,
flat_json keys win (the newest user intent). The CLI wizard surface
(``get_config_schema`` on the provider class) carries the same fields.
"""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SELECT,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
    STORAGE_FLAT_JSON,
)

_RACK_COMBOS = (
    ("base-db",),
    ("base-db", "state-episodic"),
    ("base-db", "wiki"),
    ("base-db", "state-episodic", "wiki"),
)

_RACK_LABELS = {
    "base-db": "base-db only (stdlib floor)",
    "base-db,state-episodic": "base-db + state-episodic (session history)",
    "base-db,wiki": "base-db + wiki (markdown bridge)",
    "base-db,state-episodic,wiki": "full rack (all three)",
}


def _rack_options() -> tuple[ProviderFieldOption, ...]:
    return tuple(
        ProviderFieldOption(value=",".join(c), label=_RACK_LABELS[",".join(c)])
        for c in _RACK_COMBOS
    )


CONFIG_SCHEMA = ProviderConfigSchema(
    name="memrack",
    label="memrack",
    storage=STORAGE_FLAT_JSON,
    fields=(
        ProviderField(
            key="backend", label="Force backend", kind=KIND_SELECT, default="",
            description="Force ONE backend for ALL routes (migration/transition override)",
            options=(
                ProviderFieldOption(value="", label="no force (route by kind)"),
                ProviderFieldOption(value="base-db", label="base-db"),
                ProviderFieldOption(value="wiki", label="wiki"),
            ),
            inline=True,
        ),
        ProviderField(
            key="enabled_backends", label="Rack membership", kind=KIND_SELECT,
            default="base-db,state-episodic",
            description="Which built-ins are in the rack (on/off per backend)",
            options=_rack_options(),
            inline=True,
        ),
        ProviderField(
            key="auto_enable", label="Auto-enable at activation", kind=KIND_BOOL,
            default="false",
            description="Enable base-db + wiki automatically on activation (default off)",
            inline=True,
        ),
        ProviderField(
            key="search_top_k", label="Search top_k", kind=KIND_NUMBER, default="8",
            description="Default result count for memrack_search (max 32)",
            inline=True,
        ),
        ProviderField(
            key="embedding_model", label="Embedding model", kind=KIND_TEXT,
            default="nomic-embed-text", group="vec",
            description="Ollama embedding model for the optional vec engine",
        ),
        ProviderField(
            key="ollama_host", label="Ollama host", kind=KIND_TEXT,
            default="http://127.0.0.1:11434", group="vec",
            description="Ollama endpoint for vec embeddings",
        ),
    ),
)
