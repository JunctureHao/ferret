import unittest
from datetime import UTC, datetime
from pathlib import Path

from ferret.apps.session.models import (
    SessionMeta,
    SessionSource,
    SessionTableModel,
)


def make_session(session_id: str, name: str) -> SessionMeta:
    return SessionMeta(
        schema_version=1,
        session_id=session_id,
        name=name,
        path=Path(session_id),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        flow_count=1,
        file_size=10,
        source=SessionSource.CAPTURE,
    )


class SessionTableModelTests(unittest.TestCase):
    def test_update_session_replaces_row_when_session_id_changes(self) -> None:
        model = SessionTableModel()
        model.set_sessions([make_session("old.flow", "old")])

        renamed = make_session("new.flow", "new")
        model.update_session("old.flow", renamed)

        self.assertEqual(model.rowCount(), 1)
        self.assertIsNone(model.session_by_id("old.flow"))
        self.assertEqual(model.session_by_id("new.flow"), renamed)
        self.assertEqual(model.index(0, 0).data(), "new")


if __name__ == "__main__":
    unittest.main()
