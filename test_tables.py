#!/usr/bin/env python3
"""report 用户表名列举流程（--tables / list_tables）的可重复回归测试。

只依赖 Python 标准库。每个用例在临时目录中自行准备样例库：

* 验收合成库含三张用户表 Z、a、账,单（刻意乱序创建）、一张视图、
  一个索引/触发器及 AUTOINCREMENT 自动生成的 sqlite_sequence；
* 特殊名称库另含首尾空白、双引号与换行名称，验证 CSV 往返完整保留。

用例结束后重新只读打开源库，对照建库后的结构与全部数据确认没有改写，
并核对调用没有新增报告或临时文件。

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

# 验收合成库的三张用户表（刻意按非 BINARY 顺序创建）与一个视图
TABLE_A = "Z"
TABLE_B = "a"
TABLE_C = "账,单"
VIEW_NAME = "账目视图"
ALL_TABLE_NAMES = [TABLE_A, TABLE_B, TABLE_C]
# BINARY 升序：大写 Z(0x5A) 在小写 a(0x61) 之前，中文 UTF-8 字节更大
EXPECTED_NAMES = ["Z", "a", "账,单"]

# 特殊名称：首尾空白、双引号、换行均须原样保留
WEIRD_NAMES = ['  leading', 'trailing  ', '带"双引号', '带\n换行']

ERROR_PREFIX = "错误:"
OPEN_FAIL_REASON = "无法以只读方式打开数据库"


def _parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


class _SnapshotTestCase(unittest.TestCase):
    """建库后记录结构/数据与目录基线，用例结束后逐一核对未被改写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        self._baseline = self._snapshot_db()
        self._files_baseline = self._list_files()

    def tearDown(self):
        # 无论用例成功或失败，源库结构与数据都必须与建库后一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        self.assertEqual(self._list_files(), self._files_baseline)
        self._tmp.cleanup()

    def _prepare_db(self):  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    def _snapshot_db(self):
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name COLLATE BINARY"
            ).fetchall()
            data = {}
            for name in EXPECTED_NAMES:
                try:
                    data[name] = conn.execute(
                        'SELECT * FROM "%s"' % name.replace('"', '""')
                    ).fetchall()
                except sqlite3.Error:
                    data[name] = None
            try:
                sequence = conn.execute(
                    "SELECT name, seq FROM sqlite_sequence ORDER BY name"
                ).fetchall()
            except sqlite3.Error:
                # 没有 AUTOINCREMENT 的库不存在 sqlite_sequence
                sequence = None
        finally:
            conn.close()
        return {"master": master, "data": data, "sequence": sequence}

    def _list_files(self):
        found = []
        for root, _dirs, files in os.walk(self.tmpdir):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.tmpdir))
        return sorted(found)

    def _run_list(self, db_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = report.list_tables(db_path)
        return result, buf.getvalue()

    def _run_cli(self, db_path, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


class TablesAcceptanceTest(_SnapshotTestCase):
    """验收合成库：Z、a、账,单 三张表 + 视图 + sqlite_sequence 的列举行为。"""

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # 刻意按乱序创建，证明结果不按创建顺序排列。
            # 账,单 使用 AUTOINCREMENT，插入一行后自动生成 sqlite_sequence
            conn.execute('CREATE TABLE "账,单" (id INTEGER PRIMARY KEY AUTOINCREMENT)')
            conn.execute("CREATE TABLE a (note TEXT)")
            conn.execute('CREATE TABLE Z (id INTEGER, "奇 怪,列" TEXT)')
            conn.execute('CREATE VIEW "账目视图" AS SELECT id FROM "账,单"')
            conn.execute('CREATE INDEX "z_idx" ON Z (id)')
            conn.execute(
                'CREATE TRIGGER "z_trg" AFTER INSERT ON a '
                "BEGIN UPDATE a SET note = note WHERE 0; END"
            )
            conn.execute('INSERT INTO "账,单" DEFAULT VALUES')
            conn.execute("INSERT INTO a (note) VALUES ('一行数据')")
            conn.commit()
        finally:
            conn.close()

    # -- 函数入口：正常路径 ------------------------------------------------

    def test_function_returns_three_and_lists_binary_order(self):
        result, text = self._run_list(self.db_path)

        # 返回表数量（不含表头）
        self.assertEqual(result, 3)
        records = _parse_csv(text)
        self.assertEqual(records, [["name"]] + [[n] for n in EXPECTED_NAMES])
        # 视图、索引、触发器与 sqlite_sequence 均不出现
        flat = [field for row in records for field in row]
        self.assertNotIn(VIEW_NAME, flat)
        self.assertNotIn("sqlite_sequence", flat)
        self.assertNotIn("z_idx", flat)
        self.assertNotIn("z_trg", flat)

    def test_function_header_always_single_name_column(self):
        _result, text = self._run_list(self.db_path)
        lines = text.splitlines()

        # 固定单列表头 name；每条表名一个逻辑记录
        self.assertEqual(_parse_csv(lines[0]), [["name"]])
        self.assertEqual(len(_parse_csv(text)), 4)

    def test_function_does_not_read_table_data(self):
        _result, text = self._run_list(self.db_path)
        # 只输出表名，不泄露任何行数据或列信息
        self.assertNotIn("一行数据", text)
        self.assertNotIn("奇 怪,列", text)

    # -- 命令行入口：正常路径 ----------------------------------------------

    def test_cli_exit_zero_csv_only(self):
        proc = self._run_cli(self.db_path, "--tables")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        records = _parse_csv(proc.stdout)
        self.assertEqual(records, [["name"]] + [[n] for n in EXPECTED_NAMES])

        _result, func_text = self._run_list(self.db_path)
        self.assertEqual(records, _parse_csv(func_text))

    # -- 与 --describe 互操作：列出的名称可直接按精确匹配查看结构 -----------

    def test_listed_name_works_with_describe(self):
        result, text = self._run_list(self.db_path)
        names = [row[0] for row in _parse_csv(text)[1:]]
        self.assertEqual(result, 3)

        # 取含逗号的中文名交给 describe_table，精确匹配语义保持可用
        target = names[2]
        self.assertEqual(target, TABLE_C)
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_count = report.describe_table(self.db_path, target)
        describe_records = _parse_csv(buf.getvalue())
        self.assertEqual(describe_count, 1)
        self.assertEqual(describe_records[0],
                         ["cid", "name", "type", "notnull", "dflt_value", "pk"])
        self.assertEqual(describe_records[1][1], "id")

    def test_cli_listed_name_works_with_describe_cli(self):
        listed = self._run_cli(self.db_path, "--tables")
        names = [row[0] for row in _parse_csv(listed.stdout)[1:]]

        proc = self._run_cli(self.db_path, "--describe", names[2])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = _parse_csv(proc.stdout)
        self.assertEqual(records[1][1], "id")

    # -- 选项混用：参数错误，退出 1，标准输出为空，不开库/不读查询文件 ----

    def test_cli_tables_rejects_other_options(self):
        out_path = os.path.join(self.tmpdir, "must_not_exist.csv")
        missing_sql = os.path.join(self.tmpdir, "no_such_query.sql")
        cases = [
            ("--describe", TABLE_A),
            ("--output", out_path),
            ("--format", "csv"),
            ("--preview", "2"),
            ("--sql", "SELECT 1"),
            ("--sql-file", missing_sql),
            ("--param", "x=1"),
            ("--null-text", "NULL"),
        ]
        for case in cases:
            with self.subTest(case=case[0]):
                proc = self._run_cli(self.db_path, "--tables", *case)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(ERROR_PREFIX, proc.stderr)
                self.assertIn("参数错误", proc.stderr)
                self.assertIn("--tables", proc.stderr)
                self.assertEqual(proc.stdout, "")
        # 参数解析阶段即拒绝，输出目标始终未被创建
        self.assertFalse(os.path.exists(out_path))

    def test_cli_describe_rejects_tables(self):
        proc = self._run_cli(self.db_path, "--describe", TABLE_A, "--tables")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn("参数错误", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_cli_tables_without_db_exits_one_empty_stdout(self):
        proc = subprocess.run(
            [sys.executable, REPORT_PY, "--tables"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn("参数错误", proc.stderr)
        self.assertEqual(proc.stdout, "")


class TablesEmptyDbTest(_SnapshotTestCase):
    """有效但没有任何用户表的库：仍输出 name 表头，返回 0，命令成功。"""

    def _prepare_db(self):
        # 建立一个只有文件头的空库；不创建任何对象
        conn = sqlite3.connect(self.db_path)
        conn.close()

    def test_function_empty_db_header_only_returns_zero(self):
        result, text = self._run_list(self.db_path)
        self.assertEqual(result, 0)
        self.assertEqual(_parse_csv(text), [["name"]])

    def test_cli_empty_db_exit_zero_header_only(self):
        proc = self._run_cli(self.db_path, "--tables")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(_parse_csv(proc.stdout), [["name"]])


class TablesWeirdNamesTest(_SnapshotTestCase):
    """含首尾空白、双引号与换行名称的库：CSV 往返后名称完整保留。"""

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            for name in WEIRD_NAMES:
                conn.execute('CREATE TABLE "%s" (x INTEGER)' % name.replace('"', '""'))
            # 视图与内部表仍须排除
            conn.execute('CREATE VIEW "v 视图" AS SELECT 1 AS x')
            conn.commit()
        finally:
            conn.close()

    def test_special_names_round_trip_through_csv(self):
        result, text = self._run_list(self.db_path)
        records = _parse_csv(text)
        names = [row[0] for row in records[1:]]

        self.assertEqual(result, len(WEIRD_NAMES))
        # 每个特殊名称经 CSV 解析后完整还原（首尾空白不剥离）
        for name in WEIRD_NAMES:
            self.assertIn(name, names)
        self.assertNotIn("v 视图", names)
        # 换行名称在原始 CSV 文本中占多个物理行，但只是一条逻辑记录
        self.assertEqual(len(records), 1 + len(WEIRD_NAMES))

    def test_order_is_binary_not_creation_order(self):
        _result, text = self._run_list(self.db_path)
        names = [row[0] for row in _parse_csv(text)[1:]]
        # 与 SQLite 直接给出的 BINARY 排序完全一致
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            expected = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "ORDER BY name COLLATE BINARY"
            ).fetchall()]
        finally:
            conn.close()
        self.assertEqual(names, expected)
        # 首字符为空格(0x20)的名称排在最前
        self.assertEqual(names[0], "  leading")
        # 去空白不会得到同一组名字（首尾空白被保留的直接证据）
        self.assertNotIn("leading", names)


class TablesFailureTest(unittest.TestCase):
    """源库缺失或损坏：ValueError / 退出 1，不创建任何文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_function_missing_db_raises_and_creates_nothing(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.assertFalse(os.path.exists(missing_db))

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                report.list_tables(missing_db)
        self.assertIn(OPEN_FAIL_REASON, str(ctx.exception))
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse(os.path.exists(missing_db))

    def test_cli_missing_db_exit_one_and_creates_nothing(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.assertFalse(os.path.exists(missing_db))

        proc = subprocess.run(
            [sys.executable, REPORT_PY, "--db", missing_db, "--tables"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(missing_db))

    def test_function_corrupt_db_raises_valueerror_empty_stdout(self):
        corrupt_db = os.path.join(self.tmpdir, "corrupt.sqlite")
        with open(corrupt_db, "wb") as f:
            f.write(b"this is not a sqlite database file\n" * 4)

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError) as ctx:
                report.list_tables(corrupt_db)
        self.assertIn(OPEN_FAIL_REASON, str(ctx.exception))
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
