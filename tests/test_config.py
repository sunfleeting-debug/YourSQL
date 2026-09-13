"""运行环境配置的最小回归测试。"""

from pathlib import Path

from yoursql.common.config import RuntimeConfig, load_dotenv


def test_load_dotenv_keeps_existing_environment(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("YOURSQL_PORT=9100\nYOURSQL_LEGACY_ANONYMOUS=true\n", encoding="utf-8")
    monkeypatch.setenv("YOURSQL_PORT", "9200")
    monkeypatch.delenv("YOURSQL_LEGACY_ANONYMOUS", raising=False)

    load_dotenv(path)
    config = RuntimeConfig.from_environment()

    assert config.port == 9200
    assert config.legacy_anonymous


def test_runtime_config_reads_query_limits(monkeypatch) -> None:
    monkeypatch.setenv("YOURSQL_MAX_SQL_CHARS", "2048")
    monkeypatch.setenv("YOURSQL_MAX_STATEMENTS", "4")
    monkeypatch.setenv("YOURSQL_TRACE_MAX_STEPS", "")

    config = RuntimeConfig.from_environment()

    assert config.max_sql_chars == 2048
    assert config.max_statements == 4
    assert config.trace_max_steps is None
