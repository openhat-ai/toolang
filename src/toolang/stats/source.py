"""Source views exclude execution facts imported from another instance."""

import sqlite3


def prepare_source(connection: sqlite3.Connection) -> None:
    imported = connection.execute(
        "SELECT 1 FROM src.sqlite_master WHERE name='imported_runs'"
    ).fetchone()
    predicate = (
        " WHERE id NOT IN (SELECT run FROM src.imported_runs)" if imported else ""
    )
    connection.execute(
        "CREATE TEMP VIEW local_runs AS SELECT * FROM src.runs" + predicate
    )
    predicate = (
        " WHERE NOT EXISTS (SELECT 1 FROM src.imported_runs i WHERE i.run=steps.run)"
        if imported
        else ""
    )
    connection.execute(
        "CREATE TEMP VIEW local_steps AS SELECT * FROM src.steps" + predicate
    )
