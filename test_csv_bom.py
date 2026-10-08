#!/usr/bin/env python3
"""--csv-bom（UTF-8 BOM 开关）既有行为的独立回归测试。

report.export_csv 已支持 csv_bom 开关，命令行入口已支持 --csv-bom。
本文件只锁定这一既有行为，不改动产品源码、既有接口与公开文档：

* 函数入口：同一查询导出到三个不同的未占用路径，比较省略开关、显式
  csv_bom=False 与 csv_bom=True 的文件：前两者逐字节一致，开启文件
  恰好等于 EF BB BF 三字节加上关闭文件的全部原字节，返回值均为 2；
* 普通样例解码后用标准库 CSV 解析，得到固定的表头（姓名、备注）与
  两条完整记录：小明的 NULL 备注替换为指定的“未填写”标记，小红的
  中文备注含逗号、双引号与字段内换行，换行属于同一个字段；
* 零行查询仍保留表头，开启时仅多出三个前缀字节，返回 0；
* 列名与非空数据值本身含 U+FEFF 时原字符保留：比较时只移除开启文件
  新增的三个前缀字节，内容中的 U+FEFF 不被剥掉；
* 拒绝边界：函数收到 1 或字符串 "true" 而非布尔值时抛 ValueError 且
  不创建目标；命令行将 --csv-bom 与 HTML、预览、--tables、--describe
  混用（即使查询文件不存在）时退出 1、标准输出为空、标准错误含
  “错误: ”前缀与参数错误原因；
* 合法命令行 CSV 导出保留原有行数成功提示，省略格式与显式
  --format csv 均可开启 BOM；目标已存在时退出 1、原字节不变；
* 现有 HTML、预览与元数据入口的公开行为继续保持。

只依赖 Python 标准库；每个用例在临时目录自行建库（people/notes 两张
合成表，按主键关联并排序），用例结束后重新以只读连接核对源库结构及
两表数据未变，并清理全部临时材料。

在项目根目录执行：
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

import report

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PY = os.path.join(HERE, "report.py")

# UTF-8 BOM：字符 U+FEFF 编码即字节 EF BB BF
BOM_CHAR = "\ufeff"
BOM_BYTES = BOM_CHAR.encode("utf-8")

# NULL 备注的指定导出标记
NULL_MARKER = "未填写"
# 小红的备注：中文、逗号、双引号与字段内换行同时出现
NOTE_RED = '她说:"你好,世界"\n第二行'

# 按主键关联并排序的两表联查：表头固定为 姓名、备注
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id ORDER BY p.id"
)
# 零行查询：列名、列顺序与 JOIN_SQL 完全一致，只是没有数据行
ZERO_ROW_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.id < 0 ORDER BY p.id"
)
# 首列名与一个非空数据值本身含 U+FEFF 的查询：开启 BOM 时文件最前
# 新增的前缀字节不得与内容中的同形字符混淆，内容字符必须原样保留。
# 第二列及第二个数据值刻意不含 U+FEFF，使内容中恰有两个该字符
FEFF_SQL = (
    'SELECT %s AS "%s列", \'普通\' AS 备注'
    % ("'" + BOM_CHAR + "A'", BOM_CHAR)
)

# 普通样例经 CSV 解析必须得到的完整逻辑记录（固定预期，非两份产品输出互比）
EXPECTED_RECORDS = [
    ["姓名", "备注"],
    ["小明", NULL_MARKER],
    ["小红", NOTE_RED],
]
# 含 U+FEFF 样例的完整逻辑记录：首列名与一个数据值中的字符都保留
FEFF_EXPECTED_RECORDS = [
    [BOM_CHAR + "列", "备注"],
    [BOM_CHAR + "A", "普通"],
]


class CsvBomTestCase(unittest.TestCase):
    """--csv-bom 在函数入口与命令行入口上的字节、记录与拒绝边界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中核对源库未被改写
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，重新只读打开源库，
        # 核对两表结构及全部数据与准备完成时完全一致
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
                "CREATE TABLE notes (person_id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                [(1, "小明"), (2, "小红")],
            )
            conn.executemany(
                "INSERT INTO notes (person_id, note) VALUES (?, ?)",
                [(1, None), (2, NOTE_RED)],
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

    # -- 调用助手 -----------------------------------------------------------

    def _path(self, name):
        return os.path.join(self.tmpdir, name)

    @staticmethod
    def _read_bytes(path):
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _parse_csv_text(text):
        """按 CSV 逻辑记录解析：字段内换行属于字段，不产生额外记录。"""
        return list(csv.reader(io.StringIO(text)))

    def _export_bytes(self, target, csv_bom, sql=JOIN_SQL):
        """调用 export_csv，返回 (数据行数, 文件原始字节)。"""
        count = report.export_csv(
            self.db_path,
            sql,
            target,
            null_text=NULL_MARKER,
            csv_bom=csv_bom,
        )
        return count, self._read_bytes(target)

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # -- 函数入口：省略 / False / True 三路逐字节对照 -----------------------

    def test_function_omitted_false_true_byte_comparison(self):
        path_default = self._path("default.csv")
        path_false = self._path("explicit_false.csv")
        path_true = self._path("explicit_true.csv")

        # 省略开关：与既有行为逐字节一致；三个目标互不相同且起初未占用
        count_default = report.export_csv(
            self.db_path,
            JOIN_SQL,
            path_default,
            null_text=NULL_MARKER,
        )
        count_false, bytes_false = self._export_bytes(path_false, False)
        count_true, bytes_true = self._export_bytes(path_true, True)
        bytes_default = self._read_bytes(path_default)

        # 三个入口均返回数据行数 2（表头不计入）
        self.assertEqual((count_default, count_false, count_true), (2, 2, 2))
        # 省略开关与显式 False 逐字节一致
        self.assertEqual(bytes_default, bytes_false)
        # 关闭文件不含 BOM 前缀
        self.assertFalse(bytes_false.startswith(BOM_BYTES))
        # 开启文件恰好等于三个 BOM 字节加上关闭文件的全部原字节
        self.assertEqual(len(bytes_true), len(bytes_false) + 3)
        self.assertEqual(bytes_true[:3], BOM_BYTES)
        self.assertEqual(bytes_true[3:], bytes_false)
        self.assertEqual(bytes_true, BOM_BYTES + bytes_false)

    def test_plain_samples_parse_to_fixed_records(self):
        _, bytes_false = self._export_bytes(self._path("off.csv"), False)
        _, bytes_true = self._export_bytes(self._path("on.csv"), True)

        # 关闭文件按普通 UTF-8 解码；开启文件按 utf-8-sig 解码后同样可解析
        records_off = self._parse_csv_text(bytes_false.decode("utf-8"))
        records_on = self._parse_csv_text(bytes_true.decode("utf-8-sig"))

        # 固定预期：列顺序为 姓名、备注，恰两条数据记录
        for records in (records_off, records_on):
            with self.subTest(records=records):
                self.assertEqual(records, EXPECTED_RECORDS)
                self.assertEqual(records[0], ["姓名", "备注"])
                self.assertEqual(len(records), 3)
                # 小明的 NULL 备注使用指定的未填写标记
                self.assertEqual(records[1], ["小明", NULL_MARKER])
                # 小红的中文备注完整还原：逗号、双引号与字段内换行
                self.assertEqual(records[2], ["小红", NOTE_RED])
                self.assertIn("\n", records[2][1])
                self.assertIn(",", records[2][1])
                self.assertIn('"', records[2][1])

        # 字段内换行使物理行多于逻辑记录，但逻辑记录仍恰为表头 + 2 行
        self.assertGreater(
            len(bytes_false.decode("utf-8").splitlines()), len(records_off)
        )

    # -- 零行查询：只保留表头，开启仅多出三个前缀字节 -----------------------

    def test_zero_rows_keeps_header_with_bom_prefix_only(self):
        count_false, bytes_false = self._export_bytes(
            self._path("zero_off.csv"), False, sql=ZERO_ROW_SQL
        )
        count_true, bytes_true = self._export_bytes(
            self._path("zero_on.csv"), True, sql=ZERO_ROW_SQL
        )

        self.assertEqual(count_false, 0)
        self.assertEqual(count_true, 0)
        # 零行仍保留表头
        self.assertEqual(
            self._parse_csv_text(bytes_false.decode("utf-8")),
            [["姓名", "备注"]],
        )
        self.assertEqual(
            self._parse_csv_text(bytes_true.decode("utf-8-sig")),
            [["姓名", "备注"]],
        )
        # 开启文件仅多出三个前缀字节，其余（表头）字节逐字节一致
        self.assertEqual(bytes_true, BOM_BYTES + bytes_false)
        self.assertEqual(bytes_true[3:], bytes_false)

    # -- 内容本身含 U+FEFF：只移除新增的三个字节，不剥内容字符 --------------

    def test_content_feff_characters_are_preserved(self):
        count_false, bytes_false = self._export_bytes(
            self._path("feff_off.csv"), False, sql=FEFF_SQL
        )
        count_true, bytes_true = self._export_bytes(
            self._path("feff_on.csv"), True, sql=FEFF_SQL
        )
        self.assertEqual((count_false, count_true), (1, 1))

        # 关闭文件的列名即以 U+FEFF 开头，因此其字节恰好也以 EF BB BF
        # 三个字节打头：区分两个文件只能靠“开启文件多出且仅多出三个字节”，
        # 而不能靠“是否以该三字节打头”或简单的剥前缀启发式
        self.assertTrue(bytes_false.startswith(BOM_BYTES))
        self.assertEqual(bytes_true, BOM_BYTES + bytes_false)
        # 内容中两处 U+FEFF（列名一处、数据一处）；开启文件额外多出一处
        self.assertEqual(bytes_false.count(BOM_BYTES), 2)
        self.assertEqual(bytes_true.count(BOM_BYTES), 3)

        # 比较时只移除开启文件新增的三个字节：手动切片，绝不使用会吞掉
        # 内容字符的“剥掉所有 BOM”做法
        self.assertEqual(bytes_true[:3], BOM_BYTES)
        stripped_text = bytes_true[3:].decode("utf-8")
        self.assertEqual(bytes_true[3:], bytes_false)

        # 两份内容按固定样例解析：列名与数据里的 U+FEFF 原样保留
        records_off = self._parse_csv_text(bytes_false.decode("utf-8"))
        records_on = self._parse_csv_text(stripped_text)
        self.assertEqual(records_off, FEFF_EXPECTED_RECORDS)
        self.assertEqual(records_on, FEFF_EXPECTED_RECORDS)
        # 剥除新增前缀后，内容中的两个 U+FEFF 仍在
        self.assertEqual(stripped_text.count(BOM_CHAR), 2)
        self.assertEqual(records_on[0][0], BOM_CHAR + "列")
        self.assertEqual(records_on[1][0], BOM_CHAR + "A")
        # 第二个数据值不含 U+FEFF，与首个含字符的值形成对照
        self.assertEqual(records_on[1][1], "普通")
        self.assertNotIn(BOM_CHAR, records_on[1][1])

    # -- 函数入口的拒绝边界：非布尔值不创建任何目标 -------------------------

    def test_function_rejects_non_bool_csv_bom_without_creating_target(self):
        for bad in (1, "true"):
            with self.subTest(bad=bad):
                target = self._path("reject_%r.csv" % bad)
                self.assertFalse(os.path.exists(target))
                with self.assertRaises(ValueError) as ctx:
                    report.export_csv(
                        self.db_path,
                        JOIN_SQL,
                        target,
                        null_text=NULL_MARKER,
                        csv_bom=bad,
                    )
                self.assertIn("csv_bom 必须是布尔值", str(ctx.exception))
                # 拒绝时不创建目标
                self.assertFalse(os.path.exists(target))

        # 开关类型校验先于输出目标预查：即便输出目录不存在，报告的仍是
        # 类型错误，且不会因为校验顺序变化而换成目录错误
        missing_dir_target = self._path(os.path.join("no_such_dir", "x.csv"))
        with self.assertRaises(ValueError) as ctx:
            report.export_csv(
                self.db_path, JOIN_SQL, missing_dir_target, csv_bom=1
            )
        self.assertIn("csv_bom 必须是布尔值", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.dirname(missing_dir_target)))

    # -- 命令行：与 HTML/预览/元数据模式混用一律按参数错误拒绝 --------------

    def test_cli_rejects_csv_bom_mixed_with_other_modes(self):
        html_out = self._path("must_not_exist.html")
        # (附加参数, 错误原因中必须出现的参数错误说明)
        cases = [
            (
                ["--format", "html", "--sql", JOIN_SQL, "--output", html_out],
                "--csv-bom 仅支持 CSV 格式",
            ),
            (
                ["--preview", "5", "--sql", JOIN_SQL],
                "--preview 不支持 --csv-bom",
            ),
            (
                ["--tables"],
                "--tables 仅与 --db 搭配",
            ),
            (
                ["--describe", "people"],
                "--describe 仅与 --db 搭配",
            ),
        ]
        for fragment, reason in cases:
            with self.subTest(reason=reason):
                proc = self._run_cli("--csv-bom", *fragment)
                self.assertEqual(proc.returncode, 1, fragment)
                self.assertEqual(proc.stdout, "")
                # 标准错误含“错误: ”前缀与具体的参数错误原因
                self.assertIn("错误: ", proc.stderr)
                self.assertIn("参数错误", proc.stderr)
                self.assertIn(reason, proc.stderr)
        # 所有混用路径都不生成报告
        self.assertFalse(os.path.exists(html_out))

    def test_cli_reports_argument_error_before_missing_sql_file(self):
        # 查询文件刻意不存在：参数错误必须先于文件读取报出
        missing_sql = self._path("no_such_query.sql")
        missing_out = self._path("must_not_exist_either.csv")
        self.assertFalse(os.path.exists(missing_sql))

        # HTML + --csv-bom：先报格式参数错误，不读取查询文件
        proc_html = self._run_cli(
            "--sql-file", missing_sql,
            "--format", "html", "--output", missing_out, "--csv-bom",
        )
        self.assertEqual(proc_html.returncode, 1)
        self.assertEqual(proc_html.stdout, "")
        self.assertIn("错误: ", proc_html.stderr)
        self.assertIn("--csv-bom 仅支持 CSV 格式", proc_html.stderr)
        self.assertNotIn("查询文件", proc_html.stderr)

        # 预览 + --csv-bom：同样先报参数错误
        proc_preview = self._run_cli(
            "--sql-file", missing_sql, "--preview", "3", "--csv-bom",
        )
        self.assertEqual(proc_preview.returncode, 1)
        self.assertEqual(proc_preview.stdout, "")
        self.assertIn("--preview 不支持 --csv-bom", proc_preview.stderr)
        self.assertNotIn("查询文件", proc_preview.stderr)

        # 不存在的查询文件依旧不存在，输出目标未被创建
        self.assertFalse(os.path.exists(missing_sql))
        self.assertFalse(os.path.exists(missing_out))

    # -- 命令行：合法 CSV 导出保留成功提示，省略/显式格式均开启 BOM ---------

    def test_cli_legal_csv_export_enables_bom_with_row_count_message(self):
        out_off = self._path("cli_off.csv")
        out_default_fmt = self._path("cli_bom_default_format.csv")
        out_explicit_csv = self._path("cli_bom_explicit_csv.csv")

        proc_off = self._run_cli(
            "--sql", JOIN_SQL, "--output", out_off,
            "--null-text", NULL_MARKER,
        )
        # 省略格式 + --csv-bom
        proc_default_fmt = self._run_cli(
            "--sql", JOIN_SQL, "--output", out_default_fmt,
            "--null-text", NULL_MARKER, "--csv-bom",
        )
        # 显式 --format csv + --csv-bom
        proc_explicit_csv = self._run_cli(
            "--sql", JOIN_SQL, "--format", "csv",
            "--output", out_explicit_csv,
            "--null-text", NULL_MARKER, "--csv-bom",
        )

        for proc, out in (
            (proc_default_fmt, out_default_fmt),
            (proc_explicit_csv, out_explicit_csv),
        ):
            with self.subTest(out=out):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                # 保留原有的行数成功提示（数据行数 2 与目标路径）
                self.assertEqual(
                    proc.stdout, "已导出 2 行数据：%s\n" % out
                )

        bytes_off = self._read_bytes(out_off)
        bytes_default_fmt = self._read_bytes(out_default_fmt)
        bytes_explicit_csv = self._read_bytes(out_explicit_csv)

        # 两种合法形态都恰好多出三个 BOM 前缀字节，其后逐字节一致
        self.assertEqual(bytes_default_fmt, BOM_BYTES + bytes_off)
        self.assertEqual(bytes_explicit_csv, BOM_BYTES + bytes_off)
        # 两份开启文件彼此逐字节一致
        self.assertEqual(bytes_default_fmt, bytes_explicit_csv)
        # 解码后都是固定的完整记录
        self.assertEqual(
            self._parse_csv_text(bytes_default_fmt.decode("utf-8-sig")),
            EXPECTED_RECORDS,
        )
        self.assertEqual(
            self._parse_csv_text(bytes_explicit_csv.decode("utf-8-sig")),
            EXPECTED_RECORDS,
        )

    def test_cli_existing_target_rejected_with_bytes_unchanged(self):
        out = self._path("existing.csv")
        original_bytes = b"keep,these,original,bytes\n"
        with open(out, "wb") as f:
            f.write(original_bytes)

        proc = self._run_cli(
            "--sql", JOIN_SQL, "--output", out,
            "--null-text", NULL_MARKER, "--csv-bom",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("错误: ", proc.stderr)
        self.assertIn("已存在", proc.stderr)
        # 既有目标原字节不变，没有被 BOM 或新内容截断覆盖
        self.assertEqual(self._read_bytes(out), original_bytes)

    # -- 既有 HTML、预览与元数据入口的公开行为继续保持 ----------------------

    def test_other_entry_points_keep_working(self):
        # --tables：单列固定表头，people/notes 按 BINARY 顺序列出
        proc_tables = self._run_cli("--tables")
        self.assertEqual(proc_tables.returncode, 0, proc_tables.stderr)
        self.assertEqual(proc_tables.stderr, "")
        self.assertEqual(proc_tables.stdout, "name\nnotes\npeople\n")

        # --describe people：既有固定表头与两列结构
        proc_describe = self._run_cli("--describe", "people")
        self.assertEqual(proc_describe.returncode, 0, proc_describe.stderr)
        self.assertEqual(proc_describe.stderr, "")
        describe_records = self._parse_csv_text(proc_describe.stdout)
        self.assertEqual(
            describe_records[0],
            ["cid", "name", "type", "notnull", "dflt_value", "pk"],
        )
        self.assertEqual(
            describe_records[1:],
            [
                ["0", "id", "INTEGER", "0", "", "1"],
                ["1", "name", "TEXT", "1", "", "0"],
            ],
        )

        # --preview：标准输出只含 CSV，不带 BOM、不追加成功提示
        proc_preview = self._run_cli("--sql", JOIN_SQL, "--preview", "1")
        self.assertEqual(proc_preview.returncode, 0, proc_preview.stderr)
        self.assertEqual(proc_preview.stderr, "")
        self.assertNotIn("已导出", proc_preview.stdout)
        # 默认空标记：小明的 NULL 备注为空字段
        self.assertEqual(
            self._parse_csv_text(proc_preview.stdout),
            [["姓名", "备注"], ["小明", ""]],
        )

        # HTML 导出正常工作，且文件不以 BOM 打头
        html_out = self._path("report.html")
        proc_html = self._run_cli(
            "--sql", JOIN_SQL, "--format", "html", "--output", html_out
        )
        self.assertEqual(proc_html.returncode, 0, proc_html.stderr)
        self.assertEqual(proc_html.stderr, "")
        html_bytes = self._read_bytes(html_out)
        self.assertFalse(html_bytes.startswith(BOM_BYTES))
        html_text = html_bytes.decode("utf-8")
        self.assertIn("<title>查询报告</title>", html_text)
        self.assertIn("<th>姓名</th>", html_text)
        self.assertIn("<th>备注</th>", html_text)
        self.assertIn("小明", html_text)
        self.assertIn("她说:", html_text)


if __name__ == "__main__":
    unittest.main()
