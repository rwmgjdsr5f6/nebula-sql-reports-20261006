#!/usr/bin/env python3
"""--params-file 参数文件与命令行 --param 合并的可重复回归测试。

只依赖 Python 标准库；每个用例在临时目录中自行准备 people/notes
样例库与参数文件，用例结束后重新只读打开源库核对表结构与全部数据
未变，并核对参数文件字节与目录内容相对基线不变，随后清理全部临时
文件。

覆盖要点：
- read_params_file：UTF-8/单 BOM、恰一个 JSON 对象、键名规则、值全部
  为字符串、重复键拒绝、各类非对象/非字符串值拒绝、编码与 IO 错误；
- merge_params：命令行值覆盖文件值（含空字符串覆盖非空值）、不修改
  入参、与选项顺序无关；
- 命令行：参数文件同时服务于 --preview、CSV 导出与 HTML 导出，合并
  结果与直接使用 --param 一致；混用、重复提供、空路径先拒绝且不读取
  参数文件；参数文件先于查询文件、源库与输出目标校验，失败不建文件。

在项目根目录执行：
    python -m unittest discover
或单独运行：
    python -m unittest test_params_file
"""

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

# 同时包含中文、逗号与双引号，用于验证 CSV 解析后完整还原与 HTML 转义
NOTE_VALUE = '中文,含"引号"'

PARAM_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.name = :who ORDER BY p.id"
)
POSITIONAL_SQL = "SELECT name AS 姓名 FROM people WHERE id = ?"


class ParamsFileTestCase(unittest.TestCase):
    """read_params_file 函数级校验与 merge_params 合并规则。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content):
        """以原始字节或文本写入临时文件，返回路径。"""
        path = os.path.join(self.tmpdir, name)
        if isinstance(content, str):
            content = content.encode("utf-8")
        with open(path, "wb") as f:
            f.write(content)
        return path

    # -- 合法文件 --------------------------------------------------------

    def test_simple_object_returns_str_to_str_dict(self):
        path = self._write("p.json", '{"who":"小明"}')
        self.assertEqual(report.read_params_file(path), {"who": "小明"})

    def test_leading_bom_is_accepted(self):
        path = self._write("bom.json", b"\xef\xbb\xbf" + '{"who":"小红"}'.encode("utf-8"))
        self.assertEqual(report.read_params_file(path), {"who": "小红"})

    def test_empty_object_returns_empty_dict(self):
        path = self._write("empty.json", "{}")
        self.assertEqual(report.read_params_file(path), {})

    def test_empty_string_value_is_valid(self):
        path = self._write("v.json", '{"who":""}')
        self.assertEqual(report.read_params_file(path), {"who": ""})

    def test_decoded_string_text_is_preserved(self):
        # 中文、首尾空格、换行（JSON 转义）、等号、引号、分号全部原样保留
        value = ' 小红\n第二行 a=b;"q"; '
        path = self._write("v.json", json.dumps({"who": value}, ensure_ascii=False))
        self.assertEqual(report.read_params_file(path), {"who": value})

    def test_key_names_follow_existing_param_rules_and_case(self):
        path = self._write("v.json", '{"_a1":"x","Who":"y","WHO":"z"}')
        self.assertEqual(
            report.read_params_file(path), {"_a1": "x", "Who": "y", "WHO": "z"}
        )

    # -- 顶层必须是对象 --------------------------------------------------

    def test_non_object_toplevel_rejected(self):
        for content in ("[]", '["a"]', "12", "true", "false", "null", '"str"',
                        "1.5"):
            with self.subTest(content=content):
                path = self._write("bad.json", content)
                with self.assertRaises(ValueError):
                    report.read_params_file(path)

    # -- 值必须全部是字符串 ----------------------------------------------

    def test_non_string_values_rejected(self):
        for content in ('{"who":1}', '{"who":1.5}', '{"who":true}',
                        '{"who":false}', '{"who":null}', '{"who":["x"]}',
                        '{"who":{"a":"b"}}'):
            with self.subTest(content=content):
                path = self._write("bad.json", content)
                with self.assertRaises(ValueError):
                    report.read_params_file(path)

    def test_one_illegal_value_rejects_whole_file_even_with_legal_entry(self):
        path = self._write("bad.json", '{"who":"小明","n":1}')
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    # -- 键名与重复键 ----------------------------------------------------

    def test_invalid_keys_rejected(self):
        for content in ('{"1who":"x"}', '{":who":"x"}', '{"a-b":"x"}',
                        '{"谁":"x"}'):
            with self.subTest(content=content):
                path = self._write("bad.json", content)
                with self.assertRaises(ValueError):
                    report.read_params_file(path)

    def test_duplicate_keys_rejected(self):
        path = self._write("dup.json", '{"who":"小明","who":"小红"}')
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    def test_duplicate_keys_in_nested_shape_still_rejected(self):
        # 嵌套对象本身已是非法值；同键重复在解析阶段先被拦截，同样失败
        path = self._write("dup.json", '{"a":{"x":1,"x":2}}')
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    # -- 编码、JSON 语法与 IO --------------------------------------------

    def test_invalid_json_syntax_rejected(self):
        path = self._write("bad.json", "{who:")
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    def test_literal_control_char_in_json_string_rejected(self):
        # 未转义的真实换行位于 JSON 字符串内属于语法错误
        path = self._write("bad.json", b'{"who":"a\nb"}')
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    def test_invalid_utf8_rejected(self):
        path = self._write("badenc.json", b'{"who":"\xff"}')
        with self.assertRaises(ValueError):
            report.read_params_file(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            report.read_params_file(os.path.join(self.tmpdir, "nope.json"))

    def test_directory_rejected(self):
        with self.assertRaises(ValueError):
            report.read_params_file(self.tmpdir)

    def test_unreadable_file_rejected(self):
        path = self._write("noread.json", '{"who":"x"}')
        os.chmod(path, 0)
        try:
            # root 可绕过权限位；普通用户下 open 必失败
            if os.geteuid() != 0:
                with self.assertRaises(ValueError):
                    report.read_params_file(path)
        finally:
            os.chmod(path, 0o600)


class MergeParamsTestCase(unittest.TestCase):
    """文件参数与命令行参数的合并规则（纯函数，无需临时库）。"""

    def test_neither_present_returns_none(self):
        self.assertIsNone(report.merge_params(None, None))
        self.assertIsNone(report.merge_params({}, {}))
        self.assertIsNone(report.merge_params({}, None))

    def test_file_only(self):
        self.assertEqual(report.merge_params({"who": "小明"}, None),
                         {"who": "小明"})

    def test_cli_only(self):
        self.assertEqual(report.merge_params(None, {"who": "小红"}),
                         {"who": "小红"})

    def test_cli_value_overrides_file_value(self):
        merged = report.merge_params({"who": "小明", "tag": "a"},
                                     {"who": "小红"})
        self.assertEqual(merged, {"who": "小红", "tag": "a"})

    def test_cli_empty_string_overrides_nonempty_file_value(self):
        merged = report.merge_params({"who": "小明"}, {"who": ""})
        self.assertEqual(merged, {"who": ""})

    def test_inputs_are_not_mutated(self):
        file_params = {"who": "小明"}
        cli_params = {"who": "小红"}
        report.merge_params(file_params, cli_params)
        self.assertEqual(file_params, {"who": "小明"})
        self.assertEqual(cli_params, {"who": "小红"})

    def test_cli_duplicate_is_handled_before_merge_by_parser(self):
        # 同名 --param 重复仍在 parse_param_options 阶段拒绝，与是否有
        # 文件参数无关
        parser = report._Parser()
        with self.assertRaises(SystemExit):
            report.parse_param_options(["who=a", "who=b"], parser)


class ParamsFileCliTestCase(unittest.TestCase):
    """--params-file 在预览、CSV 与 HTML 命令行入口上的端到端行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self.sql_path = os.path.join(self.tmpdir, "query.sql")
        with open(self.sql_path, "w", encoding="utf-8") as f:
            f.write(PARAM_SQL + ";\n")
        with open(self.sql_path, "rb") as f:
            self._sql_bytes = f.read()
        self._prepare_db()
        self._baseline = self._snapshot_db()

    def tearDown(self):
        self.assertEqual(self._snapshot_db(), self._baseline)
        # 查询文件始终只被读取
        with open(self.sql_path, "rb") as f:
            self.assertEqual(f.read(), self._sql_bytes)
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

    def _write_params(self, name, content):
        path = os.path.join(self.tmpdir, name)
        if isinstance(content, str):
            content = content.encode("utf-8")
        with open(path, "wb") as f:
            f.write(content)
        return path

    @staticmethod
    def _parse_csv(text):
        return list(csv.reader(io.StringIO(text)))

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True, text=True, encoding="utf-8",
        )

    # -- 验收场景：预览 --------------------------------------------------

    def test_preview_with_params_file_shows_xiaoming_empty_note(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 表头 姓名、备注；一条记录：小明与空备注（NULL 默认空字段）
        self.assertEqual(
            self._parse_csv(proc.stdout), [["姓名", "备注"], ["小明", ""]]
        )

    def test_preview_file_value_then_cli_overrides_to_xiaohong(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--param", "who=小红", "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self._parse_csv(proc.stdout)
        # 含中文、逗号与双引号的备注经 CSV 解析后完整还原
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])

    def test_preview_with_inline_sql_also_accepts_params_file(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        proc = self._run_cli("--sql", PARAM_SQL,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._parse_csv(proc.stdout), [["姓名", "备注"], ["小明", ""]]
        )

    def test_params_file_bytes_unchanged_after_preview(self):
        raw = '{"who":"小明"}'.encode("utf-8")
        params = self._write_params("who.json", raw)
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(params, "rb") as f:
            self.assertEqual(f.read(), raw)

    # -- 验收场景：CSV 导出与合并一致性 ----------------------------------

    def test_csv_export_with_params_file_matches_direct_param(self):
        params = self._write_params("who.json", '{"who":"小红"}')
        out_file = os.path.join(self.tmpdir, "from_file.csv")
        out_direct = os.path.join(self.tmpdir, "from_cli.csv")
        p1 = self._run_cli("--sql-file", self.sql_path,
                           "--params-file", params, "--output", out_file)
        p2 = self._run_cli("--sql-file", self.sql_path,
                           "--param", "who=小红", "--output", out_direct)
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        # 成功提示仅输出路径不同；产物逐字节一致
        with open(out_file, "rb") as f:
            file_bytes = f.read()
        with open(out_direct, "rb") as f:
            direct_bytes = f.read()
        self.assertEqual(file_bytes, direct_bytes)
        self.assertIn("已导出 1 行数据", p1.stdout)
        rows = self._parse_csv(file_bytes.decode("utf-8"))
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])

    def test_csv_export_merged_params_match_equivalent_param_set(self):
        # 文件给 who=小明 与一个未引用参数；命令行覆盖 who=小红
        params = self._write_params(
            "p.json", '{"who":"小明","extra":"忽略"}'
        )
        out_file = os.path.join(self.tmpdir, "merged.csv")
        out_ref = os.path.join(self.tmpdir, "ref.csv")
        p1 = self._run_cli("--sql-file", self.sql_path,
                           "--params-file", params,
                           "--param", "who=小红", "--output", out_file)
        p2 = self._run_cli("--sql-file", self.sql_path,
                           "--param", "who=小红",
                           "--param", "extra=忽略", "--output", out_ref)
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        with open(out_file, "rb") as f:
            merged_bytes = f.read()
        with open(out_ref, "rb") as f:
            ref_bytes = f.read()
        # 未被查询引用的文件参数同样忽略；合并结果与直接 --param 一致
        self.assertEqual(merged_bytes, ref_bytes)

    # -- 验收场景：HTML 导出 ---------------------------------------------

    def test_html_export_escapes_file_and_cli_values(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        out_file = os.path.join(self.tmpdir, "from_file.html")
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--format", "html", "--output", out_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_file, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("<title>查询报告</title>", page)
        self.assertIn("<th>姓名</th>", page)
        self.assertIn("<th>备注</th>", page)
        self.assertIn("<td>小明</td>", page)

        out2 = os.path.join(self.tmpdir, "override.html")
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--param", "who=小红",
                             "--format", "html", "--output", out2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out2, encoding="utf-8") as f:
            page2 = f.read()
        self.assertIn("<td>小红</td>", page2)
        # 备注中的 &、<、>、双引号、单引号按既有规则转义；此处含双引号
        self.assertIn("中文,含&quot;引号&quot;", page2)
        self.assertNotIn(NOTE_VALUE, page2)  # 原文双引号不应原样出现于标签内

    # -- 覆盖与顺序 ------------------------------------------------------

    def test_cli_overrides_file_regardless_of_option_order(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        for extra in (
            ("--params-file", params, "--param", "who=小红"),
            ("--param", "who=小红", "--params-file", params),
        ):
            with self.subTest(order=extra):
                proc = self._run_cli("--sql-file", self.sql_path, *extra,
                                     "--preview", "1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    self._parse_csv(proc.stdout)[1], ["小红", NOTE_VALUE]
                )

    def test_cli_empty_string_overrides_nonempty_file_value(self):
        params = self._write_params("who.json", '{"who":"小红"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--param", "who=", "--preview", "5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 空字符串是文本值：不匹配任何姓名，只剩表头
        self.assertEqual(self._parse_csv(proc.stdout), [["姓名", "备注"]])

    def test_illegal_file_entry_cannot_be_bypassed_by_cli_override(self):
        params = self._write_params("bad.json", '{"who":1}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--param", "who=小红", "--preview", "5")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("错误", proc.stderr)
        self.assertIn("字符串", proc.stderr)

    def test_duplicate_cli_param_still_rejected_with_params_file(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--param", "who=a", "--param", "who=b",
                             "--preview", "5")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("重复提供同名参数", proc.stderr)

    # -- 合并后仍按既有参数规则失败 --------------------------------------

    def test_empty_object_with_referenced_param_is_missing_error(self):
        params = self._write_params("empty.json", "{}")
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("缺少查询引用的参数", proc.stderr)
        self.assertIn("who", proc.stderr)

    def test_unreferenced_legal_file_param_is_ignored(self):
        params = self._write_params(
            "p.json", '{"who":"小红","other":"x"}'
        )
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._parse_csv(proc.stdout)[1], ["小红", NOTE_VALUE]
        )

    def test_positional_placeholder_rejected_with_file_params(self):
        params = self._write_params("p.json", '{"id":"1"}')
        sql_path = os.path.join(self.tmpdir, "pos.sql")
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(POSITIONAL_SQL)
        proc = self._run_cli("--sql-file", sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("位置占位符", proc.stderr)

    def test_file_param_names_are_case_sensitive(self):
        params = self._write_params("p.json", '{"Who":"小红"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("缺少查询引用的参数", proc.stderr)
        self.assertIn("who", proc.stderr)

    # -- 选项结构与混用：先拒绝且不读取参数文件 --------------------------

    def test_repeated_params_file_rejected_without_reading(self):
        params = self._write_params("who.json", '{"who":"小明"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("参数错误", proc.stderr)
        self.assertIn("只能提供一次", proc.stderr)

    def test_empty_params_file_path_rejected_without_reading(self):
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", "", "--preview", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("路径不能为空", proc.stderr)

    def test_params_file_mixed_with_tables_rejected_before_reading(self):
        # 文件不存在也必须先报混用，证明该判定不读取参数文件
        missing = os.path.join(self.tmpdir, "nope.json")
        proc = self._run_cli("--tables", "--params-file", missing)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--tables", proc.stderr)
        self.assertNotIn("参数文件不存在", proc.stderr)

    def test_params_file_mixed_with_describe_rejected_before_reading(self):
        missing = os.path.join(self.tmpdir, "nope.json")
        proc = self._run_cli("--describe", "people",
                             "--params-file", missing)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--describe", proc.stderr)
        self.assertNotIn("参数文件不存在", proc.stderr)

    # -- 文件错误：退出码 1、stdout 为空、不建任何文件 -------------------

    def _assert_file_error(self, raw_bytes, expect_reason=None):
        params = self._write_params("bad.json", raw_bytes)
        out = os.path.join(self.tmpdir, "out.csv")
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--output", out)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "), proc.stderr)
        if expect_reason is not None:
            self.assertIn(expect_reason, proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_bad_json_and_structure_errors_exit_one_empty_stdout(self):
        cases = [
            (b'{"who":1}', "字符串"),
            (b'{"who":true}', "字符串"),
            (b'{"who":null}', "字符串"),
            (b'{"who":["x"]}', "字符串"),
            (b'{"who":{"a":"b"}}', "字符串"),
            (b'["x"]', "JSON 对象"),
            (b'{"1who":"x"}', "参数名"),
            (b'{"who":"a","who":"b"}', "重复"),
            (b"{not json", "合法的 JSON"),
            (b'{"who":"\xff"}', "UTF-8"),
        ]
        for raw, reason in cases:
            with self.subTest(raw=raw):
                self._assert_file_error(raw, reason)

    def test_missing_params_file_exit_one_empty_stdout_no_output(self):
        out = os.path.join(self.tmpdir, "out.csv")
        proc = self._run_cli(
            "--sql-file", self.sql_path,
            "--params-file", os.path.join(self.tmpdir, "nope.json"),
            "--output", out,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("错误: "))
        self.assertIn("参数文件不存在", proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_directory_params_file_exit_one_empty_stdout_no_output(self):
        out = os.path.join(self.tmpdir, "out.csv")
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", self.tmpdir, "--output", out)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("目录", proc.stderr)
        self.assertFalse(os.path.exists(out))

    # -- 校验顺序：先参数文件，再查询文件、源库、输出目标 -----------------

    def test_params_file_validated_before_sql_file_and_db(self):
        bad = self._write_params("bad.json", '{"who":1}')
        # 查询文件与源库都不存在；必须先报告参数文件的内容错误
        proc = self._run_cli(
            "--sql-file", os.path.join(self.tmpdir, "missing.sql"),
            "--params-file", bad,
            "--db", os.path.join(self.tmpdir, "missing.sqlite"),
            "--output", os.path.join(self.tmpdir, "out.csv"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("字符串", proc.stderr)
        # 失败不创建源库或输出
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "missing.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "out.csv")))

    def test_params_file_error_reported_before_output_conflict(self):
        # 参数文件非法时不应走到输出目标占用判定：目标已存在也先报参数错，
        # 且既有文件字节不变
        bad = self._write_params("bad.json", '{"who":1}')
        out = os.path.join(self.tmpdir, "existing.csv")
        marker = b"original,bytes\n"
        with open(out, "wb") as f:
            f.write(marker)
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", bad, "--output", out)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("字符串", proc.stderr)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), marker)

    # -- 无新选项时的兼容性 ----------------------------------------------

    def test_without_params_file_preview_unchanged_with_cli_param(self):
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--param", "who=小明", "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._parse_csv(proc.stdout), [["姓名", "备注"], ["小明", ""]]
        )

    def test_params_file_does_not_change_source_db_or_create_temp_files(self):
        # 正常预览成功后，目录中不应多出除样例输入之外的报告/临时文件
        before = sorted(
            n for n in os.listdir(self.tmpdir) if n.endswith(".json")
        )
        params = self._write_params("who.json", '{"who":"小红"}')
        proc = self._run_cli("--sql-file", self.sql_path,
                             "--params-file", params, "--preview", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = sorted(
            n for n in os.listdir(self.tmpdir) if n.endswith(".json")
        )
        self.assertEqual(after, before + ["who.json"])


if __name__ == "__main__":
    unittest.main()
