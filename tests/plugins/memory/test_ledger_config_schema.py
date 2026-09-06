"""Configuration declaration tests for the exact-memory ledger."""

from plugins.memory.config_schema import get_provider_config_schema


def test_ledger_config_schema_is_profile_local_and_declared():
    schema = get_provider_config_schema("ledger")

    assert schema is not None
    assert schema.name == "ledger"
    assert {field.key for field in schema.fields} == {"db_path", "namespace"}
    assert "$HERMES_HOME" in next(field for field in schema.fields if field.key == "db_path").default
