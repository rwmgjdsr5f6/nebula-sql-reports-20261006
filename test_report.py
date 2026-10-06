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
from unittest import mock

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)
# 同时包含中文、逗号与双引号，用于验证 CSV 字段引用
NOTE_VALUE = '中文,含"引号"'

# 单条 SELECT 边界回归的可重复样例：字面量与引号别名中同时含有分号和
# 注释标记，用于证明引号内文本既不参与多语句切分，也不会被注释剥离改动
BOUNDARY_VALUE = "中文;--/*备注*/"
BOUNDARY_HEADER = "列;名"
BOUNDARY_SQL = "SELECT '%s' AS \"%s\"" % (BOUNDARY_VALUE, BOUNDARY_HEADER)


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

    # -- 单条 SELECT 输入边界：允许的写法 --------------------------------

    def _assert_boundary_csv(self, out, expected_count=1):
        """核对边界样例 CSV：一行数据、表头与唯一字段完整保留。"""
        self.assertTrue(os.path.isfile(out))
        _, rows = self._read_csv(out)
        self.assertEqual(rows[0], [BOUNDARY_HEADER])
        self.assertEqual(rows[1], [BOUNDARY_VALUE])
        # 完整字段核对：恰好表头 + 一行数据，每行恰好一列
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row) == 1 for row in rows))
        # CSV 原文中引号内的注释标记与分号必须原样保留
        with open(out, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn(BOUNDARY_VALUE, text)

    def test_boundary_leading_and_trailing_whitespace(self):
        out = os.path.join(self.tmpdir, "boundary_ws.csv")
        count = report.export_csv(
            self.db_path, "  \t\n " + BOUNDARY_SQL + " \n\t  ", out
        )
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_lowercase_select_keyword(self):
        out = os.path.join(self.tmpdir, "boundary_lower.csv")
        count = report.export_csv(
            self.db_path,
            "  " + BOUNDARY_SQL.replace("SELECT", "select", 1) + "  ",
            out,
        )
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_mixed_case_select_keyword(self):
        out = os.path.join(self.tmpdir, "boundary_mixed.csv")
        count = report.export_csv(
            self.db_path,
            "\t SeLeCt '%s' AS \"%s\";\n" % (BOUNDARY_VALUE, BOUNDARY_HEADER),
            out,
        )
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_single_trailing_semicolon(self):
        out = os.path.join(self.tmpdir, "boundary_semicolon.csv")
        count = report.export_csv(self.db_path, BOUNDARY_SQL + ";", out)
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_line_comments_outside_statement(self):
        out = os.path.join(self.tmpdir, "boundary_line_comment.csv")
        # 注释里故意放入分号，不能被当成多语句切分
        sql = (
            "-- 前置行注释；含分号;\n"
            "  " + BOUNDARY_SQL + "  -- 尾随行注释；也含分号;\n"
        )
        count = report.export_csv(self.db_path, sql, out)
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_block_comments_outside_statement(self):
        out = os.path.join(self.tmpdir, "boundary_block_comment.csv")
        sql = (
            "/* 前置块注释；含分号 ; 与嵌套外观 -- */\n"
            + BOUNDARY_SQL
            + "\n/* 尾随块注释；含 ; 分号 */"
        )
        count = report.export_csv(self.db_path, sql, out)
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_block_comment_between_select_and_expression(self):
        # 关键字与表达式之间的块注释应与普通空白分隔效果一致
        out = os.path.join(self.tmpdir, "boundary_mid_block.csv")
        sql = "SELECT/* 分隔注释 ; */ '%s' AS \"%s\"" % (
            BOUNDARY_VALUE,
            BOUNDARY_HEADER,
        )
        count = report.export_csv(self.db_path, sql, out)
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_semicolons_inside_quotes_are_not_statement_breaks(self):
        out = os.path.join(self.tmpdir, "boundary_quoted_semicolons.csv")
        # 语句前后再各加一条含分号的注释，集中验证分号判定只认真实分隔符
        sql = (
            "-- ;前;注;释;\n"
            + BOUNDARY_SQL
            + " /* ;块;注;释; */ -- ;行;注;释;\n"
        )
        count = report.export_csv(self.db_path, sql, out)
        self.assertEqual(count, 1)
        self._assert_boundary_csv(out)

    def test_boundary_block_comment_inside_keyword_is_rejected(self):
        out = os.path.join(self.tmpdir, "boundary_split_keyword.csv")
        self.assertFalse(os.path.exists(out))
        # 去除注释不得把 SE/*分隔*/LECT 重新拼成 SELECT
        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, "SE/*分隔*/LECT 1", out)
        self.assertFalse(os.path.exists(out))

    # -- 单条 SELECT 输入边界：被拒绝的语句形式 --------------------------

    def _assert_rejected_without_file(self, sql):
        seq = getattr(self, "_reject_seq", 0) + 1
        self._reject_seq = seq
        out = os.path.join(self.tmpdir, "rejected_%02d.csv" % seq)
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, sql, out)
        # 拒绝路径绝不产生目标 CSV
        self.assertFalse(os.path.exists(out))

    def test_reject_blank_or_comment_only_inputs(self):
        for sql in ("", "   \n\t  ", "-- 只有一条行注释\n", "/* 只有块注释 */"):
            with self.subTest(sql=sql):
                self._assert_rejected_without_file(sql)

    def test_reject_leading_semicolon(self):
        self._assert_rejected_without_file("; " + BOUNDARY_SQL)

    def test_reject_two_trailing_semicolons(self):
        self._assert_rejected_without_file(BOUNDARY_SQL + ";;")

    def test_reject_two_select_statements(self):
        self._assert_rejected_without_file(
            BOUNDARY_SQL + "; SELECT 1 AS another"
        )

    def test_reject_select_followed_by_update(self):
        self._assert_rejected_without_file(
            "SELECT id, name FROM people; UPDATE people SET name='x' WHERE id=1"
        )

    def test_reject_with_pragma_explain_and_bare_update(self):
        for sql in (
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
            "PRAGMA table_info(people)",
            "EXPLAIN SELECT 1",
            "UPDATE people SET name='x' WHERE id=1",
        ):
            with self.subTest(sql=sql):
                self._assert_rejected_without_file(sql)

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

    def test_cli_legal_boundary_sample_exits_zero_and_reports_one_row(self):
        out = os.path.join(self.tmpdir, "cli_boundary.csv")
        sql = (
            "-- 前置注释；含分号;\n"
            + BOUNDARY_SQL
            + "; -- 尾随行注释；含分号;\n"
        )
        proc = self._run_cli(sql, out)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 成功提示报告恰为一行，并给出输出路径
        self.assertIn("1", proc.stdout)
        self.assertIn("行", proc.stdout)
        self.assertIn(out, proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [[BOUNDARY_HEADER], [BOUNDARY_VALUE]])

    def test_cli_multi_statement_rejected_exit_one_stderr_no_success(self):
        out = os.path.join(self.tmpdir, "cli_multi.csv")
        self.assertFalse(os.path.exists(out))
        proc = self._run_cli(BOUNDARY_SQL + "; SELECT 1 AS another", out)

        self.assertEqual(proc.returncode, 1)
        # 拒绝原因写到标准错误
        self.assertIn("错误", proc.stderr)
        self.assertIn("语句", proc.stderr)
        # 标准输出不出现成功提示，目标文件不产生
        self.assertNotIn("已导出", proc.stdout)
        self.assertFalse(os.path.exists(out))


# 普通文本伪装成数据库的固定输入：重复三十二次后加换行
INVALID_DB_BYTES = ("not a sqlite database\n" * 32).encode("ascii")
# 不访问任何业务表的常量查询：排除 SQL 本身成为失败原因
LITERAL_SQL = "SELECT 1 AS 数值"
# 源库打开失败的公开原因前缀（底层英文消息因 SQLite 版本而异，不作断言）
OPEN_FAIL_REASON = "无法以只读方式打开数据库"


class SourceOpenFailureTestCase(unittest.TestCase):
    """源数据库打开失败的回归测试：缺失文件与非数据库文件两种边界。

    每个用例独立准备临时目录；missing.sqlite 从不创建，invalid.sqlite
    为固定内容的普通文本文件。输出 CSV 的父目录即临时目录（已存在），
    目标文件名各不相同且事先不存在，保证失败只能源于源库打开阶段。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.invalid_db = os.path.join(self.tmpdir, "invalid.sqlite")
        with open(self.invalid_db, "wb") as f:
            f.write(INVALID_DB_BYTES)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_cli(self, db_path, output):
        return subprocess.run(
            [
                sys.executable,
                REPORT_PY,
                "--db",
                db_path,
                "--sql",
                LITERAL_SQL,
                "--output",
                output,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _assert_source_state_unchanged(self):
        """核对两种源库状态未被调用改变：缺失仍缺失，文本字节不变。"""
        self.assertFalse(os.path.exists(self.missing_db))
        with open(self.invalid_db, "rb") as f:
            self.assertEqual(f.read(), INVALID_DB_BYTES)

    def _assert_function_rejects(self, db_path, out):
        """直接调用 export_csv：抛 ValueError，公开原因与源路径齐全。"""
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(db_path, LITERAL_SQL, out)
        message = str(ctx.exception)
        self.assertIn(OPEN_FAIL_REASON, message)
        self.assertIn(db_path, message)
        # 拒绝路径不产生目标 CSV
        self.assertFalse(os.path.exists(out))

    def _assert_cli_rejects(self, db_path, out):
        """命令行入口：退出码 1，标准错误给出相同原因，标准输出为空。"""
        self.assertFalse(os.path.exists(out))
        proc = self._run_cli(db_path, out)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertIn(db_path, proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 拒绝路径不产生目标 CSV
        self.assertFalse(os.path.exists(out))

    # -- 源库文件缺失 ----------------------------------------------------

    def test_function_missing_db_raises_and_creates_nothing(self):
        out = os.path.join(self.tmpdir, "fn_missing.csv")
        self._assert_function_rejects(self.missing_db, out)
        # 只读打开绝不顺手创建缺失的源库
        self._assert_source_state_unchanged()

    def test_cli_missing_db_exit_one_stderr_reason_empty_stdout(self):
        out = os.path.join(self.tmpdir, "cli_missing.csv")
        self._assert_cli_rejects(self.missing_db, out)
        self._assert_source_state_unchanged()

    # -- 源库为普通文本文件 ------------------------------------------------

    def test_function_invalid_db_raises_and_file_bytes_unchanged(self):
        out = os.path.join(self.tmpdir, "fn_invalid.csv")
        # 查询不访问任何业务表，也必须在打开阶段就被拒绝
        self._assert_function_rejects(self.invalid_db, out)
        self._assert_source_state_unchanged()

    def test_cli_invalid_db_exit_one_stderr_reason_empty_stdout(self):
        out = os.path.join(self.tmpdir, "cli_invalid.csv")
        self._assert_cli_rejects(self.invalid_db, out)
        self._assert_source_state_unchanged()


class SqlFileTestCase(unittest.TestCase):
    """--sql-file 文件入口的回归测试：与 --sql 同规则、同结果。

    每个用例独立准备临时目录与 people/notes 样例库；查询文件写在临时
    目录内（含中文与空格的路径），用例结束后全部清理。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        self._baseline = self._snapshot_db()
        # 查询文件放在含中文与空格的子目录中，验证路径不做任何假设
        self.sql_dir = os.path.join(self.tmpdir, "查询 目录")
        os.mkdir(self.sql_dir)

    def tearDown(self):
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
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_VALUE)],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
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

    def _write_sql_file(self, data, name="查询 文件.sql"):
        """写查询文件（bytes 原样写，str 按 UTF-8 编码），返回路径。"""
        path = os.path.join(self.sql_dir, name)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _run_cli(self, output, sql=None, sql_file=None):
        cmd = [sys.executable, REPORT_PY, "--db", self.db_path]
        if sql is not None:
            cmd += ["--sql", sql]
        if sql_file is not None:
            cmd += ["--sql-file", sql_file]
        cmd += ["--output", output]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

    # -- 文件入口与直接输入等价 ------------------------------------------

    def test_sql_file_produces_same_csv_as_inline_sql(self):
        sql_path = self._write_sql_file(JOIN_SQL)
        out_file = os.path.join(self.tmpdir, "from_file.csv")
        out_inline = os.path.join(self.tmpdir, "from_inline.csv")

        proc = self._run_cli(out_file, sql_file=sql_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2", proc.stdout)
        self.assertIn(out_file, proc.stdout)

        proc_inline = self._run_cli(out_inline, sql=JOIN_SQL)
        self.assertEqual(proc_inline.returncode, 0, proc_inline.stderr)

        # 两种入口生成的 CSV 字节完全一致
        with open(out_file, "rb") as f:
            file_bytes = f.read()
        with open(out_inline, "rb") as f:
            self.assertEqual(file_bytes, f.read())

        with open(out_file, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(io.StringIO(f.read())))
        self.assertEqual(rows[0], ["姓名", "备注"])
        self.assertEqual(rows[1], ["小明", ""])
        self.assertEqual(rows[2], ["小红", NOTE_VALUE])
        self.assertEqual(len(rows), 3)

        # 查询文件只被读取，内容不变
        with open(sql_path, "rb") as f:
            self.assertEqual(f.read(), JOIN_SQL.encode("utf-8"))

    def test_sql_file_with_bom_and_comments_and_semicolon(self):
        content = (
            "-- 前置行注释；含分号;\n"
            "/* 块注释 ; 含分号 */\n"
            "  " + JOIN_SQL + "  -- 尾随行注释\n"
        )
        sql_path = self._write_sql_file(b"\xef\xbb\xbf" + content.encode("utf-8"))
        out = os.path.join(self.tmpdir, "bom.csv")

        proc = self._run_cli(out, sql_file=sql_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(io.StringIO(f.read())))
        self.assertEqual(rows[0], ["姓名", "备注"])
        self.assertEqual(len(rows), 3)

    def test_sql_file_empty_result_keeps_header_and_reports_zero(self):
        sql_path = self._write_sql_file(
            "SELECT p.name AS 姓名, n.note AS 备注 "
            "FROM people p JOIN notes n ON p.id=n.person_id WHERE p.id < 0"
        )
        out = os.path.join(self.tmpdir, "zero.csv")

        proc = self._run_cli(out, sql_file=sql_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("0", proc.stdout)
        with open(out, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(io.StringIO(f.read())))
        self.assertEqual(rows, [["姓名", "备注"]])

    # -- 文件内容沿用 SQL 校验规则 ----------------------------------------

    def _assert_file_rejected(self, content, reason_part=None):
        seq = getattr(self, "_reject_seq", 0) + 1
        self._reject_seq = seq
        sql_path = self._write_sql_file(content, name="拒绝%d.sql" % seq)
        out = os.path.join(self.tmpdir, "rejected_%02d.csv" % seq)
        self.assertFalse(os.path.exists(out))

        proc = self._run_cli(out, sql_file=sql_path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误: ", proc.stderr)
        if reason_part is not None:
            self.assertIn(reason_part, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_sql_file_blank_or_comment_only_rejected_as_empty_sql(self):
        for content in ("", "   \n\t  ", "-- 只有行注释\n", "/* 只有块注释 */"):
            with self.subTest(content=content):
                self._assert_file_rejected(content, "SQL 为空")

    def test_sql_file_disallowed_statements_rejected(self):
        for content in (
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
            "PRAGMA table_info(people)",
            "EXPLAIN SELECT 1",
            "UPDATE people SET name='x' WHERE id=1",
            JOIN_SQL + "; SELECT 1 AS another",
        ):
            with self.subTest(content=content):
                self._assert_file_rejected(content)

    # -- 查询文件读取失败 --------------------------------------------------

    def _assert_read_failure(self, sql_path, reason_part):
        out = os.path.join(self.tmpdir, "never_read_fail.csv")
        self.assertFalse(os.path.exists(out))

        proc = self._run_cli(out, sql_file=sql_path)
        self.assertEqual(proc.returncode, 1)
        # 标准错误包含错误前缀、查询文件路径与对应原因
        self.assertIn("错误: ", proc.stderr)
        self.assertIn(sql_path, proc.stderr)
        self.assertIn(reason_part, proc.stderr)
        # 标准输出为空，不创建输出
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_sql_file_missing_rejected(self):
        missing = os.path.join(self.sql_dir, "不存在.sql")
        self._assert_read_failure(missing, "不存在")

    def test_sql_file_directory_rejected(self):
        self._assert_read_failure(self.sql_dir, "目录")

    def test_sql_file_invalid_utf8_rejected(self):
        sql_path = self._write_sql_file(b"SELECT 1 \xff\xfe \xc3\x28")
        self._assert_read_failure(sql_path, "UTF-8")

    # -- 参数组合 ---------------------------------------------------------

    def _assert_arg_error(self, sql, sql_file):
        out = os.path.join(self.tmpdir, "never_arg.csv")
        self.assertFalse(os.path.exists(out))
        proc = self._run_cli(out, sql=sql, sql_file=sql_file)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误: ", proc.stderr)
        self.assertIn("参数错误", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_both_sql_and_sql_file_rejected(self):
        sql_path = self._write_sql_file(JOIN_SQL)
        self._assert_arg_error(JOIN_SQL, sql_path)

    def test_neither_sql_nor_sql_file_rejected(self):
        self._assert_arg_error(None, None)

    # -- 输出目标已存在 ----------------------------------------------------

    def test_sql_file_existing_target_rejected_bytes_unchanged(self):
        sql_path = self._write_sql_file(JOIN_SQL)
        out = os.path.join(self.tmpdir, "existing.csv")
        original_bytes = "已有内容，不得变化\n".encode("utf-8")
        with open(out, "wb") as f:
            f.write(original_bytes)

        proc = self._run_cli(out, sql_file=sql_path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("已存在", proc.stderr)
        self.assertEqual(proc.stdout, "")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), original_bytes)


# 写入故障的固定消息：可重复、与真实磁盘状态无关
WRITE_FAIL_MESSAGE = "模拟写入故障：磁盘写入被拒绝"
# 表头行落盘字节（csv 默认行尾为 \r\n），用于核对"表头已写入"时点
HEADER_LINE_BYTES = "姓名,备注\r\n".encode("utf-8")


class _FlakyFile:
    """包装真实文件对象：前 fail_at - 1 次 write 正常落盘，第 fail_at 次抛固定 OSError。

    用于在 export_csv 新建目标之后、按精确时点制造可重复的写入故障；
    正常接受的写入立即 flush，使故障瞬间的真实落盘内容可被核对。
    """

    def __init__(self, real_file, fail_at, on_fail):
        self._real = real_file
        self._fail_at = fail_at
        self._on_fail = on_fail
        self._writes = 0

    def write(self, data):
        self._writes += 1
        if self._writes == self._fail_at:
            self._on_fail()
            raise OSError(WRITE_FAIL_MESSAGE)
        self._real.write(data)
        self._real.flush()
        return len(data)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._real.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._real, name)


class WriteFailureCleanupTestCase(unittest.TestCase):
    """export_csv 新建目标后写入失败的清理回归测试。

    两个用例分别覆盖：目标已创建但表头尚未写成、表头已写入而数据
    尚未写完。故障统一为带固定消息的 OSError；每个用例核对失败前后
    的文件状态、错误文本、半成品清理、目录内其他文件不受影响，并在
    恢复正常条件后用同一源库、同一查询、同一输出路径重试成功导出。
    """

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

    def _assert_write_failure_then_retry(self, out_name, fail_at,
                                         expected_bytes_during):
        """在 fail_at 指定的写入时点注入故障，核对报错与清理后重试成功。"""
        out = os.path.join(self.tmpdir, out_name)
        # 目录内预置的其他文件：故障与清理都不得触及
        sibling = os.path.join(self.tmpdir, "预置 " + out_name + ".txt")
        sibling_bytes = ("预置文件，不得变化：%s\n" % out_name).encode("utf-8")
        with open(sibling, "wb") as f:
            f.write(sibling_bytes)

        # 失败前：输出父目录已存在，目标起初不存在
        self.assertTrue(os.path.isdir(self.tmpdir))
        self.assertFalse(os.path.exists(out))

        observed = {}

        def on_fail():
            # 故障发生瞬间：目标已被本调用创建，记录当时真实落盘内容
            observed["exists"] = os.path.exists(out)
            with open(out, "rb") as f:
                observed["bytes"] = f.read()

        real_open = open

        def flaky_open(path, mode="r", *args, **kwargs):
            f = real_open(path, mode, *args, **kwargs)
            if os.path.abspath(path) == os.path.abspath(out) and "x" in mode:
                return _FlakyFile(f, fail_at, on_fail)
            return f

        with mock.patch.object(report, "open", create=True, new=flaky_open):
            with self.assertRaises(ValueError) as ctx:
                report.export_csv(self.db_path, JOIN_SQL, out)

        # 调用结果为 ValueError：错误文本包含原因前缀、目标路径与原始故障消息
        message = str(ctx.exception)
        self.assertIn("无法写入输出文件", message)
        self.assertIn(out, message)
        self.assertIn(WRITE_FAIL_MESSAGE, message)

        # 故障发生瞬间的文件状态与预期写入时点一致
        self.assertTrue(observed["exists"])
        self.assertEqual(observed["bytes"], expected_bytes_during)

        # 调用结束后半成品已清除；目录保持可写，可正常新建并删除文件
        self.assertFalse(os.path.exists(out))
        probe = os.path.join(self.tmpdir, "probe.tmp")
        with open(probe, "wb") as f:
            f.write(b"probe")
        os.remove(probe)

        # 目录内其他预置文件的字节保持不变
        with open(sibling, "rb") as f:
            self.assertEqual(f.read(), sibling_bytes)

        # 恢复正常写入条件后，同一源库、同一查询、同一输出路径再次导出：
        # 失败未留下阻碍后续导出的目标文件
        count = report.export_csv(self.db_path, JOIN_SQL, out)
        self.assertEqual(count, 2)
        _, rows = self._read_csv(out)
        # CSV 保留查询列顺序；NULL 仍为空字段；中文、逗号与引号完整读回
        self.assertEqual(rows[0], ["姓名", "备注"])
        self.assertEqual(rows[1], ["小明", ""])
        self.assertEqual(rows[2], ["小红", NOTE_VALUE])
        self.assertEqual(len(rows), 3)

    def test_failure_before_header_removes_partial_and_retry_exports(self):
        # 第一次 write（表头）即失败：目标已创建但表头尚未写成
        self._assert_write_failure_then_retry(
            "fail_before_header.csv", fail_at=1, expected_bytes_during=b""
        )

    def test_failure_after_header_removes_partial_and_retry_exports(self):
        # 第二次 write（首行数据）失败：表头已写入而数据尚未写完
        self._assert_write_failure_then_retry(
            "fail_after_header.csv",
            fail_at=2,
            expected_bytes_during=HEADER_LINE_BYTES,
        )


# 带命名参数的关联查询：与验收用 query.sql 同形
JOIN_WHO_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id "
    "WHERE p.name = :who ORDER BY p.id"
)
# 注入形态的参数值：作为文本绑定时不应匹配任何记录
INJECTION_VALUE = "小红' OR 1=1 --"


class ParamTestCase(unittest.TestCase):
    """命名文本参数（--param / export_csv 的 params）的回归测试。

    每个用例独立准备临时目录与 people/notes 样例库；tearDown 核对源库
    结构与数据相对准备基线零变化。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        self._baseline = self._snapshot_db()

    def tearDown(self):
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
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_VALUE)],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
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

    def _run_cli(self, output, sql=None, sql_file=None, params=()):
        cmd = [sys.executable, REPORT_PY, "--db", self.db_path]
        if sql is not None:
            cmd += ["--sql", sql]
        if sql_file is not None:
            cmd += ["--sql-file", sql_file]
        for item in params:
            cmd += ["--param", item]
        cmd += ["--output", output]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

    # -- 参数筛选与注入安全 ------------------------------------------------

    def test_param_filters_to_one_row(self):
        out = os.path.join(self.tmpdir, "who.csv")
        count = report.export_csv(
            self.db_path, JOIN_WHO_SQL, out, params={"who": "小红"}
        )
        self.assertEqual(count, 1)
        _, rows = self._read_csv(out)
        # 表头与列顺序不变，只导出小红及其备注
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])

    def test_param_injection_text_matches_nothing(self):
        out = os.path.join(self.tmpdir, "injected.csv")
        count = report.export_csv(
            self.db_path, JOIN_WHO_SQL, out, params={"who": INJECTION_VALUE}
        )
        # 值只作为数据参与比较：注入形态文本匹配不到任何记录
        self.assertEqual(count, 0)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["姓名", "备注"]])

    def test_params_none_matches_plain_three_arg_call(self):
        out_plain = os.path.join(self.tmpdir, "plain.csv")
        out_none = os.path.join(self.tmpdir, "none.csv")
        self.assertEqual(report.export_csv(self.db_path, JOIN_SQL, out_plain), 2)
        self.assertEqual(
            report.export_csv(self.db_path, JOIN_SQL, out_none, params=None), 2
        )
        with open(out_plain, "rb") as f:
            plain_bytes = f.read()
        with open(out_none, "rb") as f:
            self.assertEqual(f.read(), plain_bytes)

    def test_empty_string_is_valid_value(self):
        out = os.path.join(self.tmpdir, "empty_value.csv")
        count = report.export_csv(
            self.db_path, JOIN_WHO_SQL, out, params={"who": ""}
        )
        self.assertEqual(count, 0)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["姓名", "备注"]])

    def test_values_are_not_type_converted(self):
        out = os.path.join(self.tmpdir, "typeof.csv")
        count = report.export_csv(
            self.db_path,
            "SELECT typeof(:a) AS ta, typeof(@b) AS tb, typeof($c) AS tc",
            out,
            params={"a": "123", "b": "null", "c": "true"},
        )
        self.assertEqual(count, 1)
        _, rows = self._read_csv(out)
        # 数字、null、布尔词一律按文本绑定
        self.assertEqual(rows, [["ta", "tb", "tc"], ["text", "text", "text"]])

    def test_three_prefix_styles_share_one_value(self):
        out = os.path.join(self.tmpdir, "prefixes.csv")
        count = report.export_csv(
            self.db_path,
            "SELECT :who AS a, @who AS b, $who AS c, :who AS d",
            out,
            params={"who": "小红"},
        )
        self.assertEqual(count, 1)
        _, rows = self._read_csv(out)
        # 重复引用同名参数使用同一值
        self.assertEqual(rows, [["a", "b", "c", "d"], ["小红"] * 4])

    def test_placeholder_like_text_in_string_and_comment_ignored(self):
        out = os.path.join(self.tmpdir, "literal.csv")
        sql = (
            "SELECT ':who' AS 冒号, '@who' AS 圈, '$who' AS 刀, '?' AS 问 "
            "-- 行注释里的 :who @who $who ? ?1 不算参数\n"
            "/* 块注释里的 :who ? 也不算 */"
        )
        # 未提供任何参数也不应报缺少参数
        count = report.export_csv(self.db_path, sql, out)
        self.assertEqual(count, 1)
        _, rows = self._read_csv(out)
        self.assertEqual(
            rows,
            [["冒号", "圈", "刀", "问"], [":who", "@who", "$who", "?"]],
        )

    def test_unreferenced_legal_param_ignored(self):
        out = os.path.join(self.tmpdir, "extra.csv")
        count = report.export_csv(
            self.db_path, JOIN_SQL, out, params={"unused": "值", "who": "小红"}
        )
        self.assertEqual(count, 2)

    def test_param_names_are_case_sensitive(self):
        out = os.path.join(self.tmpdir, "case.csv")
        sql = "SELECT :Who AS v"
        # Who 与 who 是两个不同参数
        with self.assertRaises(ValueError):
            report.export_csv(self.db_path, sql, out, params={"who": "x"})
        self.assertFalse(os.path.exists(out))
        count = report.export_csv(self.db_path, sql, out, params={"Who": "x"})
        self.assertEqual(count, 1)

    # -- 函数入口拒绝 ------------------------------------------------------

    def _assert_param_rejected(self, sql, params, reason_part=None):
        seq = getattr(self, "_reject_seq", 0) + 1
        self._reject_seq = seq
        out = os.path.join(self.tmpdir, "param_rejected_%02d.csv" % seq)
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, sql, out, params=params)
        if reason_part is not None:
            self.assertIn(reason_part, str(ctx.exception))
        # 拒绝路径绝不产生目标 CSV
        self.assertFalse(os.path.exists(out))

    def test_missing_required_param_rejected(self):
        # 完全不提供、提供空字典、提供其他名字，均为缺少必要参数
        for params in (None, {}, {"other": "x"}):
            with self.subTest(params=params):
                self._assert_param_rejected(JOIN_WHO_SQL, params, "who")

    def test_positional_placeholders_rejected(self):
        self._assert_param_rejected("SELECT ?", None, "位置占位符")
        self._assert_param_rejected("SELECT ?1", None, "位置占位符")
        self._assert_param_rejected(
            "SELECT * FROM people WHERE id = ?2", {"who": "x"}, "位置占位符"
        )

    def test_non_dict_params_rejected(self):
        for bad in ("who=小红", ["who", "小红"], 42):
            with self.subTest(params=bad):
                self._assert_param_rejected(JOIN_WHO_SQL, bad, "字符串字典")

    def test_non_string_value_rejected(self):
        for bad_value in (1, None, True, 1.5):
            with self.subTest(value=bad_value):
                self._assert_param_rejected(
                    JOIN_WHO_SQL, {"who": bad_value}, "必须是字符串"
                )

    def test_invalid_param_names_rejected(self):
        for bad_name in (
            "",
            "1who",
            "who x",
            "who-x",
            "谁",
            ":who",  # 字典键不带占位符前缀
            "@who",
            "$who",
        ):
            with self.subTest(name=bad_name):
                self._assert_param_rejected(
                    JOIN_WHO_SQL, {bad_name: "小红"}, "非法参数名"
                )

    def test_non_string_key_rejected(self):
        self._assert_param_rejected(JOIN_WHO_SQL, {1: "小红"}, "非法参数名")

    # -- 命令行入口 --------------------------------------------------------

    def test_cli_param_with_sql_file_filters_one_row(self):
        sql_path = os.path.join(self.tmpdir, "查询.sql")
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(JOIN_WHO_SQL + ";\n")
        out = os.path.join(self.tmpdir, "cli_who.csv")

        proc = self._run_cli(out, sql_file=sql_path, params=["who=小红"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1", proc.stdout)
        self.assertIn("行", proc.stdout)
        self.assertIn(out, proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])
        # 查询文件只被读取，内容不变
        with open(sql_path, "rb") as f:
            self.assertEqual(f.read(), (JOIN_WHO_SQL + ";\n").encode("utf-8"))

    def test_cli_param_with_inline_sql(self):
        out = os.path.join(self.tmpdir, "cli_inline.csv")
        proc = self._run_cli(out, sql=JOIN_WHO_SQL, params=["who=小明"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["姓名", "备注"], ["小明", ""]])

    def test_cli_injection_value_exports_header_only(self):
        out = os.path.join(self.tmpdir, "cli_injected.csv")
        proc = self._run_cli(out, sql=JOIN_WHO_SQL, params=["who=" + INJECTION_VALUE])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("0", proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, [["姓名", "备注"]])

    def test_cli_param_value_split_at_first_equals_only(self):
        out = os.path.join(self.tmpdir, "cli_value.csv")
        tricky = "a=b 中文 \"引号\";--"
        proc = self._run_cli(
            out, sql="SELECT :who AS v", params=["who=" + tricky]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, rows = self._read_csv(out)
        # 第一个等号后的全部内容（含等号、空格、中文、引号、分号）原样保留
        self.assertEqual(rows, [["v"], [tricky]])

    def test_cli_param_errors_exit_one_empty_stdout_no_file(self):
        cases = [
            ["who"],               # 缺少等号
            ["1who=x"],            # 非法名称
            ["who x=y"],           # 非法名称
            ["who=a", "who=b"],    # 重复提供同名选项
        ]
        for i, params in enumerate(cases):
            with self.subTest(params=params):
                out = os.path.join(self.tmpdir, "cli_bad_%d.csv" % i)
                proc = self._run_cli(out, sql=JOIN_WHO_SQL, params=params)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("错误", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertFalse(os.path.exists(out))

    def test_cli_missing_param_exit_one_empty_stdout_no_file(self):
        out = os.path.join(self.tmpdir, "cli_missing_param.csv")
        proc = self._run_cli(out, sql=JOIN_WHO_SQL)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误", proc.stderr)
        self.assertIn("who", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))

    def test_cli_positional_placeholder_exit_one_empty_stdout_no_file(self):
        out = os.path.join(self.tmpdir, "cli_positional.csv")
        proc = self._run_cli(
            out, sql="SELECT * FROM people WHERE id = ?", params=["who=1"]
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))


# --null-text 专项回归的固定样例：编号 1 至 4，备注依次为
# SQL NULL、空字符串、与标记同形的普通文本、普通中文
NULLTEXT_SQL = "SELECT id AS 编号, note AS 备注 FROM records ORDER BY id"
# 借助现有命名参数只选中 NULL 所在的记录
NULLTEXT_WHERE_SQL = (
    "SELECT id AS 编号, note AS 备注 FROM records "
    "WHERE id = :id ORDER BY id"
)
NULLTEXT_PLAIN_MARKER = "未填写"
# 同时含中文、首尾空格、逗号、双引号与换行：经 csv.reader 必须完整还原
NULLTEXT_COMPLEX_MARKER = ' 标记,含"引号"中文\n第二行 '
# 省略标记（默认空字段）时的完整期望：表头 + 四条数据
NULLTEXT_EXPECTED_DEFAULT = [
    ["编号", "备注"],
    ["1", ""],
    ["2", ""],
    ["3", "未填写"],
    ["4", "普通中文"],
]
# 标记为“未填写”时的完整期望：仅 NULL 被替换
NULLTEXT_EXPECTED_PLAIN = [
    ["编号", "备注"],
    ["1", NULLTEXT_PLAIN_MARKER],
    ["2", ""],
    ["3", NULLTEXT_PLAIN_MARKER],
    ["4", "普通中文"],
]


class NullTextTestCase(unittest.TestCase):
    """--null-text / export_csv 的 null_text 参数专项回归测试。

    固定样例库只有一张 records 表：四条备注依次为 SQL NULL、空字符串、
    与标记同形的普通文本与普通中文。每个用例独立准备临时目录与输出文件，
    tearDown 重新以只读连接核对源库结构与全部数据相对基线零变化；
    使用查询文件的用例另行核对文件字节未变。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "nulltext.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新只读打开源库，
        # 核对表结构及全部数据与准备完成时完全一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE records (id INTEGER PRIMARY KEY, note TEXT)"
            )
            # 备注依次为：SQL NULL、空字符串、普通文本“未填写”、普通中文
            conn.executemany(
                "INSERT INTO records (id, note) VALUES (?, ?)",
                [(1, None), (2, ""), (3, "未填写"), (4, "普通中文")],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以只读方式重新读取源库的表结构与 records 全部数据。"""
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            records = conn.execute(
                "SELECT id, note FROM records ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "records": records}

    @staticmethod
    def _read_csv(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return text, list(csv.reader(io.StringIO(text)))

    def _write_sql_file(self, content, name="查询.sql"):
        """按 UTF-8 写查询文件，返回 (路径, 写入字节) 以便用后核对。"""
        path = os.path.join(self.tmpdir, name)
        payload = content.encode("utf-8") if isinstance(content, str) else content
        with open(path, "wb") as f:
            f.write(payload)
        return path, payload

    def _run_cli(self, output, sql=None, sql_file=None,
                 null_text=NULLTEXT_PLAIN_MARKER, params=()):
        cmd = [sys.executable, REPORT_PY, "--db", self.db_path]
        if sql is not None:
            cmd += ["--sql", sql]
        if sql_file is not None:
            cmd += ["--sql-file", sql_file]
        cmd += ["--output", output, "--null-text", null_text]
        for item in params:
            cmd += ["--param", item]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

    # -- 函数入口：省略标记与显式空字符串等价 ------------------------------

    def test_default_and_explicit_empty_marker_produce_identical_bytes(self):
        out_default = os.path.join(self.tmpdir, "default.csv")
        out_empty = os.path.join(self.tmpdir, "empty_marker.csv")

        count_default = report.export_csv(self.db_path, NULLTEXT_SQL, out_default)
        count_empty = report.export_csv(
            self.db_path, NULLTEXT_SQL, out_empty, null_text=""
        )
        # 两种调用均返回全部数据行数
        self.assertEqual(count_default, 4)
        self.assertEqual(count_empty, 4)

        # 省略标记与显式传入空字符串：CSV 字节内容完全相同
        with open(out_default, "rb") as f:
            default_bytes = f.read()
        with open(out_empty, "rb") as f:
            self.assertEqual(f.read(), default_bytes)

        _, rows = self._read_csv(out_default)
        # 列名与列顺序固定；前两条备注（NULL 与空字符串）均为空字段
        self.assertEqual(rows, NULLTEXT_EXPECTED_DEFAULT)

    # -- 函数入口：自定义标记只替换 NULL -----------------------------------

    def test_marker_replaces_null_but_empty_string_stays_empty(self):
        out = os.path.join(self.tmpdir, "marker.csv")
        count = report.export_csv(
            self.db_path, NULLTEXT_SQL, out, null_text=NULLTEXT_PLAIN_MARKER
        )
        self.assertEqual(count, 4)

        _, rows = self._read_csv(out)
        # 仅第一条（NULL）被替换；第二条（空字符串）仍为空字段；
        # 第三条恰好与标记同形的普通文本原样保留，列名/行顺序/其余值不变
        self.assertEqual(rows, NULLTEXT_EXPECTED_PLAIN)

        # 物理 CSV 层面：标记按普通文本写入，不增加引号等转义前缀；
        # NULL 替换值与同形普通文本的字节形态完全一致
        with open(out, "rb") as f:
            data = f.read()
        self.assertIn(
            ("1," + NULLTEXT_PLAIN_MARKER + "\r\n").encode("utf-8"), data
        )
        self.assertIn(
            ("3," + NULLTEXT_PLAIN_MARKER + "\r\n").encode("utf-8"), data
        )
        self.assertNotIn(
            ('"' + NULLTEXT_PLAIN_MARKER + '"').encode("utf-8"), data
        )

    # -- 函数入口：含空格/逗号/引号/换行的标记经 csv.reader 完整还原 -------

    def test_complex_marker_roundtrips_through_csv_reader(self):
        out = os.path.join(self.tmpdir, "complex_marker.csv")
        count = report.export_csv(
            self.db_path, NULLTEXT_SQL, out, null_text=NULLTEXT_COMPLEX_MARKER
        )
        self.assertEqual(count, 4)

        text, rows = self._read_csv(out)
        # csv.reader 按逻辑记录解析：表头 + 四条数据；标记首尾空格、逗号、
        # 双引号与内嵌换行全部完整还原，空字符串与其他值不受影响
        self.assertEqual(
            rows,
            [
                ["编号", "备注"],
                ["1", NULLTEXT_COMPLEX_MARKER],
                ["2", ""],
                ["3", "未填写"],
                ["4", "普通中文"],
            ],
        )
        self.assertEqual(len(rows), 5)
        # 标记内嵌换行使物理行数多于逻辑记录数：不能按物理行数计算记录数
        self.assertGreater(len(text.splitlines()), len(rows))
        # 标记只作用于数据单元格，表头行保持原样
        self.assertTrue(text.startswith("编号,备注\r\n"))

    # -- 函数入口：筛选不到记录 --------------------------------------------

    def test_empty_result_header_only_returns_zero(self):
        out = os.path.join(self.tmpdir, "zero.csv")
        sql = (
            "SELECT id AS 编号, note AS 备注 FROM records "
            "WHERE id < 0 ORDER BY id"
        )
        count = report.export_csv(
            self.db_path, sql, out, null_text=NULLTEXT_PLAIN_MARKER
        )
        self.assertEqual(count, 0)
        _, rows = self._read_csv(out)
        # 仍保留表头（列名与列顺序），但没有任何数据记录
        self.assertEqual(rows, [["编号", "备注"]])

    # -- 函数入口：非字符串标记被拒绝 --------------------------------------

    def test_non_string_null_text_rejected_without_file(self):
        for bad in (None, 1, True):
            with self.subTest(null_text=bad):
                out = os.path.join(self.tmpdir, "bad_null_%r.csv" % (bad,))
                self.assertFalse(os.path.exists(out))
                with self.assertRaises(ValueError):
                    report.export_csv(
                        self.db_path, NULLTEXT_SQL, out, null_text=bad
                    )
                # 拒绝路径绝不产生目标 CSV
                self.assertFalse(os.path.exists(out))

    # -- 拒绝覆盖既有输出文件的行为保持不变 --------------------------------

    def test_existing_target_rejected_bytes_unchanged(self):
        out = os.path.join(self.tmpdir, "existing.csv")
        original_bytes = "已有内容，不得覆盖\n".encode("utf-8")
        with open(out, "wb") as f:
            f.write(original_bytes)

        with self.assertRaises(ValueError):
            report.export_csv(
                self.db_path,
                NULLTEXT_SQL,
                out,
                null_text=NULLTEXT_COMPLEX_MARKER,
            )

        with open(out, "rb") as f:
            self.assertEqual(f.read(), original_bytes)

    # -- 命令行：--sql 传入标记 --------------------------------------------

    def test_cli_sql_null_text_matches_function_result(self):
        out = os.path.join(self.tmpdir, "cli_sql_marker.csv")
        proc = self._run_cli(out, sql=NULLTEXT_SQL)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 成功信息包含实际数据行数与输出路径
        self.assertIn("已导出 4 行数据", proc.stdout)
        self.assertIn(out, proc.stdout)
        # 逻辑记录与函数调用完全一致
        _, rows = self._read_csv(out)
        self.assertEqual(rows, NULLTEXT_EXPECTED_PLAIN)

    # -- 命令行：--sql-file 传入标记 ---------------------------------------

    def test_cli_sql_file_null_text_matches_function_result(self):
        sql_path, payload = self._write_sql_file(NULLTEXT_SQL)
        out = os.path.join(self.tmpdir, "cli_file_marker.csv")
        proc = self._run_cli(out, sql_file=sql_path)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("已导出 4 行数据", proc.stdout)
        self.assertIn(out, proc.stdout)
        _, rows = self._read_csv(out)
        self.assertEqual(rows, NULLTEXT_EXPECTED_PLAIN)
        # 查询文件只被读取，字节未变
        with open(sql_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    # -- 命令行：查询文件 + 现有命名参数只选中 NULL 记录 --------------------

    def test_cli_sql_file_param_selects_null_row_with_marker(self):
        sql_path, payload = self._write_sql_file(
            NULLTEXT_WHERE_SQL + ";\n", name="参数查询.sql"
        )
        out = os.path.join(self.tmpdir, "cli_param_null.csv")
        proc = self._run_cli(out, sql_file=sql_path, params=["id=1"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("已导出 1 行数据", proc.stdout)
        self.assertIn(out, proc.stdout)
        _, rows = self._read_csv(out)
        # 恰好表头 + 一条数据，NULL 被标记替换
        self.assertEqual(rows, [["编号", "备注"], ["1", NULLTEXT_PLAIN_MARKER]])
        with open(sql_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    # -- 命令行：--null-text 缺少值 ----------------------------------------

    def test_cli_missing_null_text_value_exit_one_empty_stdout(self):
        out = os.path.join(self.tmpdir, "cli_no_marker_value.csv")
        self.assertFalse(os.path.exists(out))
        # --null-text 放在末尾使其后面无值可消费，触发缺值错误
        cmd = [
            sys.executable,
            REPORT_PY,
            "--db",
            self.db_path,
            "--sql",
            NULLTEXT_SQL,
            "--output",
            out,
            "--null-text",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

        self.assertEqual(proc.returncode, 1)
        # 拒绝原因写入标准错误，并指出缺值的选项
        self.assertIn("错误", proc.stderr)
        self.assertIn("--null-text", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
