#!/usr/bin/env python3
"""--csv-bom 开关的独立回归测试。

覆盖 export_csv 函数入口与命令行 CSV 导出入口在省略开关、显式
csv_bom=False 与 csv_bom=True 三种形态下的字节关系、CSV 逻辑内容、
零行结果、列名/数据本身含 U+FEFF 时的原字符保留，以及开关的拒绝
边界（非布尔值、与 HTML/预览/--tables/--describe 混用、目标已存在）。

每个用例在临时目录准备 people 与 notes 两张合成表，按主键关联并排序：
小明的备注为 NULL（用 --null-text 指定的“未填写”标记导出），小红的
中文备注同时含逗号、双引号与字段内换行；预期表头为 姓名、备注，数据
行数为二。所有预期记录均由本文件的固定样例显式给出，不只比较两份
产品输出。用例结束后重新以只读连接核对源库结构及两表数据未变，并由
临时目录清理全部临时材料。

只依赖 Python 标准库。在项目根目录执行：
    python -m unittest test_csv_bom
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

# UTF-8 BOM 的字符形态与字节形态
BOM_CHAR = "\ufeff"
BOM_BYTES = b"\xef\xbb\xbf"

# NULL 备注的导出标记
NULL_MARKER = "未填写"
# 小红的中文备注：同时含逗号、双引号与字段内换行
NOTE_XIAOHONG = '中文,含"双引号"\n换行仍在同一字段'

# 按主键关联并排序的两列表查询
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id ORDER BY p.id"
)
# 零行查询：同样的列，同样的排序
ZERO_ROW_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.id < 0 ORDER BY p.id"
)

# 普通样例解码后经 CSV 解析必须得到的完整逻辑记录（固定预期）
EXPECTED_RECORDS = [
    ["姓名", "备注"],
    ["小明", NULL_MARKER],
    ["小红", NOTE_XIAOHONG],
]
EXPECTED_HEADER_ONLY = [["姓名", "备注"]]

# 首列名与一个非空数据值本身含 U+FEFF 的查询：列名 "a\ufeffb" 内含 BOM 字符，
# 数据值由小红的名字拼接 \ufeff 得到；第二列是不含 BOM 的普通中文值。
# 开关新增的仅是文件起始三个字节，内容中的 \ufeff 必须原样保留。
FEFF_SQL = (
    "SELECT p.name || '\ufeff' AS \"a\ufeffb\", '普通值' AS 次列 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.id = 2 ORDER BY p.id"
)
EXPECTED_FEFF_RECORDS = [
    ["a\ufeffb", "次列"],
    ["小红\ufeff", "普通值"],
]


class _SampleDbTestCase(unittest.TestCase):
    """在临时目录准备 people/notes 合成库，tearDown 只读核对未变并清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中核对源库未被改写
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新以只读连接打开源库，
        # 核对两表结构及全部数据与准备完成时完全一致
        self.assertEqual(self._snapshot_db(), self._baseline)
        # 临时目录连带清理全部导出文件与合成库
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
                [
                    (1, None),            # 小明：NULL 备注
                    (2, NOTE_XIAOHONG),   # 小红：逗号、双引号、字段内换行
                ],
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

    def _path(self, name):
        return os.path.join(self.tmpdir, name)

    @staticmethod
    def _read_bytes(path):
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _parse(text):
        """按 CSV 逻辑记录解析：字段内换行属于字段，不产生额外记录。"""
        return list(csv.reader(io.StringIO(text)))

    def _export(self, path, csv_bom=..., null_text=NULL_MARKER, sql=JOIN_SQL):
        """调用 export_csv；csv_bom 参数省略时不传该实参。"""
        kwargs = {"null_text": null_text}
        if csv_bom is not ...:
            kwargs["csv_bom"] = csv_bom
        return report.export_csv(self.db_path, sql, path, **kwargs)

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


class ExportCsvBomBytesTest(_SampleDbTestCase):
    """函数入口：三种开关形态的字节关系与返回行数。"""

    def test_omit_false_and_true_have_fixed_byte_relationship(self):
        off_default = self._path("off_default.csv")
        off_explicit = self._path("off_explicit.csv")
        on_path = self._path("on.csv")

        # 省略开关、显式 False、显式 True 均返回数据行数 2
        count_default = self._export(off_default)
        count_false = self._export(off_explicit, csv_bom=False)
        count_true = self._export(on_path, csv_bom=True)
        self.assertEqual((count_default, count_false, count_true), (2, 2, 2))

        off_default_bytes = self._read_bytes(off_default)
        off_explicit_bytes = self._read_bytes(off_explicit)
        on_bytes = self._read_bytes(on_path)

        # 关闭形态不以 BOM 开头
        self.assertFalse(off_default_bytes.startswith(BOM_BYTES))
        # 省略开关与显式 False 逐字节一致
        self.assertEqual(off_default_bytes, off_explicit_bytes)
        # 开启文件恰为 EF BB BF 三字节加上关闭文件的全部原字节
        self.assertTrue(on_bytes.startswith(BOM_BYTES))
        self.assertEqual(on_bytes, BOM_BYTES + off_default_bytes)
        self.assertEqual(len(on_bytes), len(off_default_bytes) + 3)
        # 全文件仅有起始这一个 BOM 字节序列（样例内容不含 U+FEFF）
        self.assertEqual(on_bytes.count(BOM_BYTES), 1)

    def test_decoded_files_parse_to_fixed_records(self):
        off_path = self._path("off.csv")
        on_path = self._path("on.csv")
        self.assertEqual(self._export(off_path), 2)
        self.assertEqual(self._export(on_path, csv_bom=True), 2)

        # 关闭文件按 UTF-8 解码；开启文件按 UTF-8-sig 解码后逻辑内容一致
        off_records = self._parse(self._read_bytes(off_path).decode("utf-8"))
        on_records = self._parse(
            self._read_bytes(on_path).decode("utf-8-sig")
        )

        # 预期来自固定样例，而非两份产品输出互相对照
        self.assertEqual(off_records, EXPECTED_RECORDS)
        self.assertEqual(on_records, EXPECTED_RECORDS)
        # 列顺序固定为 姓名、备注；恰两条数据记录
        self.assertEqual(off_records[0], ["姓名", "备注"])
        self.assertEqual(len(off_records), 3)
        # NULL 使用指定的“未填写”标记
        self.assertEqual(off_records[1], ["小明", NULL_MARKER])
        # 小红的备注完整还原：逗号、双引号与字段内换行同属一个字段
        self.assertEqual(off_records[2], ["小红", NOTE_XIAOHONG])
        self.assertIn("\n", off_records[2][1])
        self.assertIn(",", off_records[2][1])
        self.assertIn('"', off_records[2][1])

    def test_zero_rows_keeps_header_and_bom_is_prefix_only(self):
        off_path = self._path("zero_off.csv")
        on_path = self._path("zero_on.csv")
        self.assertEqual(self._export(off_path, sql=ZERO_ROW_SQL), 0)
        self.assertEqual(
            self._export(on_path, csv_bom=True, sql=ZERO_ROW_SQL), 0
        )

        off_bytes = self._read_bytes(off_path)
        on_bytes = self._read_bytes(on_path)
        # 零行仍保留表头；开启时仅多出起始三个前缀字节
        self.assertEqual(on_bytes, BOM_BYTES + off_bytes)
        self.assertEqual(
            self._parse(off_bytes.decode("utf-8")), EXPECTED_HEADER_ONLY
        )
        self.assertEqual(
            self._parse(on_bytes.decode("utf-8-sig")), EXPECTED_HEADER_ONLY
        )

    def test_feff_in_column_name_and_value_is_preserved(self):
        off_path = self._path("feff_off.csv")
        on_path = self._path("feff_on.csv")
        self.assertEqual(self._export(off_path, sql=FEFF_SQL), 1)
        self.assertEqual(self._export(on_path, csv_bom=True, sql=FEFF_SQL), 1)

        off_bytes = self._read_bytes(off_path)
        on_bytes = self._read_bytes(on_path)

        # 关闭文件本身已含两个 U+FEFF（列名与数据值各一）
        self.assertEqual(off_bytes.count(BOM_BYTES), 2)
        # 开启时仅新增文件起始三个字节：只移除这三个字节做比较，
        # 绝不剥掉内容中既有的 U+FEFF 字符
        self.assertEqual(on_bytes[:3], BOM_BYTES)
        self.assertEqual(on_bytes[3:], off_bytes)
        self.assertEqual(on_bytes.count(BOM_BYTES), 3)

        # 固定样例显式给出预期：列名与非空数据值中的 \ufeff 原样保留
        records = self._parse(on_bytes[3:].decode("utf-8"))
        self.assertEqual(records, EXPECTED_FEFF_RECORDS)
        self.assertIn(BOM_CHAR, records[0][0])
        self.assertIn(BOM_CHAR, records[1][0])
        self.assertNotIn(BOM_CHAR, records[1][1])
        # 以 utf-8-sig 整文件解码同样只去掉起始 BOM，内容字符不受影响
        self.assertEqual(
            self._parse(on_bytes.decode("utf-8-sig")), EXPECTED_FEFF_RECORDS
        )
        self.assertEqual(
            self._parse(off_bytes.decode("utf-8")), EXPECTED_FEFF_RECORDS
        )

    def test_non_bool_csv_bom_raises_and_creates_nothing(self):
        for bad in (1, "true"):
            with self.subTest(csv_bom=bad):
                target = self._path("reject_%r.csv" % bad)
                with self.assertRaises(ValueError) as ctx:
                    report.export_csv(
                        self.db_path, JOIN_SQL, target,
                        null_text=NULL_MARKER, csv_bom=bad,
                    )
                self.assertIn("csv_bom 必须是布尔值", str(ctx.exception))
                # 拒绝先于创建目标：目标路径保持未占用
                self.assertFalse(os.path.exists(target))


class CsvBomCommandLineTest(_SampleDbTestCase):
    """命令行导出入口：合法开启、混用拒绝与目标已存在。"""

    def test_legal_cli_export_enables_bom_with_or_without_format(self):
        off_path = self._path("cli_off.csv")
        on_default_path = self._path("cli_on_default.csv")
        on_csv_path = self._path("cli_on_format_csv.csv")

        proc_off = self._run_cli(
            "--sql", JOIN_SQL, "--output", off_path,
            "--null-text", NULL_MARKER,
        )
        proc_on_default = self._run_cli(
            "--sql", JOIN_SQL, "--output", on_default_path,
            "--null-text", NULL_MARKER, "--csv-bom",
        )
        proc_on_csv = self._run_cli(
            "--sql", JOIN_SQL, "--output", on_csv_path,
            "--null-text", NULL_MARKER, "--format", "csv", "--csv-bom",
        )

        for proc in (proc_off, proc_on_default, proc_on_csv):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
        # 保留原有的行数成功提示，行数为 2
        self.assertEqual(
            proc_off.stdout, "已导出 2 行数据：%s\n" % off_path
        )
        self.assertEqual(
            proc_on_default.stdout,
            "已导出 2 行数据：%s\n" % on_default_path,
        )
        self.assertEqual(
            proc_on_csv.stdout,
            "已导出 2 行数据：%s\n" % on_csv_path,
        )

        off_bytes = self._read_bytes(off_path)
        # 省略 --format 与显式 --format csv 均能开启 BOM，且内容前缀关系固定
        self.assertFalse(off_bytes.startswith(BOM_BYTES))
        self.assertEqual(
            self._read_bytes(on_default_path), BOM_BYTES + off_bytes
        )
        self.assertEqual(
            self._read_bytes(on_csv_path), BOM_BYTES + off_bytes
        )
        # 开启文件解码后仍是固定的两条完整记录
        self.assertEqual(
            self._parse(
                self._read_bytes(on_default_path).decode("utf-8-sig")
            ),
            EXPECTED_RECORDS,
        )

    def test_csv_bom_conflicts_reported_as_parameter_errors(self):
        missing_sql = self._path("no_such_query.sql")
        html_output = self._path("must_not_exist.html")
        # (额外命令行片段, 错误原因中必须出现的参数错误说明)
        cases = [
            (
                ["--sql-file", missing_sql, "--format", "html",
                 "--output", html_output, "--csv-bom"],
                "--csv-bom 仅支持 CSV 格式",
            ),
            (
                ["--sql-file", missing_sql, "--preview", "5", "--csv-bom"],
                "--preview 不支持 --csv-bom",
            ),
            (["--tables", "--csv-bom"], "--tables 仅与 --db 搭配"),
            (
                ["--describe", "people", "--csv-bom"],
                "--describe 仅与 --db 搭配",
            ),
        ]
        for extra, reason in cases:
            with self.subTest(extra=extra):
                proc = self._run_cli(*extra)
                self.assertEqual(proc.returncode, 1)
                # 标准输出为空
                self.assertEqual(proc.stdout, "")
                # 标准错误含“错误: ”前缀与参数错误原因
                self.assertIn("错误: 参数错误", proc.stderr)
                self.assertIn(reason, proc.stderr)
                # 查询文件即使不存在，也先报告参数错误而非文件读取错误
                self.assertNotIn("查询文件不存在", proc.stderr)
        # 混用拒绝不创建任何报告文件
        self.assertFalse(os.path.exists(html_output))

    def test_existing_target_is_rejected_and_bytes_unchanged(self):
        original_bytes = b"original \xe5\x8e\x9f\xe5\xad\x97\xe8\x8a\x82\n"
        for with_bom in (False, True):
            with self.subTest(csv_bom=with_bom):
                target = self._path("existing_%s.csv" % with_bom)
                with open(target, "wb") as f:
                    f.write(original_bytes)
                extra = ["--sql", JOIN_SQL, "--output", target,
                         "--null-text", NULL_MARKER]
                if with_bom:
                    extra.append("--csv-bom")
                proc = self._run_cli(*extra)

                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("错误: "))
                self.assertIn("输出目标已存在", proc.stderr)
                # 既存文件原字节不变
                self.assertEqual(self._read_bytes(target), original_bytes)


class SiblingEntriesUnaffectedTest(_SampleDbTestCase):
    """HTML、预览与元数据入口的公开行为在 BOM 特性加入后保持原样。"""

    def test_html_preview_and_metadata_entries_keep_behavior(self):
        # HTML 文件导出：无 BOM 概念，页面以 DOCTYPE 开头并含中文数据
        html_path = self._path("report.html")
        self.assertEqual(
            report.export_html(
                self.db_path, JOIN_SQL, html_path, null_text=NULL_MARKER
            ),
            2,
        )
        html_bytes = self._read_bytes(html_path)
        self.assertTrue(html_bytes.startswith(b"<!DOCTYPE html>"))
        self.assertFalse(html_bytes.startswith(BOM_BYTES))
        self.assertIn("小红".encode("utf-8"), html_bytes)

        # 终端预览：标准输出仍是不带 BOM 的 CSV，固定两条记录
        buf = io.StringIO()
        with redirect_stdout(buf):
            shown = report.preview_csv(
                self.db_path, JOIN_SQL, 10, null_text=NULL_MARKER
            )
        preview_text = buf.getvalue()
        self.assertEqual(shown, 2)
        self.assertFalse(preview_text.startswith(BOM_CHAR))
        self.assertEqual(self._parse(preview_text), EXPECTED_RECORDS)

        # 元数据入口：表名列举与表结构查看仍写固定表头的 CSV
        # （名称按 SQLite BINARY 升序：notes 在 people 之前）
        tables_buf = io.StringIO()
        with redirect_stdout(tables_buf):
            table_count = report.list_tables(self.db_path)
        self.assertEqual(table_count, 2)
        self.assertEqual(
            self._parse(tables_buf.getvalue()),
            [["name"], ["notes"], ["people"]],
        )

        describe_buf = io.StringIO()
        with redirect_stdout(describe_buf):
            column_count = report.describe_table(self.db_path, "notes")
        self.assertEqual(column_count, 2)
        describe_records = self._parse(describe_buf.getvalue())
        self.assertEqual(
            describe_records[0],
            ["cid", "name", "type", "notnull", "dflt_value", "pk"],
        )
        self.assertEqual(
            [row[1] for row in describe_records[1:]],
            ["person_id", "note"],
        )


if __name__ == "__main__":
    unittest.main()
