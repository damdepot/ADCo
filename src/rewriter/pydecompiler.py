"""Python database application to SQL queries decompiler."""

import argparse
import ast
import os
import re


def decompile_app(input_dir: str, output_dir: str) -> None:
    db_app_path = os.path.join(input_dir, "db_app.py")
    if not os.path.isfile(db_app_path):
        print(f"ERROR: db_app.py not found in {input_dir}")
        return

    with open(db_app_path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"ERROR: Failed to parse db_app.py: {e}")
        return

    # Find DatabaseClient class definition
    client_class = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DatabaseClient":
            client_class = node
            break

    # If DatabaseClient is not found, search the top-level tree body as a fallback
    nodes_to_search = client_class.body if client_class else tree.body

    os.makedirs(output_dir, exist_ok=True)
    queries_decompiled = 0

    for node in nodes_to_search:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    match = re.match(r"^QUERY_(\d+)$", name)
                    if match:
                        q_num = match.group(1)
                        # Extract the string constant value
                        val = node.value
                        sql_text = None
                        if isinstance(val, ast.Constant) and isinstance(val.value, str):
                            sql_text = val.value
                        # Support older python versions where ast.Str was used
                        elif isinstance(val, ast.Str):
                            sql_text = val.s

                        if sql_text is not None:
                            out_path = os.path.join(output_dir, f"query{q_num}-1.sql")
                            with open(out_path, "w", encoding="utf-8") as out_f:
                                out_f.write(sql_text + "\n")
                            queries_decompiled += 1

    print(f"Successfully decompiled {queries_decompiled} queries to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Decompile db_app.py back into query*.sql files")
    parser.add_argument("--input-dir", required=True, help="Directory containing db_app.py")
    parser.add_argument("--output-dir", required=True, help="Directory where query*.sql files will be recovered")
    args = parser.parse_args()

    decompile_app(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
