from pathlib import Path


def test_env_example_uses_internal_pgbouncer_port() -> None:
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    content = env_example.read_text(encoding="utf-8")

    assert "APP_DATABASE_URL=postgresql+asyncpg://imaginv:imaginv@pgbouncer:5432/imaginv" in content
