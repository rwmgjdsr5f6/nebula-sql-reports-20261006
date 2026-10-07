#!/usr/bin/env python3
"""--params-file 参数文件入口的回归测试：读取校验、合并与三种输出共用。

只依赖 Python 标准库；在临时目录中自行准备 people/notes 小型 SQLite 库
与参数文件，用例结束后清理全部临时文件，不依赖仓库中的预置数据文件。

在项目根目录执行：
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

JOIN_WHO_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id=n.person_id "
    "WHERE p.name = :who ORDER BY p.id"
)
# 同时包含中文、逗号与双引号，用于验证 CSV 字段引用与 HTML 转义
NOTE_VALUE = '中文,含"引号"'


class ParamsFileFixture(unittest.TestCase):
    """提供 people/notes 样例库、参数文件写入与源库不变性核对。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 成功或失败后源库都必须与准备完成时一致
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

    def _write_params(self, data, name="params.json", raw=False):
        """写参数文件：bytes 原样写，str 按 UTF-8 写，dict/list 以 JSON 写。

        dict/list 使用 JSON 序列化（不 ASCII 转义）。返回 (路径, 原始字节)。
        raw 仅用于表明调用方给出的是手工构造的载荷，不改变编码方式。
        """
        del raw  # 编码规则统一：str 一律 UTF-8，bytes 原样
        path = os.path.join(self.tmpdir, name)
        if isinstance(data, (dict, list)):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            payload = data.encode("utf-8")
        else:
            payload = data
        with open(path, "wb") as f:
            f.write(payload)
        return path, payload

    @staticmethod
    def _read_csv_bytes(path):
        with open(path, "rb") as f:
            data = f.read()
        return data, list(csv.reader(io.StringIO(data.decode("utf-8"))))

    def _run_cli(self, sql_args, params_file=None, extra_params=(),
                 output=None, preview=None, fmt=None,
                 description=None, title=None, tables=False, describe=None):
        cmd = [sys.executable, REPORT_PY, "--db", self.db_path]
        if tables:
            cmd += ["--tables"]
        if describe is not None:
            cmd += ["--describe", describe]
        cmd += sql_args
        if params_file is not None:
            for path in (params_file if isinstance(params_file, list)
                         else [params_file]):
                cmd += ["--params-file", path]
        for item in extra_params:
            cmd += ["--param", item]
        if fmt is not None:
            cmd += ["--format", fmt]
        if description is not None:
            cmd += ["--description", description]
        if title is not None:
            cmd += ["--title", title]
        if preview is not None:
            cmd += ["--preview", str(preview)]
        if output is not None:
            cmd += ["--output", output]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )


# -- read_params_file 函数级校验 -------------------------------------------

class ReadParamsFileTests(ParamsFileFixture):

    def test_simple_object(self):
        path, _ = self._write_params({"who": "小明"})
        self.assertEqual(report.read_params_file(path), {"who": "小明"})

    def test_bom_allowed(self):
        path = os.path.join(self.tmpdir, "bom.json")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + '{"who":"小红"}'.encode("utf-8"))
        self.assertEqual(report.read_params_file(path), {"who": "小红"})

    def test_empty_object_valid(self):
        path, _ = self._write_params({})
        self.assertEqual(report.read_params_file(path), {})

    def test_empty_string_value_valid(self):
        path, _ = self._write_params({"who": "", "also": ""})
        self.assertEqual(report.read_params_file(path), {"who": "", "also": ""})

    def test_string_value_preserved_verbatim(self):
        value = ' 前空格 尾 \n等号=a 引号"q" 分号;\t'
        path, _ = self._write_params({"v": value})
        self.assertEqual(report.read_params_file(path), {"v": value})

    def test_case_sensitive_keys_distinct(self):
        path, _ = self._write_params({"who": "a", "Who": "b", "WHO": "c"})
        self.assertEqual(
            report.read_params_file(path), {"who": "a", "Who": "b", "WHO": "c"}
        )

    def test_missing_file(self):
        with self.assertRaises(ValueError) as ctx:
            report.read_params_file(os.path.join(self.tmpdir, "nope.json"))
        self.assertIn("参数文件不存在", str(ctx.exception))

    def test_directory_path(self):
        with self.assertRaises(ValueError) as ctx:
            report.read_params_file(self.tmpdir)
        self.assertIn("目录", str(ctx.exception))

    def test_bad_utf8(self):
        path, _ = self._write_params(b"\xff\xfe not utf8", raw=True)
        with self.assertRaises(ValueError) as ctx:
            report.read_params_file(path)
        self.assertIn("UTF-8", str(ctx.exception))

    def test_json_syntax_error(self):
        path, _ = self._write_params("{not json", raw=True)
        with self.assertRaises(ValueError) as ctx:
            report.read_params_file(path)
        self.assertIn("合法的 JSON", str(ctx.exception))

    def test_root_must_be_object(self):
        for payload, kind in [
            ("[]", "list"), ("\"x\"", "str"), ("42", "int"),
            ("true", "bool"), ("false", "bool"), ("null", "NoneType"),
        ]:
            with self.subTest(payload=payload):
                path, _ = self._write_params(payload, raw=True)
                with self.assertRaises(ValueError) as ctx:
                    report.read_params_file(path)
                self.assertIn("JSON 对象", str(ctx.exception))
                self.assertIn(kind, str(ctx.exception))

    def test_non_string_values_rejected(self):
        for payload, kind in [
            ('{"who":1}', "int"),
            ('{"who":true}', "bool"),
            ('{"who":null}', "NoneType"),
            ('{"a":["x"]}', "list"),
            ('{"a":{"who":"x"}}', "dict"),
        ]:
            with self.subTest(payload=payload):
                path, _ = self._write_params(payload, raw=True)
                with self.assertRaises(ValueError) as ctx:
                    report.read_params_file(path)
                self.assertIn("必须是字符串", str(ctx.exception))
                self.assertIn(kind, str(ctx.exception))

    def test_duplicate_keys_rejected(self):
        path, _ = self._write_params('{"who":"a","who":"b"}', raw=True)
        with self.assertRaises(ValueError) as ctx:
            report.read_params_file(path)
        self.assertIn("重复键", str(ctx.exception))
        self.assertIn("who", str(ctx.exception))

    def test_invalid_key_names_rejected(self):
        for payload in ['{"1who":"x"}', '{"who x":"x"}',
                        '{"who-x":"x"}', '{"谁":"x"}']:
            with self.subTest(payload=payload):
                path, _ = self._write_params(payload, raw=True)
                with self.assertRaises(ValueError) as ctx:
                    report.read_params_file(path)
                self.assertIn("非法参数名", str(ctx.exception))

    def test_trailing_content_rejected(self):
        for payload in ['{"who":"x"} garbage', '{"who":"x"}{"a":"b"}',
                        '{"who":"x"} 1', '{"who":"x"};']:
            with self.subTest(payload=payload):
                path, _ = self._write_params(payload, raw=True)
                with self.assertRaises(ValueError) as ctx:
                    report.read_params_file(path)
                self.assertIn("多余内容", str(ctx.exception))

    def test_file_only_read_not_modified(self):
        path, before = self._write_params({"who": "小明"})
        report.read_params_file(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)


# -- merge_param_sources 函数级合并 ----------------------------------------

class MergeParamsTests(unittest.TestCase):

    def test_neither_source_is_none(self):
        self.assertIsNone(report.merge_param_sources(None, None))
        self.assertIsNone(report.merge_param_sources({}, {}))

    def test_file_only(self):
        self.assertEqual(
            report.merge_param_sources({"who": "小明"}, None), {"who": "小明"}
        )

    def test_cli_only(self):
        self.assertEqual(
            report.merge_param_sources(None, {"who": "小红"}), {"who": "小红"}
        )

    def test_cli_overrides_file(self):
        merged = report.merge_param_sources(
            {"who": "小明", "only_file": "f"}, {"who": "小红", "only_cli": "c"}
        )
        self.assertEqual(
            merged, {"who": "小红", "only_file": "f", "only_cli": "c"}
        )

    def test_cli_empty_string_overrides_nonempty_file_value(self):
        merged = report.merge_param_sources({"who": "小明"}, {"who": ""})
        self.assertEqual(merged, {"who": ""})


# -- 命令行端到端 ----------------------------------------------------------

class ParamsFileCliTests(ParamsFileFixture):

    def test_preview_file_param_filters_xiaoming_with_empty_note(self):
        path, _ = self._write_params({"who": "小明"})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, preview=1,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        rows = list(csv.reader(io.StringIO(proc.stdout)))
        self.assertEqual(rows, [["姓名", "备注"], ["小明", ""]])

    def test_cli_param_overrides_file_in_preview_with_note_roundtrip(self):
        path, _ = self._write_params({"who": "小明"})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, extra_params=["who=小红"], preview=2,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = list(csv.reader(io.StringIO(proc.stdout)))
        # 含中文、逗号与双引号的备注经 CSV 解析完整还原
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])

    def test_override_order_independent(self):
        path, _ = self._write_params({"who": "小明"})
        sql_file = os.path.join(HERE, "query.sql")
        # --param 出现在 --params-file 之前时同样覆盖
        proc = subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path,
             "--sql-file", sql_file, "--preview", "2",
             "--param", "who=小红", "--params-file", path],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = list(csv.reader(io.StringIO(proc.stdout)))
        self.assertEqual(rows, [["姓名", "备注"], ["小红", NOTE_VALUE]])

    def test_csv_export_file_params_equals_direct_param_bytes(self):
        path, _ = self._write_params({"who": "小明"})
        out_file = os.path.join(self.tmpdir, "via_file.csv")
        out_cli = os.path.join(self.tmpdir, "via_cli.csv")
        p1 = self._run_cli(["--sql-file", os.path.join(HERE, "query.sql")],
                           params_file=path, output=out_file)
        p2 = self._run_cli(["--sql-file", os.path.join(HERE, "query.sql")],
                           extra_params=["who=小明"], output=out_cli)
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        data1, rows1 = self._read_csv_bytes(out_file)
        data2, rows2 = self._read_csv_bytes(out_cli)
        self.assertEqual(data1, data2)
        self.assertEqual(rows1, rows2)
        self.assertEqual(rows1, [["姓名", "备注"], ["小明", ""]])

    def test_html_export_shows_escaped_note(self):
        path, _ = self._write_params({"who": "小红"})
        out = os.path.join(self.tmpdir, "r.html")
        proc = self._run_cli(["--sql-file", os.path.join(HERE, "query.sql")],
                             params_file=path, output=out, fmt="html")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as f:
            page = f.read()
        # 双引号按既有规则转义，原文不以属性破坏形式出现；中文与逗号保留
        self.assertIn("<td>小红</td>", page)
        self.assertIn("中文,含&quot;引号&quot;", page)
        self.assertNotIn(NOTE_VALUE, page)

    def test_cli_empty_string_overrides_file_value(self):
        path, _ = self._write_params({"who": "小明"})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, extra_params=["who="], preview=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = list(csv.reader(io.StringIO(proc.stdout)))
        self.assertEqual(rows, [["姓名", "备注"]])  # 只保留表头，0 数据行

    def test_empty_object_with_missing_referenced_param_fails(self):
        path, _ = self._write_params({})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, preview=1,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("错误", proc.stderr)
        self.assertIn("who", proc.stderr)

    def test_valid_unreferenced_file_param_ignored(self):
        path, _ = self._write_params({"unused": "x"})
        proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                             params_file=path, preview=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            list(csv.reader(io.StringIO(proc.stdout))), [["x"], ["1"]]
        )

    def test_positional_placeholder_still_rejected_with_file_params(self):
        path, _ = self._write_params({"who": "x"})
        proc = self._run_cli(["--sql", "SELECT ?"],
                             params_file=path, preview=1)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("位置占位符", proc.stderr)

    def test_params_file_unchanged_after_success(self):
        path, before = self._write_params({"who": "小明"})
        proc = self._run_cli(["--sql-file", os.path.join(HERE, "query.sql")],
                             params_file=path, preview=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)


# -- 命令行错误约定 --------------------------------------------------------

class ParamsFileCliErrorTests(ParamsFileFixture):

    def _assert_error(self, proc, *reason_parts, output=None):
        """统一核对：退出码 1、stdout 空、stderr 以“错误: ”开头并含原因。"""
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(
            proc.stderr.lstrip().startswith("错误: ")
            or "错误: " in proc.stderr,
            proc.stderr,
        )
        for part in reason_parts:
            self.assertIn(part, proc.stderr)
        if output is not None:
            self.assertFalse(os.path.exists(output))

    def test_missing_file(self):
        proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                             params_file=os.path.join(self.tmpdir, "nope.json"),
                             preview=1)
        self._assert_error(proc, "参数文件不存在")

    def test_directory_path(self):
        proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                             params_file=self.tmpdir, preview=1)
        self._assert_error(proc, "目录")

    def test_bad_encoding_and_json_errors(self):
        cases = [
            (b"\xff\xfe bad", ["UTF-8"]),
            (b"not json", ["合法的 JSON"]),
            (b"[]", ["JSON 对象"]),
            (b'{"who":1}', ["必须是字符串"]),
            (b'{"who":"a","who":"b"}', ["重复键"]),
            (b'{"1who":"x"}', ["非法参数名"]),
            (b'{"who":"x"} trailing', ["多余内容"]),
        ]
        for i, (payload, reasons) in enumerate(cases):
            with self.subTest(payload=payload):
                path = os.path.join(self.tmpdir, "bad_%d.json" % i)
                with open(path, "wb") as f:
                    f.write(payload)
                out = os.path.join(self.tmpdir, "out_%d.csv" % i)
                proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                                     params_file=path, output=out)
                self._assert_error(proc, *reasons, output=out)

    def test_empty_path_rejected_without_reading(self):
        proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                             params_file="", preview=1)
        self._assert_error(proc, "参数错误", "路径不能为空")

    def test_duplicate_option_rejected_without_reading(self):
        good, _ = self._write_params({"a": "1"}, name="g1.json")
        good2, _ = self._write_params({"a": "2"}, name="g2.json")
        proc = self._run_cli(["--sql", "SELECT 1 AS x"],
                             params_file=[good, good2], preview=1)
        self._assert_error(proc, "参数错误", "只能提供一次")

    def test_mixed_with_tables_rejected_before_file_read(self):
        # 文件不存在也必须先报混用，证明该路径不读取参数文件
        proc = self._run_cli([], tables=True,
                             params_file=os.path.join(self.tmpdir, "nope.json"))
        self._assert_error(proc, "参数错误", "--tables")

    def test_mixed_with_describe_rejected_before_file_read(self):
        proc = self._run_cli([], describe="notes",
                             params_file=os.path.join(self.tmpdir, "nope.json"))
        self._assert_error(proc, "参数错误", "--describe")

    def test_invalid_file_entry_cannot_be_bypassed_by_cli_override(self):
        path, _ = self._write_params('{"who":1}', raw=True)
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, extra_params=["who=小红"], preview=1,
        )
        self._assert_error(proc, "必须是字符串")

    def test_duplicate_key_cannot_be_bypassed_by_cli_override(self):
        path, _ = self._write_params('{"who":"a","who":"b"}', raw=True)
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, extra_params=["who=小红"], preview=1,
        )
        self._assert_error(proc, "重复键")

    def test_duplicate_cli_param_still_rejected(self):
        path, _ = self._write_params({"who": "小明"})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path,
            extra_params=["who=小红", "who=小明"], preview=1,
        )
        self._assert_error(proc, "参数错误", "重复提供同名参数")

    def test_params_validated_before_sql_file_db_and_output(self):
        # 参数文件非法时：缺失的查询文件、缺失的源库、已存在的输出都不报错
        path, _ = self._write_params('{"who":1}', raw=True)
        out = os.path.join(self.tmpdir, "exists.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("原内容,不得改动\n")
        proc = self._run_cli(
            ["--sql-file", os.path.join(self.tmpdir, "missing.sql")],
            params_file=path, output=out,
        )
        self._assert_error(proc, "必须是字符串")
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "原内容,不得改动\n")

    def test_failure_creates_no_output_file(self):
        path, _ = self._write_params({})
        out = os.path.join(self.tmpdir, "never.csv")
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, output=out,
        )
        self._assert_error(proc, "缺少查询引用的参数", "who", output=out)


# -- 不提供新选项时的兼容性 ------------------------------------------------

class NoNewOptionCompatTests(ParamsFileFixture):

    def test_preview_without_params_file_unchanged(self):
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            extra_params=["who=小明"], preview=1,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        rows = list(csv.reader(io.StringIO(proc.stdout)))
        self.assertEqual(rows, [["姓名", "备注"], ["小明", ""]])

    def test_export_success_message_unchanged(self):
        out = os.path.join(self.tmpdir, "old.csv")
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            extra_params=["who=小红"], output=out,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         "已导出 1 行数据：%s" % out)

    def test_existing_output_still_refused(self):
        out = os.path.join(self.tmpdir, "exists.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("原内容\n")
        path, _ = self._write_params({"who": "小红"})
        proc = self._run_cli(
            ["--sql-file", os.path.join(HERE, "query.sql")],
            params_file=path, output=out,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("拒绝覆盖", proc.stderr)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "原内容\n")

    def test_query_file_unchanged_after_file_params_run(self):
        sql_file = os.path.join(self.tmpdir, "query_copy.sql")
        with open(sql_file, "wb") as f:
            payload = (
                "SELECT p.name AS 姓名, n.note AS 备注 "
                "FROM people p JOIN notes n ON p.id=n.person_id "
                "WHERE p.name = :who ORDER BY p.id;\n"
            ).encode("utf-8")
            f.write(payload)
        path, _ = self._write_params({"who": "小红"})
        proc = self._run_cli(["--sql-file", sql_file],
                             params_file=path, preview=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(sql_file, "rb") as f:
            self.assertEqual(f.read(), payload)


if __name__ == "__main__":
    unittest.main()
