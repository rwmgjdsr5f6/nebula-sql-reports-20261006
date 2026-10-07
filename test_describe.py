#!/usr/bin/env python3
"""report 单表结构查看流程（--describe / describe_table）的可重复回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备含表
"Ledger 账,单"、视图与 sqlite_sequence 的小型样例库，用例结束后
重新只读打开源库，对照建库后的表结构与全部数据确认没有改写，并核对
调用没有新增报告或临时文件，全部临时文件随之清理。

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

# 表名同时包含中文、空格与逗号；列名 "备\"注" 含双引号
TABLE_NAME = "Ledger 账,单"
VIEW_NAME = "账目视图"
CASE_VARIANT_NAME = "ledger 账,单"
MISSING_NAME = "不存在的表 名称"
INTERNAL_NAME = "sqlite_sequence"

# 一条含中文、逗号与引号的合法记录
DATA_NOTE = '首条记录，含中文与"引号"'
DATA_QTY = 7
DATA_STATUS = "在册"

# describe_table 输出的六字段固定表头及四条列记录（CSV 解析后的逻辑值）
EXPECTED_RECORDS = [
    ["cid", "name", "type", "notnull", "dflt_value", "pk"],
    ["0", "编号", "INTEGER", "0", "", "1"],
    ["1", '备"注', "TEXT", "0", "'待填'", "0"],
    ["2", "数量", "", "0", "", "0"],
    ["3", "状态", "TEXT", "1", "NULL", "0"],
]
REJECT_REASON = "未找到可查看的用户表"
OPEN_FAIL_REASON = "无法以只读方式打开数据库"
ERROR_PREFIX = "错误:"

# 函数入口与命令行共同覆盖的四类拒绝名字：不存在、仅大小写不同、
# 视图、sqlite_ 开头的内部表（AUTOINCREMENT 会自动生成 sqlite_sequence）
REJECTED_NAMES = [
    ("不存在的表名", MISSING_NAME),
    ("仅大小写不同", CASE_VARIANT_NAME),
    ("视图名", VIEW_NAME),
    ("内部表", INTERNAL_NAME),
]


class DescribeTestCase(unittest.TestCase):
    """describe_table 函数与 --describe 命令行入口的公开行为与拒绝边界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构、数据与目录内容基线，tearDown 中逐一核对
        self._baseline = self._snapshot_db()
        self._files_baseline = self._list_files()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新以只读方式读取已有源库，
        # 对照建库后的表结构与全部数据确认没有改写
        self.assertEqual(self._snapshot_db(), self._baseline)
        # 不创建报告或临时文件：临时目录内容与基线一致
        self.assertEqual(self._list_files(), self._files_baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # 四列依次：INTEGER PRIMARY KEY AUTOINCREMENT、TEXT DEFAULT '待填'、
            # 未声明类型及默认值、TEXT NOT NULL DEFAULT NULL
            conn.execute(
                'CREATE TABLE "Ledger 账,单" ('
                '"编号" INTEGER PRIMARY KEY AUTOINCREMENT, '
                '"备""注" TEXT DEFAULT \'待填\', '
                '"数量", '
                '"状态" TEXT NOT NULL DEFAULT NULL)'
            )
            # 另建一个视图，用于验证视图名被拒绝；AUTOINCREMENT 还会
            # 自动生成登记为 table 的 sqlite_sequence
            conn.execute(
                'CREATE VIEW "账目视图" AS '
                'SELECT "编号", "状态" FROM "Ledger 账,单"'
            )
            # 状态列 NOT NULL，必须显式给值；其余列显式提供一条含中文的记录
            conn.execute(
                'INSERT INTO "Ledger 账,单" ("备""注", "数量", "状态") '
                "VALUES (?, ?, ?)",
                (DATA_NOTE, DATA_QTY, DATA_STATUS),
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以只读方式重新读取源库的结构、元数据与全部数据。"""
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            columns = conn.execute(
                'SELECT cid, name, type, "notnull", dflt_value, pk '
                'FROM pragma_table_info(?, "main") ORDER BY cid',
                (TABLE_NAME,),
            ).fetchall()
            ledger = conn.execute(
                'SELECT "编号", "备""注", "数量", "状态" '
                'FROM "Ledger 账,单" ORDER BY "编号"'
            ).fetchall()
            view_rows = conn.execute(
                'SELECT "编号", "状态" FROM "账目视图" ORDER BY "编号"'
            ).fetchall()
            sequence = conn.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return {
            "master": master,
            "columns": columns,
            "ledger": ledger,
            "view_rows": view_rows,
            "sequence": sequence,
        }

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

    def _run_describe(self, db_path, name):
        """调用 describe_table 并捕获其写入标准输出的文本。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = report.describe_table(db_path, name)
        return result, buf.getvalue()

    def _run_cli(self, db_path, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # -- 函数入口：正常路径 ------------------------------------------------

    def test_function_describe_returns_four_with_full_metadata(self):
        result, text = self._run_describe(self.db_path, TABLE_NAME)

        # 返回声明列数四（不含表头）
        self.assertEqual(result, 4)
        records = self._parse_csv(text)
        # 六字段表头 + 四条列记录，按声明顺序排列
        self.assertEqual(records, EXPECTED_RECORDS)
        self.assertEqual([row[0] for row in records[1:]], ["0", "1", "2", "3"])
        # 带引号的列名经 CSV 解析后完整还原
        self.assertEqual(records[2][1], '备"注')

    def test_function_describe_field_semantics(self):
        _result, text = self._run_describe(self.db_path, TABLE_NAME)
        rows = {row[1]: row for row in self._parse_csv(text)[1:]}

        # 编号：notnull 为 0、pk 为 1，且没有默认值
        self.assertEqual(rows["编号"][3:6], ["0", "", "1"])
        # 其余列 pk 均为 0
        self.assertEqual([rows[name][5] for name in ('备"注', "数量", "状态")],
                         ["0", "0", "0"])
        # 只有状态的 notnull 为 1
        self.assertEqual(rows['备"注'][3], "0")
        self.assertEqual(rows["数量"][3], "0")
        self.assertEqual(rows["状态"][3], "1")
        # 数量的类型与默认值均为空字段
        self.assertEqual(rows["数量"][2], "")
        self.assertEqual(rows["数量"][4], "")
        # 备"注列的默认表达式保留单引号、不求值
        self.assertEqual(rows['备"注'][4], "'待填'")
        # 状态的默认值是文本 NULL
        self.assertEqual(rows["状态"][4], "NULL")

    def test_function_describe_output_has_structure_only(self):
        _result, text = self._run_describe(self.db_path, TABLE_NAME)
        records = self._parse_csv(text)

        # 只有表头与四条列记录，不混入表中数据
        self.assertEqual(len(records), 5)
        flat = [field for row in records for field in row]
        self.assertNotIn(DATA_NOTE, flat)
        self.assertNotIn(DATA_STATUS, flat)
        self.assertNotIn(str(DATA_QTY), flat)

    # -- 命令行入口：正常路径 ------------------------------------------------

    def test_cli_describe_exit_zero_csv_only(self):
        proc = self._run_cli(self.db_path, "--describe", TABLE_NAME)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 退出零、标准错误为空，不追加成功提示
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        # 标准输出只有与函数入口相同的 CSV 逻辑记录
        records = self._parse_csv(proc.stdout)
        self.assertEqual(records, EXPECTED_RECORDS)
        self.assertEqual(len(records), 5)

        _result, func_text = self._run_describe(self.db_path, TABLE_NAME)
        self.assertEqual(records, self._parse_csv(func_text))

    # -- 函数入口：拒绝边界，ValueError 且标准输出为空 ------------------------

    def test_function_rejected_names_raise_valueerror_empty_stdout(self):
        for label, name in REJECTED_NAMES:
            with self.subTest(case=label):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(ValueError) as ctx:
                        report.describe_table(self.db_path, name)
                # 拒绝原因与被拒绝的名字都在异常消息中
                self.assertIn(REJECT_REASON, str(ctx.exception))
                self.assertIn(name, str(ctx.exception))
                # 任何失败都在写入标准输出之前抛出
                self.assertEqual(buf.getvalue(), "")

    # -- 命令行入口：拒绝边界，退出 1 且标准输出为空 --------------------------

    def test_cli_rejected_names_exit_one_stderr_reason_empty_stdout(self):
        for label, name in REJECTED_NAMES:
            with self.subTest(case=label):
                proc = self._run_cli(self.db_path, "--describe", name)

                self.assertEqual(proc.returncode, 1)
                # 标准错误含错误前缀与拒绝原因（含被拒绝的名字）
                self.assertIn(ERROR_PREFIX, proc.stderr)
                self.assertIn(REJECT_REASON, proc.stderr)
                self.assertIn(name, proc.stderr)
                # 标准输出为空
                self.assertEqual(proc.stdout, "")

    # -- 源库路径不存在：不创建数据库 ----------------------------------------

    def test_function_missing_db_raises_and_creates_nothing(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.assertFalse(os.path.exists(missing_db))

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                report.describe_table(missing_db, TABLE_NAME)
        self.assertIn(OPEN_FAIL_REASON, str(ctx.exception))
        self.assertEqual(buf.getvalue(), "")
        # 绝不会创建数据库文件
        self.assertFalse(os.path.exists(missing_db))

    def test_cli_missing_db_exit_one_and_creates_nothing(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.assertFalse(os.path.exists(missing_db))

        proc = self._run_cli(missing_db, "--describe", TABLE_NAME)

        self.assertEqual(proc.returncode, 1)
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 绝不会创建数据库文件
        self.assertFalse(os.path.exists(missing_db))

    # -- --describe 与 --output 同用按参数错误拒绝，不创建所指文件 -------------

    def test_cli_describe_with_output_rejected_no_file(self):
        out_path = os.path.join(self.tmpdir, "must_not_exist.csv")
        self.assertFalse(os.path.exists(out_path))

        proc = self._run_cli(
            self.db_path, "--describe", TABLE_NAME, "--output", out_path
        )

        self.assertEqual(proc.returncode, 1)
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn("参数错误", proc.stderr)
        self.assertIn("--describe", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 参数解析阶段即拒绝，不创建所指文件
        self.assertFalse(os.path.exists(out_path))


if __name__ == "__main__":
    unittest.main()
