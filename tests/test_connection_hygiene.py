"""Every SQLite connection must be closed, not left to the garbage collector.

On Python 3.13+ a connection finalized while still open raises an unraisable
exception, which surfaces as a test failure once warnings are errors. It was
also a real leak: the server kept one connection per worker thread for the life
of the process, and no CLI command closed the one it opened.

These tests find leaks by inspecting live objects, so they fail on any Python
version rather than only the ones that complain.
"""

import gc
import sqlite3

import pytest

from mcp_militarypay import cli, server


def open_connections() -> list[sqlite3.Connection]:
    """Every sqlite3 connection still alive and usable."""
    alive = []
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.Error):
                continue  # already closed
            alive.append(obj)
    return alive


@pytest.fixture(autouse=True)
def _collect():
    gc.collect()
    yield
    gc.collect()


class TestCli:
    @pytest.mark.parametrize(
        "command",
        [["verify"], ["status"], ["notes"],
         ["lookup", "--grade", "E-5", "--years", "4"]],
    )
    def test_read_only_commands_close_their_connection(self, db_path, command, capsys):
        before = len(open_connections())
        cli.main(["--db", str(db_path), *command])
        capsys.readouterr()
        assert len(open_connections()) == before

    def test_ingest_closes_its_connection(self, tmp_path, enlisted_html, capsys):
        fixture = tmp_path / "enlisted.html"
        fixture.write_text(enlisted_html)
        before = len(open_connections())
        cli.main(["--db", str(tmp_path / "new.sqlite3"), "ingest",
                  "--base-pay", "enlisted", "--from-file", str(fixture)])
        capsys.readouterr()
        assert len(open_connections()) == before

    def test_a_failing_command_still_closes(self, db_path, capsys):
        before = len(open_connections())
        cli.main(["--db", str(db_path), "lookup", "--grade", "E-5", "--zip", "99999"])
        capsys.readouterr()
        assert len(open_connections()) == before


class TestServer:
    def test_connections_are_registered_so_they_can_be_closed(self, db_path):
        server.configure(db_path)
        try:
            server.get_connection()
            assert len(server._connections) == 1
        finally:
            server.configure(None)

    def test_reconfigure_closes_what_it_replaces(self, db_path):
        server.configure(db_path)
        connection = server.get_connection()
        server.configure(None)
        assert server._connections == []
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_a_connection_opened_on_one_thread_closes_from_another(self, db_path):
        """The reason connections are opened with check_same_thread=False:
        FastMCP dispatches on worker threads that then exit, and their
        connections must still be closable."""
        import threading

        server.configure(db_path)
        try:
            opened: list[sqlite3.Connection] = []
            worker = threading.Thread(target=lambda: opened.append(server.get_connection()))
            worker.start()
            worker.join()
            assert opened and opened[0] in server._connections

            server._close_all_connections()   # from the main thread
            with pytest.raises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")
        finally:
            server.configure(None)

    def test_serving_leaves_nothing_open(self, db_path):
        server.configure(db_path)
        server.get_connection()
        server.configure(None)
        assert server._connections == []
