#!/usr/bin/env python3
"""带引号列别名与真实命名参数同时出现时的占位符识别回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
样例库，用例结束后重新以只读方式打开源库，核对表结构与两表全部
数据未变；命令行用例额外核对 --sql-file 查询文件字节未变，随后
清理全部临时文件。

覆盖范围仅限 CSV 导出的参数识别行为：
- 双引号、反引号、方括号三种带特殊字符的列别名（含 :ghost、分号、
  ?1、-- 与 /* 标记）均不被拆改，真正的命名参数 :who 正常绑定；
- 字符串字面量内的 '';:ghost ?1 --/*文本*/ 不被识别为占位符或注释；
- 省略真实参数 who 时拒绝并明确指出缺少 who（不能误报 ghost），
  目标文件不产生；
- 引号外的 ?1 位置占位符即使提供了 who 也拒绝，目标文件不产生；
- 命令行 --sql-file 覆盖一例成功（退出 0、保留数据行数提示）与
  一例缺少 who 的失败（退出 1、标准输出为空、标准错误以“错误: ”开头）。

在项目根目录执行：
    python -m unittest discover
或单独运行：
    python -m unittest test_sql_placeholder_quoting
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

# 同时包含中文、逗号与双引号，与公开样例保持一致
NOTE_VALUE = '中文,含"引号"'

# 第一列别名：命名参数外观、分号、位置占位符与两种注释标记全部关在引号内
ALIAS = "列:ghost;?1--/*标记*/"
# 第三列的字符串字面量（SQL 中 '' 表示一个单引号）与其求值后的文本
TEXT_LITERAL = "'中文'';:ghost ?1 --/*文本*/'"
TEXT_VALUE = "中文';:ghost ?1 --/*文本*/"

EXPECTED_HEADER = [ALIAS, "备注", "文本"]

JOIN_SQL_TEMPLATE = (
    "SELECT p.name AS {alias}, n.note AS 备注, {third} AS 文本 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.name = :who ORDER BY p.id"
)


def quoted_alias(style):
    """按双引号、反引号、方括号三种风格引用同一特殊别名。"""
    if style == "double":
        return '"' + ALIAS.replace('"', '""') + '"'
    if style == "backtick":
        return "`" + ALIAS + "`"
    if style == "bracket":
        return "[" + ALIAS + "]"
    raise ValueError("未知引号风格：%r" % (style,))


def build_sql(style, third_column=TEXT_LITERAL):
    """组装固定联表查询；third_column 可替换为引号外的 ?1 拒绝用例。"""
    return JOIN_SQL_TEMPLATE.format(
        alias=quoted_alias(style), third=third_column
    )


def parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


class _SampleDBFixture(unittest.TestCase):
    """临时 people/notes 样例库；tearDown 只读复核结构与数据未变。"""

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
        """重新以只读方式打开源库，取结构与两表全部数据作为指纹。"""
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

    def _output_path(self, name):
        return os.path.join(self.tmpdir, name)

    @staticmethod
    def _read_csv(path):
        with open(path, encoding="utf-8", newline="") as f:
            return parse_csv(f.read())


class ExportCsvPlaceholderQuotingTest(_SampleDBFixture):
    """export_csv：引号内的伪占位符不识别，引号外的真实参数仍识别。"""

    QUOTING_STYLES = ("double", "backtick", "bracket")

    def test_three_quoting_styles_filter_xiaoming_with_empty_note(self):
        for style in self.QUOTING_STYLES:
            with self.subTest(style=style):
                output = self._output_path("xiaoming_%s.csv" % style)
                count = report.export_csv(
                    self.db_path,
                    build_sql(style),
                    output,
                    params={"who": "小明"},
                )
                # ghost 从未提供也不报错：别名与字面量中的 :ghost 不算参数
                self.assertEqual(count, 1)
                rows = self._read_csv(output)
                # 表头顺序逐项固定：特殊别名原样、备注、文本
                # 数据仅一条：姓名小明、NULL 备注为空字段、文本字面量求值结果
                self.assertEqual(
                    rows,
                    [
                        EXPECTED_HEADER,
                        ["小明", "", TEXT_VALUE],
                    ],
                )

    def test_three_quoting_styles_keep_xiaohong_chinese_note(self):
        for style in self.QUOTING_STYLES:
            with self.subTest(style=style):
                output = self._output_path("xiaohong_%s.csv" % style)
                count = report.export_csv(
                    self.db_path,
                    build_sql(style),
                    output,
                    params={"who": "小红"},
                )
                self.assertEqual(count, 1)
                rows = self._read_csv(output)
                # 含中文、逗号与双引号的备注完整保留，文本列同样不被改写
                self.assertEqual(
                    rows,
                    [
                        EXPECTED_HEADER,
                        ["小红", NOTE_VALUE, TEXT_VALUE],
                    ],
                )

    def test_omitting_real_who_param_is_rejected_and_ghost_not_reported(self):
        output = self._output_path("missing_who.csv")
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, build_sql("double"), output)
        message = str(ctx.exception)
        # 必须指出缺少的是 who；别名/字面量里的 ghost 绝不能被误报
        self.assertIn("缺少", message)
        self.assertIn("who", message)
        self.assertNotIn("ghost", message)
        # 任何拒绝路径都不创建目标文件
        self.assertFalse(os.path.exists(output))

    def test_positional_placeholder_outside_quotes_is_rejected_even_with_who(self):
        # 第三列从字符串字面量改为引号外的 ?1；别名里的 ?1 仍在双引号内
        output = self._output_path("positional.csv")
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(
                self.db_path,
                build_sql("double", third_column="?1"),
                output,
                params={"who": "小明"},
            )
        message = str(ctx.exception)
        self.assertIn("不支持位置占位符", message)
        self.assertFalse(os.path.exists(output))


class CliSqlFilePlaceholderQuotingTest(_SampleDBFixture):
    """命令行 --sql-file：一例成功、一例缺少 who 失败。"""

    def setUp(self):
        super().setUp()
        self.sql_path = os.path.join(self.tmpdir, "query.sql")
        with open(self.sql_path, "w", encoding="utf-8") as f:
            f.write(build_sql("double") + ";\n")
        with open(self.sql_path, "rb") as f:
            self._sql_bytes = f.read()

    def tearDown(self):
        # 查询文件始终只被读取，字节不变
        with open(self.sql_path, "rb") as f:
            self.assertEqual(f.read(), self._sql_bytes)
        super().tearDown()

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_sql_file_success_exports_one_row_with_count_hint(self):
        output = self._output_path("cli_success.csv")
        proc = self._run_cli(
            "--sql-file", self.sql_path,
            "--param", "who=小明",
            "--output", output,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 保留数据行数提示
        self.assertIn("已导出 1 行数据", proc.stdout)
        rows = self._read_csv(output)
        self.assertEqual(
            rows,
            [EXPECTED_HEADER, ["小明", "", TEXT_VALUE]],
        )

    def test_sql_file_missing_who_fails_with_empty_stdout_and_error_prefix(self):
        output = self._output_path("cli_missing_who.csv")
        proc = self._run_cli(
            "--sql-file", self.sql_path,
            "--output", output,
        )
        self.assertEqual(proc.returncode, 1)
        # 失败时标准输出为空，错误走标准错误且以“错误: ”开头
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "))
        self.assertIn("who", proc.stderr)
        self.assertNotIn("ghost", proc.stderr)
        self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
