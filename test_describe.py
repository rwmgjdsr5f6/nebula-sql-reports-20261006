#!/usr/bin/env python3
"""report 单表结构查看流程（--describe / describe_table）的可重复回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备含
"Ledger 账,单" 表（中文、空格、逗号与引号齐备）的小型样例库，
用例结束后重新只读打开源库，核对建库后的表结构与全部数据未变，
并核对 describe 调用没有新增报告或临时文件，全部临时文件随之清理。

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

# 表名同时包含中文、空格与逗号，按保存名称精确匹配
TABLE_NAME = "Ledger 账,单"
# 仅首字母大小写不同的名字，必须按不同对象拒绝
OTHER_CASE_NAME = "ledger 账,单"
VIEW_NAME = "账本视图"
MISSING_NAME = "无此表"
SEQUENCE_NAME = "sqlite_sequence"

# 建表后插入的唯一一条数据行（含中文、逗号、引号）；结构输出中不得混入。
# 数量取 7：结构 CSV 中不出现该数字串，便于核对数据未混入
DATA_REMARK = '中文首条，"引号"备注'
DATA_COUNT = 7
DATA_STATUS = "在册"

# describe CSV 解析后的逻辑记录：六字段表头 + 四条按声明顺序排列的列记录
EXPECTED_RECORDS = [
    ["cid", "name", "type", "notnull", "dflt_value", "pk"],
    ["0", "编号", "INTEGER", "0", "", "1"],
    ["1", '备"注', "TEXT", "0", "'待填'", "0"],
    ["2", "数量", "", "0", "", "0"],
    ["3", "状态", "TEXT", "1", "NULL", "0"],
]

REJECT_REASON = "未找到可查看的用户表"
OPEN_FAIL_REASON = "无法以只读方式打开数据库"
ERROR_PREFIX = "错误"


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
        # 每个用例（无论成功或失败）结束后，重新以只读方式打开源库，
        # 核对表结构、视图与全部数据与准备完成时完全一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        # describe 不创建报告或临时文件：临时目录内容与基线一致
        self.assertEqual(self._list_files(), self._files_baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # 四列依次为：自增主键、带引号列名与文本默认值、未声明类型
            # 及默认值、NOT NULL 且显式 DEFAULT NULL
            conn.execute(
                'CREATE TABLE "Ledger 账,单" ('
                '"编号" INTEGER PRIMARY KEY AUTOINCREMENT, '
                '"备""注" TEXT DEFAULT \'待填\', '
                '"数量", '
                '"状态" TEXT NOT NULL DEFAULT NULL)'
            )
            conn.execute(
                "INSERT INTO \"Ledger 账,单\" (\"备\"\"注\", \"数量\", \"状态\")"
                " VALUES (?, ?, ?)",
                (DATA_REMARK, DATA_COUNT, DATA_STATUS),
            )
            # 另建视图：PRAGMA table_info 对视图同样返回列信息，入口须排除
            conn.execute(
                'CREATE VIEW "账本视图" AS '
                'SELECT "编号", "状态" FROM "Ledger 账,单"'
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以只读方式重新读取源库的结构（含内部表登记）与全部数据。"""
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            columns = conn.execute(
                'SELECT cid, name, type, "notnull", dflt_value, pk '
                'FROM pragma_table_info(?, "main") ORDER BY cid',
                (TABLE_NAME,),
            ).fetchall()
            ledger_rows = conn.execute(
                'SELECT "编号", "备""注", "数量", "状态" '
                'FROM "Ledger 账,单" ORDER BY "编号"'
            ).fetchall()
            view_rows = conn.execute(
                'SELECT "编号", "状态" FROM "账本视图" ORDER BY "编号"'
            ).fetchall()
        finally:
            conn.close()
        return {
            "schema": schema,
            "columns": columns,
            "ledger_rows": ledger_rows,
            "view_rows": view_rows,
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

    def _run_describe(self, table_name):
        """调用 describe_table 并捕获其写入标准输出的文本。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = report.describe_table(self.db_path, table_name)
        return result, buf.getvalue()

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _assert_expected_records(self, records):
        """逐条核对表头、列顺序、cid 递增及各字段的元数据表达。"""
        self.assertEqual(records, EXPECTED_RECORDS)
        self.assertEqual(len(records), 5)
        # 四条列记录按声明顺序排列，cid 从零递增
        self.assertEqual([row[0] for row in records[1:]], ["0", "1", "2", "3"])
        # 编号 notnull 为 0、pk 为 1；其余列 pk 均为 0
        self.assertEqual((records[1][3], records[1][5]), ("0", "1"))
        self.assertEqual([row[5] for row in records[2:]], ["0", "0", "0"])
        # 只有状态的 notnull 为 1
        self.assertEqual([row[3] for row in records[1:]], ["0", "0", "0", "1"])
        # 编号没有默认值；数量的类型与默认值均为空字段
        self.assertEqual(records[1][4], "")
        self.assertEqual((records[3][2], records[3][4]), ("", ""))
        # 默认表达式保留单引号不求值；显式 DEFAULT NULL 保留文本 NULL
        self.assertEqual(records[2][4], "'待填'")
        self.assertEqual(records[4][4], "NULL")

    # -- 函数入口：正常路径 ------------------------------------------------

    def test_function_describe_returns_four_and_emits_structure_csv(self):
        result, text = self._run_describe(TABLE_NAME)

        # 返回声明列数四（不含表头）
        self.assertEqual(result, 4)
        records = self._parse_csv(text)
        self._assert_expected_records(records)
        # 含引号的列名经 CSV 解析后完整还原
        self.assertEqual(records[2][1], '备"注')
        # 结果只含列结构，不混入表中数据
        for value in (DATA_REMARK, str(DATA_COUNT), DATA_STATUS):
            self.assertNotIn(value, text)

    # -- 命令行入口：正常路径 ------------------------------------------------

    def test_cli_describe_exit_zero_csv_only(self):
        result, function_text = self._run_describe(TABLE_NAME)
        self.assertEqual(result, 4)

        # 以字节捕获后自行解码：text 模式会把标准 csv 方言的 \r\n 归一成
        # \n，无法逐字节比对；这里需要核对子进程的真实标准输出
        proc = subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path,
             "--describe", TABLE_NAME],
            capture_output=True,
        )
        stdout = proc.stdout.decode("utf-8")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 标准错误为空，标准输出仅为相同 CSV，不追加成功提示
        self.assertEqual(proc.stderr, b"")
        self.assertNotIn("已导出", stdout)
        self._assert_expected_records(self._parse_csv(stdout))
        # 命令行与函数写入标准输出的 CSV 逐字节一致
        self.assertEqual(stdout, function_text)

    # -- 函数入口：拒绝边界 --------------------------------------------------

    def test_function_rejected_names_raise_valueerror_empty_stdout(self):
        for label, name in [
            ("不存在的表名", MISSING_NAME),
            ("仅大小写不同", OTHER_CASE_NAME),
            ("视图名", VIEW_NAME),
            ("内部表 sqlite_sequence", SEQUENCE_NAME),
        ]:
            with self.subTest(case=label):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(ValueError) as ctx:
                        report.describe_table(self.db_path, name)
                # 标准错误原因随异常给出，标准输出为空
                self.assertIn(REJECT_REASON, str(ctx.exception))
                self.assertEqual(buf.getvalue(), "")

    # -- 命令行入口：拒绝边界 ------------------------------------------------

    def test_cli_rejected_names_exit_one_empty_stdout(self):
        for label, name in [
            ("不存在的表名", MISSING_NAME),
            ("仅大小写不同", OTHER_CASE_NAME),
            ("视图名", VIEW_NAME),
            ("内部表 sqlite_sequence", SEQUENCE_NAME),
        ]:
            with self.subTest(case=label):
                proc = self._run_cli("--describe", name)

                self.assertEqual(proc.returncode, 1)
                # 标准错误含错误前缀与拒绝原因，标准输出为空
                self.assertIn(ERROR_PREFIX, proc.stderr)
                self.assertIn(REJECT_REASON, proc.stderr)
                self.assertEqual(proc.stdout, "")

    # -- 命令行入口：--describe 与 --output 互斥 -----------------------------

    def test_cli_describe_with_output_rejected_no_file(self):
        out = os.path.join(self.tmpdir, "describe_out.csv")
        proc = self._run_cli("--describe", TABLE_NAME, "--output", out)

        self.assertEqual(proc.returncode, 1)
        # 按参数错误拒绝：标准错误含错误前缀，标准输出为空
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn("参数错误", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 不创建所指文件
        self.assertFalse(os.path.exists(out))


class MissingDatabaseTestCase(unittest.TestCase):
    """源库路径不存在：函数与命令行均拒绝，且绝不会创建数据库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.missing_db = os.path.join(self.tmpdir, "sample.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def _list_files(self):
        found = []
        for root, _dirs, files in os.walk(self.tmpdir):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.tmpdir))
        return sorted(found)

    def test_function_missing_db_raises_valueerror_empty_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                report.describe_table(self.missing_db, TABLE_NAME)
        self.assertIn(OPEN_FAIL_REASON, str(ctx.exception))
        self.assertEqual(buf.getvalue(), "")
        # 没有创建数据库或任何其他文件
        self.assertFalse(os.path.exists(self.missing_db))
        self.assertEqual(self._list_files(), [])

    def test_cli_missing_db_exit_one_empty_stdout_no_db_created(self):
        proc = subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.missing_db,
             "--describe", TABLE_NAME],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(proc.returncode, 1)
        # 标准错误含错误前缀与无法打开数据库的原因，标准输出为空
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertEqual(proc.stdout, "")
        # 没有创建数据库或任何其他文件
        self.assertFalse(os.path.exists(self.missing_db))
        self.assertEqual(self._list_files(), [])


if __name__ == "__main__":
    unittest.main()
