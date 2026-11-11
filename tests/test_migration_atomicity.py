"""DB-02: сбой после ALTER откатывает основной этап миграции."""
import pytest

from database import migrations


async def test_main_migration_rollback_then_retry(db, monkeypatch):
    await db.execute("ALTER TABLE orders DROP COLUMN assignment_generation")
    await db.commit()
    original = migrations._run_ddl_script
    injected = False

    async def fail_after_alter(connection, script):
        nonlocal injected
        columns = await (await connection.execute("PRAGMA table_info(orders)")).fetchall()
        if "assignment_generation" in {row[1] for row in columns}:
            injected = True
            raise RuntimeError("synthetic migration interruption after ALTER")
        await original(connection, script)

    monkeypatch.setattr(migrations, "_run_ddl_script", fail_after_alter)
    with pytest.raises(RuntimeError):
        await migrations.run_migrations()
    assert injected
    columns = await (await db.execute("PRAGMA table_info(orders)")).fetchall()
    assert "assignment_generation" not in {row[1] for row in columns}
    monkeypatch.setattr(migrations, "_run_ddl_script", original)
    await migrations.run_migrations()
    await migrations.run_migrations()
    columns = await (await db.execute("PRAGMA table_info(orders)")).fetchall()
    assert "assignment_generation" in {row[1] for row in columns}
    assert (await (await db.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
