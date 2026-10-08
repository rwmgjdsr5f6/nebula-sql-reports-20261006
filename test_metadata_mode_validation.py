#!/usr/bin/env python3
"""--tables/--describe 参数兼容性校验重构的边界回归测试。

只覆盖本次重构的校验边界，不重复既有 test_tables.py / test_describe.py
已覆盖的元数据读取语义：

* 小型固定样例上的合法表名列举与列结构输出（含仅含空格表名不被剥离）；
* 两个模式对全部冲突选项的拒绝（含显式 --format csv 与空字符串值选项）；
* 多项冲突时错误原因中的选项名按固定次序排列，与命令行输入顺序无关；
* 错误优先级：--describe 空表名与冲突并存时先报混用，--tables 与
  --describe 并存时报 --tables 的混用错误；
* 混用命令在参数解析阶段即失败：不读取查询/参数文件、不打开源库、
  不创建报告，已有输出文件字节保持不变；
* 直接调用 parse_args 时抛 SystemExit(code=1)，命令行入口退出码 1、
  标准输出为空、标准错误保留用法与“错误: 参数错误”前缀；
* parse_args 在两个元数据模式下的返回属性与默认值保持不变
  （--format/--null-text 仍为 None，不被补成查询模式默认值）。

只依赖 Python 标准库。在项目根目录执行：
    python -m unittest test_metadata_mode_validation
"""

import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

ERROR_PREFIX = "错误: 参数错误"
USAGE_PREFIX = "usage:"
EMPTY_NAME_REASON = "--describe 表名不能为空"
OPEN_FAIL_REASON = "无法以只读方式打开数据库"
NOT_FOUND_REASON = "未找到可查看的用户表"
SQL_FILE_REASON = "查询文件"
PARAMS_FILE_REASON = "参数文件"

# 固定样例库中的对象
MAIN_TABLE = "t_main"
SPACE_TABLE = " "
VIEW_NAME = "v_view"

# --tables 在固定样例库上的完整输出（BINARY 升序：空格 0x20 先于字母；
# 视图 v_view 与 AUTOINCREMENT 生成的 sqlite_sequence 均不出现）
EXPECTED_TABLES_CSV = "name\n \nt_main\n"

# --describe t_main 的完整原始 CSV：固定表头 + 四列。
# note 的默认表达式 'n/a' 原文保留；qty 无类型无默认值 -> 两个空字段；
# flag 的显式 DEFAULT NULL 在元数据中是文本 NULL
EXPECTED_DESCRIBE_CSV = (
    "cid,name,type,notnull,dflt_value,pk\n"
    "0,id,INTEGER,0,,1\n"
    "1,note,TEXT,0,'n/a',0\n"
    "2,qty,,0,,0\n"
    "3,flag,TEXT,1,NULL,0\n"
)
EXPECTED_SPACE_DESCRIBE_CSV = (
    "cid,name,type,notnull,dflt_value,pk\n"
    "0,x,TEXT,0,,0\n"
)

# 全部冲突选项在错误原因中的固定次序（--describe 仅在 --tables 模式下
# 额外计入并排在最前）
FIXED_EXTRA_ORDER = [
    "--sql",
    "--sql-file",
    "--output",
    "--preview",
    "--format",
    "--param",
    "--params-file",
    "--null-text",
    "--description",
    "--title",
]


class _SampleDbTestCase(unittest.TestCase):
    """准备固定样例库并记录基线，用例结束后核对结构、数据与文件未变。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        conn = sqlite3.connect(self.db_path)
        try:
            # 四列覆盖：INTEGER 主键自增、文本默认表达式、无类型无默认、
            # NOT NULL 且显式 DEFAULT NULL；自增还会生成 sqlite_sequence
            conn.execute(
                "CREATE TABLE t_main ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "note TEXT DEFAULT 'n/a', "
                "qty, "
                "flag TEXT NOT NULL DEFAULT NULL)"
            )
            # 仅含空格的表名：合法调用时必须原样进入查表流程
            conn.execute('CREATE TABLE " " (x TEXT)')
            conn.execute("CREATE VIEW v_view AS SELECT 1 AS one")
            conn.execute("INSERT INTO t_main (flag) VALUES ('on')")
            conn.commit()
        finally:
            conn.close()
        self._db_baseline = self._snapshot_db()
        self._files_baseline = self._list_files()

    def tearDown(self):
        self.assertEqual(self._snapshot_db(), self._db_baseline)
        self.assertEqual(self._list_files(), self._files_baseline)
        self._tmp.cleanup()

    def _snapshot_db(self):
        uri = "file:%s?mode=ro" % os.path.abspath(self.db_path)
        conn = sqlite3.connect(uri, uri=True)
        try:
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name COLLATE BINARY"
            ).fetchall()
            data = conn.execute(
                "SELECT id, note, qty, flag FROM t_main ORDER BY id"
            ).fetchall()
            try:
                sequence = conn.execute(
                    "SELECT name, seq FROM sqlite_sequence"
                ).fetchall()
            except sqlite3.Error:
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

    # -- 调用助手 -----------------------------------------------------------

    def _run_cli(self, *argv, db_path=None):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", db_path or self.db_path] + list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _parse_args_expecting_exit(self, argv):
        """直接调用 parse_args，断言抛 SystemExit(code=1)，返回捕获的 stderr。"""
        buf = io.StringIO()
        with redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                report.parse_args(argv)
        self.assertEqual(ctx.exception.code, 1)
        return buf.getvalue()

    def _assert_usage_error(self, text, mode_option):
        """命令行/parse_args 共用的用法错误外观断言。"""
        self.assertTrue(text.startswith(USAGE_PREFIX), text)
        self.assertIn(ERROR_PREFIX, text)
        self.assertIn(mode_option + " 仅与 --db 搭配", text)

    @staticmethod
    def _error_line(text):
        """取标准错误中“错误: …”所在的最后一行（冲突原因完整在此行）。"""
        return [line for line in text.splitlines() if line.startswith("错误:")][-1]


class LegalMetadataOutputTest(_SampleDbTestCase):
    """固定样例上的合法输出：带既有表头的 CSV，成功零提示，属性默认值不变。"""

    def test_tables_lists_fixed_sample_binary_order(self):
        proc = self._run_cli("--tables")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 不追加导出成功提示
        self.assertNotIn("已导出", proc.stdout)
        # 原始字节即固定预期（空格名不加引号、视图与内部表不出现）
        self.assertEqual(proc.stdout, EXPECTED_TABLES_CSV)
        self.assertNotIn(VIEW_NAME, proc.stdout)
        self.assertNotIn("sqlite_sequence", proc.stdout)
        # 不读取表内数据
        self.assertNotIn("on", proc.stdout)

    def test_describe_fixed_sample_full_column_structure(self):
        proc = self._run_cli("--describe", MAIN_TABLE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("已导出", proc.stdout)
        self.assertEqual(proc.stdout, EXPECTED_DESCRIBE_CSV)

    def test_describe_whitespace_only_name_goes_to_lookup_untrimmed(self):
        # 仅含空格的表名不被剥离、不被判空：样例库中确有该表，正常输出
        proc = self._run_cli("--describe", SPACE_TABLE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, EXPECTED_SPACE_DESCRIBE_CSV)

        # 纯空格但不存在的名字交给既有查表流程（报“未找到”而非参数错误，
        # 名字中的空格原样出现在原因中），证明没有自行 strip
        proc_missing = self._run_cli("--describe", "   ")
        self.assertEqual(proc_missing.returncode, 1)
        self.assertEqual(proc_missing.stdout, "")
        self.assertIn(NOT_FOUND_REASON, proc_missing.stderr)
        self.assertIn("：   ", proc_missing.stderr)
        self.assertNotIn(EMPTY_NAME_REASON, proc_missing.stderr)

    def test_parse_args_keeps_attributes_and_defaults(self):
        tables_ns = report.parse_args(
            ["--db", self.db_path, "--tables"]
        )
        self.assertTrue(tables_ns.tables)
        self.assertIsNone(tables_ns.describe)
        # 元数据模式不套用查询模式默认值：两个属性保持解析默认 None
        self.assertIsNone(tables_ns.format)
        self.assertIsNone(tables_ns.null_text)

        describe_ns = report.parse_args(
            ["--db", self.db_path, "--describe", MAIN_TABLE]
        )
        self.assertFalse(describe_ns.tables)
        self.assertEqual(describe_ns.describe, MAIN_TABLE)
        self.assertIsNone(describe_ns.format)
        self.assertIsNone(describe_ns.null_text)

        # 仅含空格的表名同样通过参数校验、原样保留
        spaced_ns = report.parse_args(
            ["--db", self.db_path, "--describe", SPACE_TABLE]
        )
        self.assertEqual(spaced_ns.describe, SPACE_TABLE)


# 单个冲突选项用例：(命令行片段, 错误原因中应出现的选项名)。
# 查询文件与参数文件刻意指向不存在的路径，以证明解析阶段失败时不读文件。
def _single_conflict_cases(tmpdir, include_describe):
    missing_sql = os.path.join(tmpdir, "no_such_query.sql")
    missing_params = os.path.join(tmpdir, "no_such_params.json")
    out_path = os.path.join(tmpdir, "must_not_exist.csv")
    cases = [
        (["--sql", "SELECT 1"], "--sql"),
        (["--sql-file", missing_sql], "--sql-file"),
        (["--output", out_path], "--output"),
        (["--preview", "2"], "--preview"),
        # 显式 --format csv 仍算冲突
        (["--format", "csv"], "--format"),
        (["--param", "x=1"], "--param"),
        (["--params-file", missing_params], "--params-file"),
        # 空字符串也是“已提供”，不得放行
        (["--null-text", ""], "--null-text"),
        (["--description", ""], "--description"),
        (["--title", ""], "--title"),
    ]
    if include_describe:
        cases.append((["--describe", MAIN_TABLE], "--describe"))
    return cases


class ConflictRejectionTest(_SampleDbTestCase):
    """两个模式对每个冲突选项的拒绝：CLI 退出 1 与 parse_args 抛 SystemExit。"""

    def _assert_single_conflict(self, mode_argv, mode_option, label):
        # 命令行入口：退出码 1、标准输出为空、标准错误保留用法与前缀
        proc = self._run_cli(*mode_argv)
        self.assertEqual(proc.returncode, 1, mode_argv)
        self.assertEqual(proc.stdout, "")
        self._assert_usage_error(proc.stderr, mode_option)
        self.assertIn(label, self._error_line(proc.stderr))

        # 直接调用 parse_args：SystemExit code 1，错误外观一致
        text = self._parse_args_expecting_exit(
            ["--db", self.db_path] + mode_argv
        )
        self._assert_usage_error(text, mode_option)
        self.assertIn(label, self._error_line(text))

    def test_tables_rejects_each_conflicting_option(self):
        for fragment, label in _single_conflict_cases(self.tmpdir, True):
            with self.subTest(option=label):
                self._assert_single_conflict(
                    ["--tables"] + fragment, "--tables", label
                )

    def test_describe_rejects_each_conflicting_option(self):
        for fragment, label in _single_conflict_cases(self.tmpdir, False):
            with self.subTest(option=label):
                self._assert_single_conflict(
                    ["--describe", MAIN_TABLE] + fragment, "--describe", label
                )

    def test_multiple_conflicts_keep_fixed_order_regardless_of_input_order(self):
        # 刻意打乱输入次序（再按选项对整体逆序），原因行必须同一次序
        pairs = [
            ("--title", "标题"),
            ("--params-file", os.path.join(self.tmpdir, "p.json")),
            ("--param", "a=1"),
            ("--format", "csv"),
            ("--preview", "2"),
            ("--output", os.path.join(self.tmpdir, "out.csv")),
            ("--sql-file", os.path.join(self.tmpdir, "q.sql")),
            ("--null-text", ""),
            ("--description", ""),
            ("--sql", "SELECT 1"),
        ]

        def flatten(seq):
            out = []
            for flag, value in seq:
                out.extend([flag, value])
            return out

        scrambled = flatten(pairs)
        reversed_pairs = flatten(list(reversed(pairs)))
        expected_tables = " ".join(["--describe"] + FIXED_EXTRA_ORDER)
        expected_describe = " ".join(FIXED_EXTRA_ORDER)

        for order_name, extra in (
            ("乱序", scrambled + ["--describe", MAIN_TABLE]),
            ("逆序", reversed_pairs + ["--describe", MAIN_TABLE]),
        ):
            with self.subTest(order=order_name):
                proc = self._run_cli("--tables", *extra)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(
                    self._error_line(proc.stderr).endswith(expected_tables),
                    proc.stderr,
                )

        # --describe 模式的固定次序不含 --describe 自身
        for order_name, body in (
            ("乱序", scrambled),
            ("逆序", reversed_pairs),
        ):
            with self.subTest(mode="describe", order=order_name):
                proc = self._run_cli("--describe", MAIN_TABLE, *body)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(
                    self._error_line(proc.stderr).endswith(expected_describe),
                    proc.stderr,
                )

    def test_tables_and_describe_together_reports_tables_error(self):
        proc = self._run_cli("--tables", "--describe", MAIN_TABLE)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self._assert_usage_error(proc.stderr, "--tables")
        # --describe 出现在原因列表首位
        self.assertTrue(
            self._error_line(proc.stderr).endswith("--describe"),
            proc.stderr,
        )

        text = self._parse_args_expecting_exit(
            ["--db", self.db_path, "--tables", "--describe", MAIN_TABLE]
        )
        self.assertIn("--tables 仅与 --db 搭配", text)
        self.assertNotIn(EMPTY_NAME_REASON, text)


class ErrorPriorityAndUntouchedFilesTest(unittest.TestCase):
    """错误优先级与“混用先失败、不接触任何文件”的保证（多用不存在的路径）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.missing_db = os.path.join(self.tmpdir, "missing.sqlite")
        self.missing_sql = os.path.join(self.tmpdir, "missing_query.sql")
        self.missing_params = os.path.join(self.tmpdir, "missing_params.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_cli_paths(self, *argv, db_path):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", db_path] + list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_describe_empty_name_with_conflicts_reports_mix_first(self):
        # 空表名 + 多项冲突：先报混用；即使库/查询文件/参数文件都不存在，
        # 也不读取它们、不打开源库、不补报表名错误
        for argv in (
            ["--describe", "", "--sql-file", self.missing_sql,
             "--params-file", self.missing_params, "--null-text", ""],
            # 调换输入次序，原因行次序不变
            ["--null-text", "", "--params-file", self.missing_params,
             "--sql-file", self.missing_sql, "--describe", ""],
        ):
            with self.subTest(argv=argv):
                proc = self._run_cli_paths(*argv, db_path=self.missing_db)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertIn("--describe 仅与 --db 搭配", proc.stderr)
                error_line = [
                    line for line in proc.stderr.splitlines()
                    if line.startswith("错误:")
                ][-1]
                self.assertTrue(
                    error_line.endswith(
                        "--sql-file --params-file --null-text"
                    ),
                    proc.stderr,
                )
                self.assertNotIn(EMPTY_NAME_REASON, proc.stderr)
                self.assertNotIn(SQL_FILE_REASON, proc.stderr)
                self.assertNotIn(PARAMS_FILE_REASON, proc.stderr)
                self.assertNotIn(OPEN_FAIL_REASON, proc.stderr)
                # 源库未被创建，不存在的文件路径依旧不存在
                self.assertFalse(os.path.exists(self.missing_db))
                self.assertFalse(os.path.exists(self.missing_sql))
                self.assertFalse(os.path.exists(self.missing_params))

        # parse_args 直接调用同样抛 SystemExit(1) 且报混用
        buf = io.StringIO()
        with redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                report.parse_args(
                    ["--db", self.missing_db, "--describe", "",
                     "--sql-file", self.missing_sql]
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--describe 仅与 --db 搭配", buf.getvalue())
        self.assertNotIn(EMPTY_NAME_REASON, buf.getvalue())

    def test_describe_empty_name_without_conflict_reports_empty_name(self):
        # 没有冲突选项时才报表名不能为空（即使库不存在也不切到开库错误）
        proc = self._run_cli_paths(
            "--describe", "", db_path=self.missing_db
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn(ERROR_PREFIX, proc.stderr)
        self.assertIn(EMPTY_NAME_REASON, proc.stderr)
        self.assertNotIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertFalse(os.path.exists(self.missing_db))

    def test_whitespace_name_with_conflict_reports_mix_not_lookup(self):
        # 仅含空格的表名在有冲突时同样先报混用，不进入查表/开库流程
        proc = self._run_cli_paths(
            "--describe", "   ", "--title", "",
            db_path=self.missing_db,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--describe 仅与 --db 搭配", proc.stderr)
        self.assertIn("--title", proc.stderr)
        self.assertNotIn(EMPTY_NAME_REASON, proc.stderr)
        self.assertNotIn(NOT_FOUND_REASON, proc.stderr)
        self.assertNotIn(OPEN_FAIL_REASON, proc.stderr)
        self.assertFalse(os.path.exists(self.missing_db))

    def test_mixed_command_does_not_read_files_or_touch_existing_output(self):
        # 已存在的输出文件放入固定字节，混用命令后必须逐字节不变
        existing_out = os.path.join(self.tmpdir, "existing.csv")
        with open(existing_out, "wb") as f:
            f.write(b"keep,these,bytes\n")
        # 内容非法的查询文件与参数文件：若被读取必然报各自的内容错误
        bad_sql = os.path.join(self.tmpdir, "bad.sql")
        with open(bad_sql, "w", encoding="utf-8") as f:
            f.write("NOT A SELECT;;;")
        bad_params = os.path.join(self.tmpdir, "bad.json")
        with open(bad_params, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        proc = self._run_cli_paths(
            "--tables",
            "--sql-file", bad_sql,
            "--params-file", bad_params,
            "--output", existing_out,
            db_path=self.missing_db,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--tables 仅与 --db 搭配", proc.stderr)
        error_line = [
            line for line in proc.stderr.splitlines()
            if line.startswith("错误:")
        ][-1]
        self.assertTrue(
            error_line.endswith("--sql-file --output --params-file"),
            proc.stderr,
        )
        # 没有任何文件被读取后产生的下游错误
        self.assertNotIn(SQL_FILE_REASON, proc.stderr)
        self.assertNotIn(PARAMS_FILE_REASON, proc.stderr)
        self.assertNotIn(OPEN_FAIL_REASON, proc.stderr)
        # 既有输出字节不变；非法输入文件字节不变；源库未创建
        with open(existing_out, "rb") as f:
            self.assertEqual(f.read(), b"keep,these,bytes\n")
        with open(bad_sql, "rb") as f:
            self.assertEqual(f.read(), "NOT A SELECT;;;".encode("utf-8"))
        with open(bad_params, "rb") as f:
            self.assertEqual(f.read(), b"{not valid json")
        self.assertFalse(os.path.exists(self.missing_db))
        # 没有产生任何新的输出/临时文件
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            sorted(["existing.csv", "bad.sql", "bad.json"]),
        )


if __name__ == "__main__":
    unittest.main()
