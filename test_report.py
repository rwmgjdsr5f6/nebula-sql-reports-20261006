#!/usr/bin/env python3
"""report.export_csv / report.py 命令行的回归测试。

只依赖 Python 标准库（unittest、sqlite3、csv、subprocess、tempfile 等）。
在项目根目录执行 `python -m unittest discover` 即可发现并运行：
成功时退出码为 0，断言失败时以非零退出码结束并报告失败用例。

每个用例都在独立临时目录中自建小型 SQLite 库，结束后自动清理，
可重复执行，不依赖仓库中的任何预置文件。
"""

import csv
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import report

# report.py 与本测试文件同处项目根目录
REPORT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.py")

# 准备数据时使用的固定内容（含中文、NULL、逗号与双引号）
PEOPLE_ROWS = [(1, "小明"), (2, "小红")]
NOTES_ROWS = [(1, None), (2, '中文,含"引号"')]

JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
EXPECTED_HEADER = ["姓名", "备注"]
EXPECTED_DATA_ROWS = [["小明", ""], ['小红', '中文,含"引号"']]


class ReportCsvTests(unittest.TestCase):
    def setUp(self):
        # 每个用例独立的临时目录，库与输出文件都放在其中
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._build_database()
        # 准备完成时的数据库状态快照，tearDown 中逐一核对
        self._db_snapshot = self._read_database_state()

    def tearDown(self):
        # 无论成功或失败用例，结束后都重新打开源库核对：
        # 两表结构与全部数据必须与准备完成时完全一致（只读导出不得改动源库）。
        try:
            self.assertEqual(
                self._read_database_state(),
                self._db_snapshot,
                "导出后源数据库的结构或数据发生了变化",
            )
        finally:
            self._tmp.cleanup()

    # ---------- 辅助方法 ----------

    def _build_database(self):
        """在临时目录创建 people/notes 两表并写入固定样例数据。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            conn.execute("CREATE TABLE notes (person_id INTEGER NOT NULL, note TEXT)")
            conn.executemany("INSERT INTO people (id, name) VALUES (?, ?)", PEOPLE_ROWS)
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)", NOTES_ROWS
            )
            conn.commit()
        finally:
            conn.close()

    def _read_database_state(self):
        """重新以只读视角读取源库，记录两表结构与全部数据。"""
        conn = sqlite3.connect(self.db_path)
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            state = {"tables": tables}
            for table in ("people", "notes"):
                state[table + "_columns"] = [
                    row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)
                ]
            state["people_rows"] = conn.execute(
                "SELECT id, name FROM people ORDER BY id"
            ).fetchall()
            state["notes_rows"] = conn.execute(
                "SELECT person_id, note FROM notes ORDER BY person_id"
            ).fetchall()
            return state
        finally:
            conn.close()

    def _output_path(self, name="out.csv"):
        return os.path.join(self.tmpdir, name)

    def _read_csv_rows(self, path):
        """以 UTF-8 读取 CSV 并解析为行列表。"""
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    # ---------- export_csv 直接入口 ----------

    def test_join_export_returns_two_rows_and_quoted_content_stays_one_field(self):
        output = self._output_path("join.csv")
        count = report.export_csv(self.db_path, JOIN_SQL, output)

        self.assertEqual(count, 2)
        self.assertTrue(os.path.isfile(output))

        rows = self._read_csv_rows(output)
        self.assertEqual(rows[0], EXPECTED_HEADER)
        self.assertEqual(len(rows), 3)  # 表头 + 两行数据
        self.assertEqual(rows[1:], EXPECTED_DATA_ROWS)
        # NULL 成为空字段；含逗号、双引号的备注仍是同一个完整字段
        self.assertEqual(rows[1][1], "")
        self.assertEqual(rows[2][1], '中文,含"引号"')

    def test_empty_result_returns_zero_with_header_only(self):
        output = self._output_path("empty.csv")
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id "
            "WHERE p.id > 100 ORDER BY p.id"
        )
        count = report.export_csv(self.db_path, sql, output)

        self.assertEqual(count, 0)
        rows = self._read_csv_rows(output)
        self.assertEqual(rows, [EXPECTED_HEADER])  # 仅表头，没有数据记录

    def test_invalid_sql_raises_value_error_and_creates_no_file(self):
        output = self._output_path("invalid.csv")
        self.assertFalse(os.path.exists(output))

        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, "SELECT FROM people", output)

        self.assertFalse(
            os.path.exists(output), "SQL 被拒绝时不得创建目标文件"
        )

    def test_existing_target_raises_value_error_and_bytes_unchanged(self):
        output = self._output_path("existing.csv")
        original_bytes = "既存内容，不得覆盖\n".encode("utf-8")
        with open(output, "wb") as f:
            f.write(original_bytes)

        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, JOIN_SQL, output)

        with open(output, "rb") as f:
            self.assertEqual(
                f.read(), original_bytes, "目标已存在时其原始字节必须保持不变"
            )

    # ---------- 命令行入口 ----------

    def _run_cli(self, sql, output):
        return subprocess.run(
            [
                sys.executable,
                REPORT_SCRIPT,
                "--db",
                self.db_path,
                "--sql",
                sql,
                "--output",
                output,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_cli_success_exits_zero_reports_row_count_and_writes_file(self):
        output = self._output_path("cli_ok.csv")
        proc = self._run_cli(JOIN_SQL, output)

        self.assertEqual(
            proc.returncode,
            0,
            "正常导出应退出 0，stderr=%r" % proc.stderr.decode("utf-8", "replace"),
        )
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("2", stdout)
        self.assertIn("已导出 2 行数据", stdout)

        rows = self._read_csv_rows(output)
        self.assertEqual(rows[0], EXPECTED_HEADER)
        self.assertEqual(rows[1:], EXPECTED_DATA_ROWS)

    def test_cli_existing_target_exits_one_and_preserves_file(self):
        output = self._output_path("cli_existing.csv")
        original_bytes = b"CLI existing target must be kept intact\n"
        with open(output, "wb") as f:
            f.write(original_bytes)

        proc = self._run_cli(JOIN_SQL, output)

        self.assertEqual(proc.returncode, 1)
        stderr = proc.stderr.decode("utf-8")
        self.assertIn("目标已存在", stderr)

        with open(output, "rb") as f:
            self.assertEqual(f.read(), original_bytes)


if __name__ == "__main__":
    unittest.main()
