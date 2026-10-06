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

# 单条 SELECT 输入边界的可重复样例：字符串内含分号与注释标记，
# 列名也含分号；这些分号不得被当作语句分隔，引号内的注释标记不得被删去
SAMPLE_SQL = "SELECT '中文;--/*备注*/' AS \"列;名\""
SAMPLE_HEADER = "列;名"
SAMPLE_VALUE = "中文;--/*备注*/"


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

    # -- 单条 SELECT 输入边界：允许的形式 -------------------------------

    def _assert_sample_csv(self, path):
        """核对样例导出：恰好表头一行加数据一行，均为一列且内容完整。"""
        text, rows = self._read_csv(path)
        self.assertEqual(rows, [[SAMPLE_HEADER], [SAMPLE_VALUE]])
        # 每行恰好一列：字符串与列名中的分号未被误判为多条语句
        self.assertTrue(all(len(row) == 1 for row in rows))
        # 原文层面完整保留中文与注释标记字符
        self.assertIn(SAMPLE_VALUE, text)

    def test_leading_trailing_whitespace_accepted(self):
        out = os.path.join(self.tmpdir, "ws.csv")
        count = report.export_csv(self.db_path, " \t\n  SELECT 1 AS 值  \n\t ", out)

        self.assertEqual(count, 1)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["值"], ["1"]])

    def test_select_keyword_case_variations_accepted(self):
        for i, keyword in enumerate(["select", "SELECT", "SeLeCt"]):
            with self.subTest(keyword=keyword):
                out = os.path.join(self.tmpdir, "case_%d.csv" % i)
                count = report.export_csv(
                    self.db_path, "%s 1 AS 值" % keyword, out
                )
                self.assertEqual(count, 1)
                _, rows = self._read_csv(out)
                self.assertEqual(rows, [["值"], ["1"]])

    def test_single_trailing_semicolon_accepted(self):
        out = os.path.join(self.tmpdir, "semicolon.csv")
        count = report.export_csv(self.db_path, SAMPLE_SQL + ";", out)

        self.assertEqual(count, 1)
        self._assert_sample_csv(out)

    def test_comments_with_semicolons_around_statement_accepted(self):
        # 语句前后的行注释与块注释都含分号，不得被当作语句分隔
        sql = (
            "-- 前置行注释;含分号;--/*\n"
            "/* 前置块注释;含;分号 */\n"
            + SAMPLE_SQL + ";\n"
            "/* 后置块注释;含;分号 */\n"
            "-- 后置行注释;含分号"
        )
        out = os.path.join(self.tmpdir, "comments.csv")
        count = report.export_csv(self.db_path, sql, out)

        self.assertEqual(count, 1)
        self._assert_sample_csv(out)

    def test_block_comment_between_keyword_and_expression_acts_as_space(self):
        # SELECT 与表达式之间的块注释与普通空白分隔效果一致
        sql = "SELECT/*分隔*/'中文;--/*备注*/' AS \"列;名\""
        out = os.path.join(self.tmpdir, "block_as_space.csv")
        count = report.export_csv(self.db_path, sql, out)

        self.assertEqual(count, 1)
        self._assert_sample_csv(out)

    # -- 单条 SELECT 输入边界：拒绝的形式 -------------------------------

    def test_block_comment_cannot_join_keyword_fragments(self):
        # 去除注释后不得把 SE 与 LECT 拼成合法关键词
        out = os.path.join(self.tmpdir, "joined_keyword.csv")
        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, "SE/*分隔*/LECT 1", out)
        self.assertFalse(os.path.exists(out))

    def test_rejected_inputs_raise_valueerror_and_create_no_file(self):
        cases = {
            "empty_string": "",
            "blank_whitespace": " \t\n ",
            "comment_only": "-- 只有行注释;含分号\n/* 块注释;含分号 */",
            "leading_semicolon": ";SELECT 1",
            "double_trailing_semicolon": "SELECT 1;;",
            "two_selects": "SELECT 1; SELECT 2",
            "select_then_update": "SELECT 1; UPDATE people SET name='篡改' WHERE id=1",
            "with_cte": "WITH x AS (SELECT 1) SELECT * FROM x",
            "pragma": "PRAGMA table_info(people)",
            "explain": "EXPLAIN SELECT 1",
            "update_only": "UPDATE people SET name='篡改' WHERE id=1",
        }
        for name, sql in cases.items():
            with self.subTest(name=name):
                # 每个被拒绝的输入使用独立输出路径
                out = os.path.join(self.tmpdir, "reject_%s.csv" % name)
                with self.assertRaises(ValueError):
                    report.export_csv(self.db_path, sql, out)
                # 拒绝路径不创建目标文件
                self.assertFalse(os.path.exists(out))

        # 重新只读打开源库：两表结构与全部数据仍与准备完成时一致
        self.assertEqual(self._snapshot_db(), self._baseline)

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

    def test_cli_single_select_with_comments_exits_zero(self):
        out = os.path.join(self.tmpdir, "cli_sample.csv")
        sql = "-- 前置注释;含分号\n" + SAMPLE_SQL + ";"
        proc = self._run_cli(sql, out)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 标准输出报告导出一行
        self.assertIn("1", proc.stdout)
        self.assertIn("行", proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [[SAMPLE_HEADER], [SAMPLE_VALUE]])

    def test_cli_multi_statement_rejected_with_exit_code_one(self):
        out = os.path.join(self.tmpdir, "cli_multi.csv")
        proc = self._run_cli("SELECT 1; SELECT 2", out)

        self.assertEqual(proc.returncode, 1)
        # 原因写到标准错误，标准输出不出现成功提示
        self.assertIn("错误", proc.stderr)
        self.assertNotIn("已导出", proc.stdout)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
