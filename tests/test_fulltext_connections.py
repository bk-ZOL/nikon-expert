"""Regression tests for thread-owned FTS connection initialization."""
import concurrent.futures
import importlib.util
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


def load_fulltext():
    spec = importlib.util.spec_from_file_location(
        'fulltext_connection_test', Path(__file__).parents[1] / 'src/fulltext.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConnectionTests(unittest.TestCase):
    def test_initialization_keeps_connection_on_its_owner_thread(self):
        module = load_fulltext()
        barrier = threading.Barrier(8)

        class Connection:
            def __init__(self):
                self.owner = threading.get_ident()

            def execute(self, sql):
                assert self.owner == threading.get_ident(), 'connection crossed threads'
                if 'VIRTUAL' in sql:
                    barrier.wait(timeout=10)

            def commit(self):
                assert self.owner == threading.get_ident(), 'commit crossed threads'

            def close(self):
                pass

        def worker(_):
            connection = module.init_fts(':memory:')
            self.assertIs(connection, module._tls.conn)
            self.assertEqual(connection.owner, threading.get_ident())

        with patch.object(module, 'sqlite3', types.SimpleNamespace(connect=lambda *a, **k: Connection())):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(worker, range(8)))

    def test_failed_initialization_closes_connection_and_allows_retry(self):
        module = load_fulltext()
        bad = unittest.mock.Mock()
        bad.execute.side_effect = RuntimeError('initialization failed')
        good = unittest.mock.Mock()
        with patch.object(module, 'sqlite3', types.SimpleNamespace(connect=unittest.mock.Mock(side_effect=[bad, good]))):
            with self.assertRaisesRegex(RuntimeError, 'initialization failed'):
                module.init_fts(':memory:')
            bad.close.assert_called_once()
            self.assertFalse(hasattr(module._tls, 'conn'))
            self.assertIs(module._get_conn(), good)
            good.commit.assert_called_once()

    def test_concurrent_initialization_and_writes_do_not_leave_locks(self):
        module = load_fulltext()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'fts.db')
            module.init_fts(path).close()
            del module._tls.conn

            def worker(i):
                connection = module.init_fts(path)
                try:
                    self.assertIs(connection, module._tls.conn)
                    module.ingest_fts(str(i), 'test', 'test', '', '', 'concurrency regression')
                    self.assertFalse(connection.in_transaction)
                finally:
                    connection.close()
                    del module._tls.conn

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(worker, range(60)))
            connection = module.init_fts(path)
            try:
                self.assertEqual(connection.execute('SELECT count(*) FROM doc_fts').fetchone()[0], 60)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
