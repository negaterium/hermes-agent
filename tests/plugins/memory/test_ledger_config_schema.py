"""Configuration declaration tests for the exact-memory ledger."""

from plugins.memory.config_schema import get_provider_config_schema
from plugins.memory.ledger import LedgerMemoryProvider


def test_ledger_config_schema_is_profile_local_and_declared():
    schema = get_provider_config_schema("ledger")

    assert schema is not None
    assert schema.name == "ledger"
    assert {field.key for field in schema.fields} == {"db_path", "namespace"}
    assert "$HERMES_HOME" in next(field for field in schema.fields if field.key == "db_path").default


def test_ledger_save_config_preserves_existing_config_comments(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# preserve this operator note\nmodel:\n  default: test/model\n",
        encoding="utf-8",
    )

    LedgerMemoryProvider({}).save_config({"namespace": "test"}, str(tmp_path))

    content = config_path.read_text(encoding="utf-8-sig")
    assert "# preserve this operator note" in content
    assert "default: test/model" in content
    assert "namespace: test" in content
