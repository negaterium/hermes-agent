"""Declarative configuration for the exact-memory ledger provider."""

from plugins.memory.config_schema import (
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="ledger",
    label="Exact Memory Ledger",
    fields=(
        ProviderField(
            key="db_path",
            label="SQLite database path",
            kind=KIND_TEXT,
            default="$HERMES_HOME/exact-memory/ledger.sqlite3",
            description="Profile-local ledger database. Paths outside the active Hermes home are rejected.",
            inline=True,
        ),
        ProviderField(
            key="namespace",
            label="Namespace override",
            kind=KIND_TEXT,
            default="default",
            description="Normally the active Hermes profile name; use an override only when deliberately isolating a ledger namespace.",
        ),
    ),
)
