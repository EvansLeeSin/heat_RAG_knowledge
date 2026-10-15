"""Checks for the local bootstrap; actual Docker validation is separate."""
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import MagicMock, patch

from app.models import IngestionJob
from tools import init_wsl_database


class WslBootstrapTests(unittest.TestCase):
    def test_ingestion_schema_includes_required_task_fields(self):
        columns = IngestionJob.__table__.columns
        for name in ('attempt_count', 'error_code', 'error_message', 'finished_at'):
            self.assertIn(name, columns)
        self.assertFalse(columns['attempt_count'].nullable)

    def test_repeated_bootstrap_is_schema_only_and_idempotent(self):
        engine = MagicMock()
        connection = engine.begin.return_value.__enter__.return_value
        with patch.object(init_wsl_database, 'get_engine', return_value=engine), \
             patch.object(init_wsl_database.Base.metadata, 'create_all') as create_all, \
             redirect_stdout(StringIO()):
            init_wsl_database.main()
            init_wsl_database.main()
        self.assertEqual(create_all.call_count, 2)
        create_all.assert_called_with(engine)
        statements = [str(c.args[0]).upper() for c in connection.execute.call_args_list]
        self.assertEqual(len(statements), 8)
        self.assertTrue(all('IF NOT EXISTS' in s for s in statements))
        self.assertTrue(all(s.startswith('CREATE EXTENSION') or s.startswith('CREATE INDEX') for s in statements))
        self.assertFalse(any('IVFFLAT' in s for s in statements))


if __name__ == '__main__':
    unittest.main()
