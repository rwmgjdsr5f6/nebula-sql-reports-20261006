#!/usr/bin/env python3
"""--tables / --describe 独立模式参数兼容性校验的回归测试。

只依赖 Python 标准库。本文件覆盖 parse_args 中两种只读元数据模式
共用混用判定（_standalone_mode_conflicts）的行为边界：

* 合法调用：小型固定样例库上的表名列举与列结构输出（带既有表头的
  CSV、退出码 0、标准错误为空、不追加成功提示、源库不变）；
* 空字符串值的 --null-text/--description/--title 与显式 --format csv
  同样算"已提供"，按混用拒绝；
* 多个冲突选项时错误原因中的选项名按固定声明顺序排列，与输入顺序无关；
* 错误优先级：--tables 与 --describe 同现报 --tables 混用；--describe
  空表名与冲突选项并存时先报混用，无冲突才报表名不能为空；仅含空白的
  表名不在参数层拦截，交给既有查表流程；
* 混用命令即使指向不存在的数据库、查询文件或参数文件，也在参数解析
  阶段先报参数错误：不读取这些文件、不打开源库、不创建报告，已有
  输出文件字节保持不变；
* 直接调用 parse_args 时用法错误抛 SystemExit 且 code 为 1，合法调用
  的返回属性与默认值保持不变。

每个用例在临时目录中自行准备样例库，tearDown 重新只读打开源库对照
结构与数据确认没有改写，并核对目录内没有新增文件。

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
from contextlib import redirect_stderr

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

# 小型固定样例库：两张用户表（BINARY 序 alpha < notes）与一条数据
TABLE_ALPHA = "alpha"
TABLE_NOTES = "notes"
EXPECTED_TABLES_CSV = [["name"], [TABLE_ALPHA], [TABLE_NOTES]]
EXPECTED_DESCRIBE_CSV = [
    ["cid", "name", "type", "notnull", "dflt_value", "pk"],
    ["0", "编号", "INTEGER", "0", "", "1"],
    ["1", "内容", "TEXT", "1", "NULL", "0"],
]

# 已有输出文件的固定字节：混用拒绝后必须逐字节保持
EXISTING_OUTPUT_BYTES = "已有内容，不得变化\n".encode("utf-8")

ERROR_PREFIX = "错误: "
USAGE_ERROR = "参数错误"
NOT_FOUND_REASON = "未找到可查看的用户表"


def _parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


class MetadataModeArgsTest(unittest.TestCase):
    """--tables / --describe 参数兼容性校验的合法路径与错误边界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 一个已存在的输出目标：混用命令引用它时字节必须保持不变
        self.existing_output = os.path.join(self.tmpdir, "existing.csv")
        with open(self.existing_output, "wb") as f:
            f.write(EXISTING_OUTPUT_BYTES)
        self._baseline = self._snapshot_db()
        self._files_baseline = self._list_files()

    def tearDown(self):
        # 无论用例成功或失败，源库结构与数据、目录内容都必须与基线一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        self.assertEqual(self._list_files(), self._files_baseline)
        self._tmp.cleanup()

    def _prepare_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE alpha (x TEXT)")
            conn.execute(
                'CREATE TABLE notes ('
                '"编号" INTEGER PRIMARY KEY, '
                '"内容" TEXT NOT NULL DEFAULT NULL)'
            )
            conn.execute("INSERT INTO alpha (x) VALUES ('一行数据')")
            conn.execute('INSERT INTO notes ("编号", "内容") VALUES (1, ?)',
                         ("首条记录",))
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name COLLATE BINARY"
            ).fetchall()
            data = {
                name: conn.execute('SELECT * FROM "%s"' % name).fetchall()
                for name in (TABLE_ALPHA, TABLE_NOTES)
            }
        finally:
            conn.close()
        return {"master": master, "data": data}

    def _list_files(self):
        found = []
        for root, _dirs, files in os.walk(self.tmpdir):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.tmpdir))
        return sorted(found)

    def _run_cli(self, db_path, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _assert_usage_error(self, proc, *expected_fragments):
        """用法错误的共同形态：退出 1、标准输出为空、用法与"错误: 参数错误"前缀。"""
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("usage"), proc.stderr)
        self.assertIn(ERROR_PREFIX + USAGE_ERROR, proc.stderr)
        for fragment in expected_fragments:
            self.assertIn(fragment, proc.stderr)

    # -- 合法路径：表名列举与列结构输出 --------------------------------------

    def test_cli_tables_legal_csv_only(self):
        proc = self._run_cli(self.db_path, "--tables")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        self.assertEqual(_parse_csv(proc.stdout), EXPECTED_TABLES_CSV)

    def test_cli_describe_legal_csv_only(self):
        proc = self._run_cli(self.db_path, "--describe", TABLE_NOTES)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        self.assertEqual(_parse_csv(proc.stdout), EXPECTED_DESCRIBE_CSV)

    def test_parse_args_legal_modes_keep_attributes_and_defaults(self):
        tables_args = report.parse_args(["--db", self.db_path, "--tables"])
        self.assertTrue(tables_args.tables)
        self.assertIsNone(tables_args.describe)
        # 元数据模式下 format/null_text 保持 None，不由默认值填充
        self.assertIsNone(tables_args.format)
        self.assertIsNone(tables_args.null_text)
        self.assertIsNone(tables_args.sql)
        self.assertIsNone(tables_args.output)

        describe_args = report.parse_args(
            ["--db", self.db_path, "--describe", TABLE_NOTES]
        )
        self.assertEqual(describe_args.describe, TABLE_NOTES)
        self.assertFalse(describe_args.tables)
        self.assertIsNone(describe_args.format)
        self.assertIsNone(describe_args.null_text)

        # 查询/导出模式的既有默认值填充不受影响
        query_args = report.parse_args(
            ["--db", self.db_path, "--sql", "SELECT 1", "--output", "o.csv"]
        )
        self.assertEqual(query_args.format, "csv")
        self.assertEqual(query_args.null_text, "")
        self.assertFalse(query_args.tables)
        self.assertIsNone(query_args.describe)

    # -- 空字符串值与显式 csv 同样算"已提供" ----------------------------------

    def test_empty_valued_options_count_as_conflicts(self):
        for opt in ("--null-text", "--description", "--title"):
            for mode in (["--tables"], ["--describe", TABLE_NOTES]):
                with self.subTest(mode=mode[0], option=opt):
                    proc = self._run_cli(self.db_path, *(mode + [opt, ""]))
                    # 空字符串也算已提供：按混用拒绝而非放行
                    self._assert_usage_error(proc, mode[0], opt)

    def test_explicit_format_csv_still_conflicts(self):
        for mode in (["--tables"], ["--describe", TABLE_NOTES]):
            with self.subTest(mode=mode[0]):
                proc = self._run_cli(self.db_path, *(mode + ["--format", "csv"]))
                self._assert_usage_error(proc, mode[0], "--format")

    # -- 多个冲突选项：错误原因按固定声明顺序，与输入顺序无关 -------------------

    def test_multiple_conflicts_listed_in_fixed_order(self):
        orders = [
            ["--title", "标题", "--sql", "SELECT 1", "--null-text", "M"],
            ["--null-text", "M", "--title", "标题", "--sql", "SELECT 1"],
        ]
        for extra in orders:
            with self.subTest(first=extra[0]):
                proc = self._run_cli(self.db_path, "--tables", *extra)
                # 声明顺序固定为 --sql 在 --null-text 前、--title 最后
                self._assert_usage_error(
                    proc, "--sql --null-text --title"
                )

        proc = self._run_cli(
            self.db_path,
            "--describe", TABLE_NOTES,
            "--format", "csv", "--output", self.existing_output,
        )
        # --describe 模式同样按声明顺序：--output 在 --format 前
        self._assert_usage_error(proc, "--output --format")

    # -- 错误优先级 -----------------------------------------------------------

    def test_tables_and_describe_together_reports_tables(self):
        proc = self._run_cli(self.db_path, "--tables", "--describe", TABLE_NOTES)
        self._assert_usage_error(proc, "--tables 仅与 --db 搭配", "--describe")

        # 选项顺序对调仍报 --tables 的混用错误
        proc = self._run_cli(self.db_path, "--describe", TABLE_NOTES, "--tables")
        self._assert_usage_error(proc, "--tables 仅与 --db 搭配", "--describe")

    def test_describe_empty_name_conflict_reported_before_empty_name(self):
        proc = self._run_cli(self.db_path, "--describe", "", "--sql", "SELECT 1")
        self._assert_usage_error(proc, "--describe 仅与 --db 搭配", "--sql")
        # 混用优先：不报表名不能为空
        self.assertNotIn("表名不能为空", proc.stderr)

    def test_describe_empty_name_without_conflicts_reports_empty_name(self):
        proc = self._run_cli(self.db_path, "--describe", "")
        self._assert_usage_error(proc, "--describe 表名不能为空")

    def test_describe_whitespace_only_name_goes_to_lookup(self):
        proc = self._run_cli(self.db_path, "--describe", " ")
        # 仅含空白的表名不在参数层拦截：交给既有查表流程按未找到拒绝
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn(NOT_FOUND_REASON, proc.stderr)
        self.assertNotIn(USAGE_ERROR, proc.stderr)

    # -- 混用判定先于一切文件与数据库接触 --------------------------------------

    def test_conflicts_reported_before_touching_files_or_db(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        missing_sql = os.path.join(self.tmpdir, "no_such_query.sql")
        missing_params = os.path.join(self.tmpdir, "no_such_params.json")

        proc = self._run_cli(
            missing_db,
            "--tables",
            "--sql-file", missing_sql,
            "--params-file", missing_params,
            "--output", self.existing_output,
        )
        # 参数错误优先：不打开不存在的源库、不读取查询/参数文件、不创建报告
        self._assert_usage_error(
            proc, "--tables 仅与 --db 搭配",
            "--sql-file --output --params-file",
        )
        self.assertFalse(os.path.exists(missing_db))
        self.assertFalse(os.path.exists(missing_sql))
        self.assertFalse(os.path.exists(missing_params))
        with open(self.existing_output, "rb") as f:
            self.assertEqual(f.read(), EXISTING_OUTPUT_BYTES)

    def test_describe_conflicts_reported_before_touching_files_or_db(self):
        missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        missing_params = os.path.join(self.tmpdir, "no_such_params.json")

        proc = self._run_cli(
            missing_db,
            "--describe", TABLE_NOTES,
            "--params-file", missing_params,
            "--output", self.existing_output,
        )
        self._assert_usage_error(
            proc, "--describe 仅与 --db 搭配", "--output --params-file"
        )
        self.assertFalse(os.path.exists(missing_db))
        self.assertFalse(os.path.exists(missing_params))
        with open(self.existing_output, "rb") as f:
            self.assertEqual(f.read(), EXISTING_OUTPUT_BYTES)

    # -- 直接调用 parse_args：SystemExit code 为 1 ------------------------------

    def test_parse_args_conflict_raises_systemexit_code_one(self):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                report.parse_args(
                    ["--db", self.db_path, "--tables", "--sql", "SELECT 1"]
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(ERROR_PREFIX + USAGE_ERROR, stderr_buf.getvalue())

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                report.parse_args(
                    ["--db", self.db_path, "--describe", TABLE_NOTES, "--preview", "2"]
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(ERROR_PREFIX + USAGE_ERROR, stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
