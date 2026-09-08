#!/usr/bin/env python3
"""Exercise FCOPY filesystem aliases against an isolated Oracle-mode database."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--psql", required=True, help="Path to IvorySQL psql")
    parser.add_argument("--workdir", required=True, type=Path,
                        help="Parent of temporary files, visible to the server")
    args = parser.parse_args()
    command = [args.psql, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"]

    def execute(statement, error=None):
        result = subprocess.run(command, input=statement + "\n", text=True,
                                capture_output=True, timeout=30, check=False)
        if error is None:
            if result.returncode != 0:
                raise AssertionError(result.stderr)
        elif result.returncode == 0 or error not in result.stderr:
            raise AssertionError(f"Expected {error}: {result.stderr}")
        return result.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="ivory-fcopy-", dir=args.workdir) as name:
        directory = Path(name).resolve()
        registry = directory.name
        registered = sql_literal(registry)
        execute("INSERT INTO sys.utl_file_directory(dirname, dir) VALUES "
                f"({registered}, {sql_literal(directory)});")
        try:
            source = directory / "source"
            destination = directory / "destination"
            payload = b"first\x00line\n" + b"z" * 131072 + b"\nlast\x00line"
            source.write_bytes(payload)

            def copy(target, start=1, end=2147483647, error=None):
                return execute("SELECT sys.ora_utl_file_fcopy("
                               f"{registered}, 'source', {registered}, "
                               f"{sql_literal(target)}, {start}, {end});", error)

            # Compare actual bytes, including the final unterminated line.
            copy("destination")
            assert destination.read_bytes() == payload
            destination.write_bytes(payload * 2)
            copy("destination", 3, 3)
            assert destination.read_bytes() == b"last\x00line"
            assert source.read_bytes() == payload
            print("PASS: byte preservation and destination truncation")

            # Distinct pathnames for the same object must never destroy it.
            hardlink = directory / "hardlink"
            os.link(source, hardlink)
            copy("hardlink", error="Source and destination refer to the same file.")
            assert source.read_bytes() == payload
            assert hardlink.read_bytes() == payload
            print("PASS: hard-link identity")

            symlink = directory / "symlink"
            try:
                symlink.symlink_to(source.name)
            except OSError as error:
                print(f"SKIP: symbolic link unavailable: {error}")
            else:
                copy("symlink", error="Source and destination refer to the same file.")
                assert source.read_bytes() == payload
                print("PASS: symbolic-link identity")

            # Repeated caught errors use one backend and subtransactions.
            # Checking the detail prevents unrelated open failures from passing.
            execute(f"""DO $$
DECLARE
    detail text;
BEGIN
    FOR i IN 1..600 LOOP
        BEGIN
            PERFORM sys.ora_utl_file_fcopy({registered}, 'source',
                {registered}, 'hardlink');
            RAISE EXCEPTION 'FCOPY unexpectedly succeeded';
        EXCEPTION WHEN raise_exception THEN
            GET STACKED DIAGNOSTICS detail = PG_EXCEPTION_DETAIL;
            IF detail IS DISTINCT FROM 'Source and destination refer to the same file.' THEN
                RAISE;
            END IF;
        END;
    END LOOP;
    PERFORM sys.ora_utl_file_fcopy({registered}, 'source',
        {registered}, 'destination');
END;
$$;
/
""")
            assert source.read_bytes() == payload
            assert destination.read_bytes() == payload
            print("PASS: 600 subtransaction errors and subsequent copy")

            # A missing source must leave an existing destination untouched.
            destination.write_bytes(b"keep destination")
            execute("SELECT sys.ora_utl_file_fcopy("
                    f"{registered}, 'missing', {registered}, 'destination');",
                    error="INVALID_PATH")
            assert destination.read_bytes() == b"keep destination"
            print("PASS: source open failure preserves destination")

            subdir = directory / "subdir"
            subdir.mkdir()
            copy("subdir", error="INVALID_OPERATION")
            assert source.read_bytes() == payload
            print("PASS: destination open failure preserves source")

            if os.name != "nt":
                execute("SELECT sys.ora_utl_file_fcopy("
                        f"{registered}, 'subdir', {registered}, 'destination');",
                        error="READ_ERROR")
                print("PASS: directory read failure is reported")

            # A sparse file gives us a long line without allocating 1 GiB of RAM.
            # Skip the line so cancellation never writes a large destination.
            with source.open("wb") as stream:
                stream.truncate(1024 * 1024 * 1024)
            statements = ["\\set ON_ERROR_STOP off", "SET statement_timeout = '2ms';"]
            for _ in range(10):
                statements.extend([
                    "SELECT sys.ora_utl_file_fcopy("
                    f"{registered}, 'source', {registered}, 'destination', 2, 2);",
                    "\\echo cancel_state=:SQLSTATE",
                ])
            statements.extend(["SET statement_timeout = 0;", "SELECT 'connection_alive';"])
            result = execute("\n".join(statements))
            assert result.count("cancel_state=57014") == 10, result
            assert "connection_alive" in result, result
            assert source.stat().st_size == 1024 * 1024 * 1024
            source.write_bytes(payload)
            copy("destination")
            assert destination.read_bytes() == payload
            print("PASS: ten cancellations while skipping a long line")
        finally:
            execute("DELETE FROM sys.utl_file_directory WHERE dirname = "
                    f"{registered};")


if __name__ == "__main__":
    main()
