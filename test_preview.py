#!/usr/bin/env python3
"""report 预览流程（--preview / preview_csv）的可重复回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
样例库，用例结束后重新只读打开源库核对表结构与全部数据未变，
并核对预览调用没有新增报告或临时文件，全部临时文件随之清理。

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
from contextlib import redirect_stdout

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
# 同时包含中文、逗号与双引号，用于验证 CSV 解析后完整还原
NOTE_VALUE = '中文,含"引号"'

# 参数化查询：按姓名筛选，备注中的 NULL 由 --null-text 标记替换
PARAM_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id WHERE p.name = :who"
)
NULL_MARKER = "未填写"

# 合法 SELECT，但 fetchall 求值第二项结果时 SQLite 报整数溢出
OVERFLOW_SQL = "SELECT 1 AS 数值 UNION ALL SELECT abs(-9223372036854775808)"
OVERFLOW_REASON = "integer overflow"
SQL_EXEC_FAIL_REASON = "SQL 执行失败"


class PreviewTestCase(unittest.TestCase):
    """preview_csv 函数与 --preview 命令行入口的正常路径与失败约定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 带开头一个 BOM 的 UTF-8 参数化查询文件，供文件入口使用
        self.sql_file = os.path.join(self.tmpdir, "查询.sql")
        with open(self.sql_file, "wb") as f:
            f.write(b"\xef\xbb\xbf" + PARAM_SQL.encode("utf-8"))
        with open(self.sql_file, "rb") as f:
            self._sql_file_bytes = f.read()
        # 准备完成时的结构、数据与目录内容基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()
        self._files_baseline = self._list_files()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新只读打开源库，
        # 核对两表结构及全部数据与准备完成时完全一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        # 预览不创建报告或临时文件：临时目录内容与基线一致
        self.assertEqual(self._list_files(), self._files_baseline)
        # 查询文件只被读取，字节不变
        with open(self.sql_file, "rb") as f:
            self.assertEqual(f.read(), self._sql_file_bytes)
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
            # 两人的备注分别为 SQL NULL 与含逗号、双引号的中文
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

    def _list_files(self):
        """临时目录内全部文件的相对路径列表（排序）。"""
        found = []
        for root, _dirs, files in os.walk(self.tmpdir):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.tmpdir))
        return sorted(found)

    @staticmethod
    def _parse_csv(text):
        return list(csv.reader(io.StringIO(text)))

    def _run_preview(self, sql, limit, params=None, null_text=""):
        """调用 preview_csv 并捕获其写入标准输出的文本。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = report.preview_csv(
                self.db_path, sql, limit, params=params, null_text=null_text
            )
        return result, buf.getvalue()

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # -- 函数入口：正常路径 ------------------------------------------------

    def test_function_preview_one_row_returns_one(self):
        result, text = self._run_preview(JOIN_SQL, 1)

        self.assertEqual(result, 1)
        # 表头依次为 姓名、备注；只输出小明与空备注（NULL 默认空字段）
        self.assertEqual(self._parse_csv(text), [["姓名", "备注"], ["小明", ""]])

    def test_function_limit_above_result_count_returns_two(self):
        result, text = self._run_preview(JOIN_SQL, 10)

        self.assertEqual(result, 2)
        rows = self._parse_csv(text)
        self.assertEqual(rows[0], ["姓名", "备注"])
        self.assertEqual(rows[1], ["小明", ""])
        # 中文、逗号、引号按 CSV 解析后完整还原为一个字段
        self.assertEqual(rows[2], ["小红", NOTE_VALUE])
        self.assertEqual(len(rows), 3)

    def test_function_zero_rows_keeps_header_and_returns_zero(self):
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id WHERE p.id < 0"
        )
        result, text = self._run_preview(sql, 5)

        self.assertEqual(result, 0)
        # 零行结果只保留表头
        self.assertEqual(self._parse_csv(text), [["姓名", "备注"]])

    def test_function_descending_with_limit_one_shows_xiaohong(self):
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id "
            "ORDER BY p.id DESC LIMIT 1"
        )
        result, text = self._run_preview(sql, 10)

        self.assertEqual(result, 1)
        # 降序且带 LIMIT 1：只显示小红，排序与 LIMIT 语义保留
        self.assertEqual(
            self._parse_csv(text), [["姓名", "备注"], ["小红", NOTE_VALUE]]
        )

    def test_function_param_query_with_null_marker(self):
        result, text = self._run_preview(
            PARAM_SQL, 5, params={"who": "小明"}, null_text=NULL_MARKER
        )

        self.assertEqual(result, 1)
        self.assertEqual(
            self._parse_csv(text), [["姓名", "备注"], ["小明", NULL_MARKER]]
        )

    # -- 函数入口：行数非法一律 ValueError，标准输出为空 --------------------

    def test_function_invalid_limit_raises_valueerror_empty_stdout(self):
        for bad_limit in (0, -1, True, "2", 2.5):
            with self.subTest(limit=bad_limit):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(ValueError):
                        report.preview_csv(self.db_path, JOIN_SQL, bad_limit)
                # 失败路径标准输出为空，不留下表头或部分数据
                self.assertEqual(buf.getvalue(), "")

    # -- 函数入口：结果求值失败 --------------------------------------------

    def test_function_overflow_raises_valueerror_empty_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                # 即使只预览一行，全部结果求值失败同样拒绝
                report.preview_csv(self.db_path, OVERFLOW_SQL, 1)
        message = str(ctx.exception)
        self.assertIn(SQL_EXEC_FAIL_REASON, message)
        self.assertIn(OVERFLOW_REASON, message)
        # 失败时标准输出为空：不留下表头或部分数据
        self.assertEqual(buf.getvalue(), "")

    # -- 命令行入口：正常路径 ------------------------------------------------

    def test_cli_preview_one_row_exit_zero_csv_only(self):
        proc = self._run_cli("--sql", JOIN_SQL, "--preview", "1")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 标准错误为空，标准输出仅为 CSV，不追加成功提示
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        self.assertEqual(
            self._parse_csv(proc.stdout), [["姓名", "备注"], ["小明", ""]]
        )

    def test_cli_default_format_matches_explicit_csv(self):
        proc_default = self._run_cli("--sql", JOIN_SQL, "--preview", "2")
        proc_explicit = self._run_cli(
            "--sql", JOIN_SQL, "--preview", "2", "--format", "csv"
        )

        self.assertEqual(proc_default.returncode, 0, proc_default.stderr)
        self.assertEqual(proc_explicit.returncode, 0, proc_explicit.stderr)
        self.assertEqual(proc_default.stderr, "")
        self.assertEqual(proc_explicit.stderr, "")
        # 默认格式与显式 --format csv 的标准输出逐字节一致
        self.assertEqual(proc_default.stdout, proc_explicit.stdout)
        self.assertEqual(
            self._parse_csv(proc_explicit.stdout),
            [["姓名", "备注"], ["小明", ""], ["小红", NOTE_VALUE]],
        )

    def test_cli_param_query_via_sql_and_bom_file_same_result(self):
        expected = [["姓名", "备注"], ["小明", NULL_MARKER]]
        common = ["--param", "who=小明", "--null-text", NULL_MARKER, "--preview", "5"]

        proc_sql = self._run_cli("--sql", PARAM_SQL, *common)
        self.assertEqual(proc_sql.returncode, 0, proc_sql.stderr)
        self.assertEqual(proc_sql.stderr, "")
        self.assertEqual(self._parse_csv(proc_sql.stdout), expected)

        # 带开头一个 BOM 的 UTF-8 查询文件提供相同参数化查询
        proc_file = self._run_cli("--sql-file", self.sql_file, *common)
        self.assertEqual(proc_file.returncode, 0, proc_file.stderr)
        self.assertEqual(proc_file.stderr, "")
        self.assertEqual(proc_file.stdout, proc_sql.stdout)
        self.assertEqual(self._parse_csv(proc_file.stdout), expected)

        # 查询文件字节不变
        with open(self.sql_file, "rb") as f:
            self.assertEqual(f.read(), self._sql_file_bytes)

    # -- 命令行入口：行数非法一律退出 1 --------------------------------------

    def test_cli_invalid_preview_count_exit_one(self):
        cases = [
            ("缺值", ["--preview"]),
            ("零", ["--preview", "0"]),
            ("负数", ["--preview", "-2"]),
            ("非整数", ["--preview", "abc"]),
        ]
        for label, preview_args in cases:
            with self.subTest(case=label):
                proc = self._run_cli("--sql", JOIN_SQL, *preview_args)

                self.assertEqual(proc.returncode, 1)
                # 标准错误包含错误原因，标准输出为空
                self.assertIn("错误", proc.stderr)
                self.assertEqual(proc.stdout, "")

    # -- 命令行入口：预览与导出选项互斥 --------------------------------------

    def test_cli_preview_with_output_rejected_no_file(self):
        out = os.path.join(self.tmpdir, "preview_out.csv")
        proc = self._run_cli(
            "--sql", JOIN_SQL, "--preview", "1", "--output", out
        )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 不创建输出文件
        self.assertFalse(os.path.exists(out))

    def test_cli_preview_with_format_html_rejected(self):
        proc = self._run_cli(
            "--sql", JOIN_SQL, "--preview", "1", "--format", "html"
        )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_cli_preview_with_description_rejected(self):
        for label, text in [("非空说明", "筛选口径"), ("空说明", "")]:
            with self.subTest(case=label):
                proc = self._run_cli(
                    "--sql", JOIN_SQL, "--preview", "1", "--description", text
                )

                self.assertEqual(proc.returncode, 1)
                self.assertIn("错误", proc.stderr)
                self.assertEqual(proc.stdout, "")

    # -- 命令行入口：结果求值失败 --------------------------------------------

    def test_cli_overflow_exit_one_stderr_reason_empty_stdout(self):
        proc = self._run_cli("--sql", OVERFLOW_SQL, "--preview", "1")

        self.assertEqual(proc.returncode, 1)
        # 标准错误包含 SQL 执行失败与底层整数溢出原因
        self.assertIn(SQL_EXEC_FAIL_REASON, proc.stderr)
        self.assertIn(OVERFLOW_REASON, proc.stderr)
        # 标准输出为空：不留下表头或部分数据
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
