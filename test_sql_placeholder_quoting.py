#!/usr/bin/env python3
"""带引号列别名与真实命名参数同时出现时的参数识别回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
两张小型 SQLite 表（沿用公开样例的 id/name 与 person_id/note 关联
字段），用例结束后重新只读打开源库，逐项核对表结构与两表全部数据
相对用例开始时的快照未变；命令行用例还核对 --sql-file 查询文件的
字节自始至终未被改动。全部临时文件由 TemporaryDirectory 清理，不依赖
预置数据库或公网。

覆盖要点（范围只限 CSV 导出的参数识别行为，不新增查询语法或选项）：
- 双引号、反引号、方括号三种引号写法把第一列别名设为
  “列:ghost;?1--/*标记*/”，第三列文本表达式整体位于单引号字符串内
  （'中文'';:ghost ?1 --/*文本*/'）；三种写法下别名与字符串内的
  :ghost、?1、分号与注释标记都不得被当作参数或注释，ghost 无需提供，
  真实参数只有引号外的 :who，查询不得被拆分或删改；
- who=小明 export_csv 返回 1，CSV 解析后恰有表头与一条数据，NULL
  备注为空字段，第三列文本逐字保留；who=小红 时中文备注完整保留；
- 相邻的拒绝用例：省略真实 who 报“缺少…who”，不误报 ghost，目标
  文件不产生；第三列改为引号外的 ?1 时，即使提供 who 也报“不支持
  位置占位符”，目标同样不存在；
- 命令行经 --sql-file 走一例成功（退出 0、保留“已导出 N 行数据”
  提示）与一例缺少 who 的失败（退出 1、stdout 为空、stderr 以
  “错误: ”开头）。

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

# 别名内的“伪占位符”：冒号名、分号、位置占位符、行注释与块注释标记
ALIAS_TEXT = "列:ghost;?1--/*标记*/"
# 第三列是一个完整的单引号字符串表达式：单引号内双写单引号输出一个
# 单引号，故求值结果为 中文';:ghost ?1 --/*文本*/
TEXT_EXPR = "'中文'';:ghost ?1 --/*文本*/'"
TEXT_VALUE = "中文';:ghost ?1 --/*文本*/"
# 小红的中文备注同时含逗号与双引号，用于核对 CSV 解析后完整还原
NOTE_VALUE = '中文,含"引号"'

EXPECTED_HEADER = [ALIAS_TEXT, "备注", "文本"]


def _build_select(alias_quote):
    """生成三种引号包裹别名的固定联表查询（第三列表达式位于字符串内）。"""
    return (
        "SELECT p.name AS %s, n.note AS 备注, %s AS 文本 "
        "FROM people p JOIN notes n ON p.id = n.person_id "
        "WHERE p.name = :who ORDER BY p.id"
        % (alias_quote % ALIAS_TEXT, TEXT_EXPR)
    )


def _build_positional_select():
    """第三列改为引号外的 ?1，用于证明引号外的位置占位符仍被识别。"""
    return (
        "SELECT p.name AS 姓名, n.note AS 备注, ?1 AS 文本 "
        "FROM people p JOIN notes n ON p.id = n.person_id "
        "WHERE p.name = :who ORDER BY p.id"
    )


# 三种引号写法；圆括号在 Python 字符串模板中无需转义
QUOTED_ALIASES = [
    ("double_quote", '"%s"'),
    ("backtick", "`%s`"),
    ("square_bracket", "[%s]"),
]


class _SampleDBMixin:
    """临时目录、样例库与源库快照/复核共用逻辑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 无论用例成功还是失败，都重新只读打开源库核对结构与数据未变
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
        """只读打开源库，取表结构（含建表 SQL）与两表全部数据。"""
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
            return list(csv.reader(f))

    @staticmethod
    def _parse_csv_text(text):
        return list(csv.reader(io.StringIO(text)))


class QuotedAliasExportTestCase(_SampleDBMixin, unittest.TestCase):
    """三种引号别名与真实 :who 参数并存时的 CSV 导出。"""

    def _assert_placeholder_scan(self, sql_text):
        # 词法扫描只能看到引号外的 who；ghost 与 ?1 都在引号内
        statement = report.validate_single_select(sql_text)
        named, positional = report.find_placeholders(statement)
        self.assertEqual(named, ["who"])
        self.assertFalse(positional)

    def test_ghost_in_quoted_alias_and_string_is_not_a_param(self):
        for label, quote_template in QUOTED_ALIASES:
            with self.subTest(style=label):
                sql_text = _build_select(quote_template)
                self._assert_placeholder_scan(sql_text)

    def test_export_xiaoming_empty_note_row_count_and_cells(self):
        for label, quote_template in QUOTED_ALIASES:
            with self.subTest(style=label):
                sql_text = _build_select(quote_template)
                out = os.path.join(self.tmpdir, "out_%s.csv" % label)
                row_count = report.export_csv(
                    self.db_path, sql_text, out, {"who": "小明"}
                )
                self.assertEqual(row_count, 1)
                records = self._read_csv(out)
                # 恰有表头与一条数据
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0], EXPECTED_HEADER)
                self.assertEqual(
                    records[1], ["小明", "", TEXT_VALUE]
                )

    def test_export_xiaohong_chinese_note_fully_preserved(self):
        for label, quote_template in QUOTED_ALIASES:
            with self.subTest(style=label):
                sql_text = _build_select(quote_template)
                out = os.path.join(self.tmpdir, "hong_%s.csv" % label)
                row_count = report.export_csv(
                    self.db_path, sql_text, out, {"who": "小红"}
                )
                self.assertEqual(row_count, 1)
                records = self._read_csv(out)
                self.assertEqual(records[0], EXPECTED_HEADER)
                # 中文、逗号与引号备注经 CSV 解析后完整保留
                self.assertEqual(
                    records[1], ["小红", NOTE_VALUE, TEXT_VALUE]
                )

    def test_extra_ghost_value_is_ignored_like_any_unreferenced_param(self):
        # ghost 本就不是查询引用的参数；即便顺手传入也按“未引用参数忽略”
        sql_text = _build_select('"%s"')
        out = os.path.join(self.tmpdir, "ghost_extra.csv")
        row_count = report.export_csv(
            self.db_path, sql_text, out,
            {"who": "小明", "ghost": "不应参与绑定"},
        )
        self.assertEqual(row_count, 1)
        records = self._read_csv(out)
        self.assertEqual(records[1], ["小明", "", TEXT_VALUE])

    def test_quoted_semicolon_does_not_split_statement(self):
        # 别名引号内的分号不得被 split_statements 当成语句分隔：
        # 三种写法都必须通过“恰一条 SELECT”校验并可由 SQLite 执行
        for label, quote_template in QUOTED_ALIASES:
            with self.subTest(style=label):
                sql_text = _build_select(quote_template)
                statement = report.validate_single_select(sql_text)
                segments = report.split_statements(
                    report.strip_sql_comments(statement), keep_empty=True
                )
                nonempty = [s for s in segments if s]
                self.assertEqual(len(nonempty), 1)


class QuotedAliasRejectTestCase(_SampleDBMixin, unittest.TestCase):
    """引号外的参数仍被识别：缺 who 与引号外 ?1 两条相邻拒绝用例。"""

    def test_missing_who_reports_who_not_ghost_and_creates_nothing(self):
        for label, quote_template in QUOTED_ALIASES:
            with self.subTest(style=label):
                out = os.path.join(self.tmpdir, "missing_%s.csv" % label)
                sql_text = _build_select(quote_template)
                with self.assertRaises(ValueError) as ctx:
                    report.export_csv(self.db_path, sql_text, out, None)
                message = str(ctx.exception)
                self.assertIn("缺少", message)
                self.assertIn("who", message)
                self.assertNotIn("ghost", message)
                # 拒绝路径不得创建目标文件
                self.assertFalse(os.path.exists(out))

    def test_missing_who_with_empty_params_dict_same_result(self):
        sql_text = _build_select('"%s"')
        out = os.path.join(self.tmpdir, "missing_empty.csv")
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, sql_text, out, {})
        message = str(ctx.exception)
        self.assertIn("who", message)
        self.assertNotIn("ghost", message)
        self.assertFalse(os.path.exists(out))

    def test_positional_outside_quotes_rejected_even_with_who(self):
        for params in ({"who": "小明"}, {"who": "小红"}):
            with self.subTest(params=params):
                out = os.path.join(
                    self.tmpdir, "pos_%s.csv" % params["who"]
                )
                sql_text = _build_positional_select()
                with self.assertRaises(ValueError) as ctx:
                    report.export_csv(self.db_path, sql_text, out, params)
                message = str(ctx.exception)
                self.assertIn("位置占位符", message)
                self.assertFalse(os.path.exists(out))

    def test_positional_rejection_takes_precedence_over_missing_who(self):
        # 同时存在引号外 ?1 与未提供 who 时，先报位置占位符，且不建文件
        out = os.path.join(self.tmpdir, "pos_missing.csv")
        sql_text = _build_positional_select()
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(self.db_path, sql_text, out, None)
        self.assertIn("位置占位符", str(ctx.exception))
        self.assertFalse(os.path.exists(out))


class SqlFileCliTestCase(_SampleDBMixin, unittest.TestCase):
    """--sql-file 命令行入口的成功与失败两例。"""

    def setUp(self):
        super().setUp()
        self.sql_path = os.path.join(self.tmpdir, "query.sql")
        sql_text = _build_select('"%s"') + "\n"
        with open(self.sql_path, "w", encoding="utf-8") as f:
            f.write(sql_text)
        with open(self.sql_path, "rb") as f:
            self._sql_bytes = f.read()

    def tearDown(self):
        # 查询文件自始至终只被读取，字节不变
        with open(self.sql_path, "rb") as f:
            self.assertEqual(f.read(), self._sql_bytes)
        super().tearDown()

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path,
             "--sql-file", self.sql_path] + list(extra),
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_sql_file_success_exits_zero_keeps_row_count_hint(self):
        out = os.path.join(self.tmpdir, "cli_out.csv")
        proc = self._run_cli(
            "--param", "who=小明", "--output", out
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 保留既有的数据行数提示
        self.assertEqual(
            proc.stdout, "已导出 1 行数据：%s\n" % out
        )
        records = self._read_csv(out)
        self.assertEqual(records, [
            EXPECTED_HEADER,
            ["小明", "", TEXT_VALUE],
        ])

    def test_sql_file_missing_who_fails_exit_one_empty_stdout(self):
        out = os.path.join(self.tmpdir, "cli_missing.csv")
        proc = self._run_cli("--output", out)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "), proc.stderr)
        self.assertIn("who", proc.stderr)
        self.assertNotIn("ghost", proc.stderr)
        # 失败不产生目标文件
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
