#!/usr/bin/env python3
"""report.export_csv 查询到 CSV 路径的可重复回归测试。

只依赖 Python 标准库；在临时目录中自行准备小型 SQLite 库，
用例结束后清理全部临时文件，不依赖仓库中的任何预置数据文件。

在项目根目录执行：
    python -m unittest discover
"""

import csv
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
# 同时包含中文、逗号与双引号，用于验证 CSV 字段引用
NOTE_VALUE = '中文,含"引号"'


class ReportTestCase(unittest.TestCase):
    """直接调用 export_csv 的行为测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新只读打开源库，
        # 核对两表结构及全部数据与准备完成时完全一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE people ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE notes ("
                "person_id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                [(1, "小明"), (2, "小红")],
            )
            # 两人的备注分别为 NULL 与含逗号、双引号的中文
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_VALUE)],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以只读方式重新读取源库的表结构与两表全部数据。"""
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            people = conn.execute(
                "SELECT id, name FROM people ORDER BY id"
            ).fetchall()
            notes = conn.execute(
                "SELECT person_id, note FROM notes ORDER BY person_id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "people": people, "notes": notes}

    @staticmethod
    def _read_csv(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return text, list(csv.reader(io.StringIO(text)))

    # -- 正常导出 --------------------------------------------------------

    def test_join_export_returns_two_rows_with_expected_csv(self):
        out = os.path.join(self.tmpdir, "out.csv")
        count = report.export_csv(self.db_path, JOIN_SQL, out)

        self.assertEqual(count, 2)
        self.assertTrue(os.path.isfile(out))

        text, rows = self._read_csv(out)
        # UTF-8 读取后表头依次为 姓名、备注
        self.assertEqual(rows[0], ["姓名", "备注"])
        # 两行内容及次序与样例一致；NULL 成为空字段
        self.assertEqual(rows[1], ["小明", ""])
        # 逗号和双引号仍属于一个完整字段（整行恰好 2 列）
        self.assertEqual(rows[2], ["小红", NOTE_VALUE])
        self.assertEqual(len(rows), 3)
        # CSV 原文层面：含逗号/引号的字段被整体加引号，内部引号双写
        self.assertIn('"中文,含""引号"""', text)

    # -- 筛选不到记录 ----------------------------------------------------

    def test_empty_result_returns_zero_with_header_only(self):
        out = os.path.join(self.tmpdir, "empty.csv")
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id "
            "WHERE p.id < 0"
        )
        count = report.export_csv(self.db_path, sql, out)

        self.assertEqual(count, 0)
        _, rows = self._read_csv(out)
        # 仍输出同样的表头，但没有任何数据记录
        self.assertEqual(rows, [["姓名", "备注"]])

    # -- SQL 错误 --------------------------------------------------------

    def test_invalid_sql_raises_valueerror_and_creates_no_file(self):
        out = os.path.join(self.tmpdir, "never.csv")
        self.assertFalse(os.path.exists(out))

        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, "SELECT FROM people", out)

        # 拒绝路径不创建目标文件
        self.assertFalse(os.path.exists(out))

    # -- 目标已存在 ------------------------------------------------------

    def test_existing_target_raises_and_original_bytes_unchanged(self):
        out = os.path.join(self.tmpdir, "existing.csv")
        original_bytes = b"original \xe5\x8e\x9f\xe5\xa7\x8b bytes \xff\x00\n"
        with open(out, "wb") as f:
            f.write(original_bytes)

        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, JOIN_SQL, out)

        with open(out, "rb") as f:
            self.assertEqual(f.read(), original_bytes)

    # -- 命令行入口 ------------------------------------------------------

    def _run_cli(self, sql, output):
        return subprocess.run(
            [
                sys.executable,
                REPORT_PY,
                "--db",
                self.db_path,
                "--sql",
                sql,
                "--output",
                output,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_cli_success_exits_zero_and_reports_row_count(self):
        out = os.path.join(self.tmpdir, "cli_out.csv")
        proc = self._run_cli(JOIN_SQL, out)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 标准输出报告数据行数
        self.assertIn("2", proc.stdout)
        self.assertIn("行", proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows[0], ["姓名", "备注"])
        self.assertEqual(len(rows), 3)

    def test_cli_existing_target_rejected_with_exit_code_one(self):
        out = os.path.join(self.tmpdir, "cli_existing.csv")
        original_bytes = "已有内容，不得变化\n".encode("utf-8")
        with open(out, "wb") as f:
            f.write(original_bytes)

        proc = self._run_cli(JOIN_SQL, out)

        self.assertEqual(proc.returncode, 1)
        # 标准错误说明目标已存在
        self.assertIn("已存在", proc.stderr)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), original_bytes)


if __name__ == "__main__":
    unittest.main()
