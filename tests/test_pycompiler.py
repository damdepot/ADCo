"""Tests for pycompiler and pydecompiler."""

import os
import tempfile
from pathlib import Path

from src.code_rewriter.pycompiler import compile_queries
from src.code_rewriter.pydecompiler import decompile_app


def test_compile_decompile_roundtrip():
    with tempfile.TemporaryDirectory() as temp_input:
        # Create temp query sql files
        q1_content = (
            "SELECT\n"
            "    COUNT(*)\n"
            "FROM\n"
            "    comments as c\n"
            "WHERE\n"
            "    c.Score = 0;"
        )
        q2_content = (
            "SELECT\n"
            "    *\n"
            "FROM\n"
            "    users\n"
            "LIMIT 10;"
        )
        
        Path(temp_input, "query1.sql").write_text(q1_content, encoding="utf-8")
        Path(temp_input, "query2.sql").write_text(q2_content, encoding="utf-8")
        Path(temp_input, "schema.sql").write_text("CREATE TABLE users (Id int);", encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_output:
            # 1. Compile
            compile_queries(temp_input, temp_output)
            
            # Check db_app.py exists and matches expectations
            db_app_path = Path(temp_output, "db_app.py")
            assert db_app_path.is_file()
            
            db_app_content = db_app_path.read_text(encoding="utf-8")
            assert "class DatabaseClient:" in db_app_content
            assert "QUERY_1" in db_app_content
            assert "QUERY_2" in db_app_content
            assert "schema.sql" not in db_app_content
            assert "def run_query1" in db_app_content
            assert "def run_query2" in db_app_content

            with tempfile.TemporaryDirectory() as temp_recovered:
                # 2. Decompile
                decompile_app(temp_output, temp_recovered)
                
                # Check query files are recovered correctly
                rec_q1_path = Path(temp_recovered, "query1.sql")
                rec_q2_path = Path(temp_recovered, "query2.sql")
                rec_schema_path = Path(temp_recovered, "schema.sql")

                assert rec_q1_path.is_file()
                assert rec_q2_path.is_file()
                assert not rec_schema_path.exists()

                # Verify contents are identical to originals
                assert rec_q1_path.read_text(encoding="utf-8").strip() == q1_content.strip()
                assert rec_q2_path.read_text(encoding="utf-8").strip() == q2_content.strip()
