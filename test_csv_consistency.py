#!/usr/bin/env python3
"""export_csv 与 preview_csv 的 CSV 表达一致性回归测试。

重构后两条入口共用同一份 CSV 结果表达逻辑（report._write_csv_records）。
本文件用 people/notes 两张小型合成表的固定排序联表查询，核对文件导出
与终端预览对同一查询产生相同的列名、列顺序、行顺序与字段内容：
小明的 NULL 备注、小红的含引号中文备注、空字符串、零值、与标记同形的
普通文本、首尾空格与字段内换行。比较一律按 CSV 逻辑记录（csv.reader）
进行，字段内换行不会被当成额外数据行。

只依赖 Python 标准库；每个用例在临时目录自行建库，用例结束后重新只读
打开源库核对表结构与全部数据未变，并清理全部临时文件。

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

# 固定排序的联表查询：列名、列顺序与行顺序在两条入口间必须一致
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注, n.flag AS 标记 "
    "FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id"
)

NULL_MARKER = "未填写"
# 小红的备注：同时包含中文、逗号与双引号
NOTE_QUOTED = '中文,含"引号"'
# 小丽的备注：首尾空格与字段内换行
NOTE_MULTILINE = " 首行\n次行 "
# 小强的备注：与 NULL 标记同形的普通文本，不应被额外转换
NOTE_SAME_AS_MARKER = NULL_MARKER

# 使用 NULL_MARKER 时两条入口都应得到的完整逻辑记录：
# 小明 NULL->标记；小华空字符串原样；零值 "0" 原样；小强同形文本原样
EXPECTED_RECORDS = [
    ["姓名", "备注", "标记"],
    ["小明", NULL_MARKER, "0"],
    ["小红", NOTE_QUOTED, "1"],
    ["小华", "", "0"],
    ["小丽", NOTE_MULTILINE, "2"],
    ["小强", NOTE_SAME_AS_MARKER, "3"],
]


class CsvConsistencyTestCase(unittest.TestCase):
    """同一查询下 export_csv 文件与 preview_csv 标准输出的对照。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self.out_path = os.path.join(self.tmpdir, "out.csv")
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
                "CREATE TABLE notes ("
                "person_id INTEGER PRIMARY KEY, note TEXT, flag INTEGER)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                [(1, "小明"), (2, "小红"), (3, "小华"), (4, "小丽"), (5, "小强")],
            )
            conn.executemany(
                "INSERT INTO notes (person_id, note, flag) VALUES (?, ?, ?)",
                [
                    (1, None, 0),               # NULL 备注，零值标记
                    (2, NOTE_QUOTED, 1),        # 含逗号、双引号的中文
                    (3, "", 0),                 # 空字符串
                    (4, NOTE_MULTILINE, 2),     # 首尾空格与字段内换行
                    (5, NOTE_SAME_AS_MARKER, 3),  # 与标记同形的普通文本
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
                "SELECT person_id, note, flag FROM notes ORDER BY person_id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "people": people, "notes": notes}

    @staticmethod
    def _parse_csv(text):
        """按 CSV 逻辑记录解析：字段内换行属于字段，不产生额外记录。"""
        return list(csv.reader(io.StringIO(text)))

    def _run_export(self, null_text=NULL_MARKER, sql=JOIN_SQL):
        """调用 export_csv，返回 (数据行数, 文件原文, 逻辑记录)。"""
        count = report.export_csv(
            self.db_path, sql, self.out_path, null_text=null_text
        )
        with open(self.out_path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return count, text, self._parse_csv(text)

    def _run_preview(self, limit, null_text=NULL_MARKER, sql=JOIN_SQL):
        """调用 preview_csv 并捕获标准输出，返回 (展示行数, 原文, 逻辑记录)。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            shown = report.preview_csv(
                self.db_path, sql, limit, null_text=null_text
            )
        text = buf.getvalue()
        return shown, text, self._parse_csv(text)

    def _run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, REPORT_PY, "--db", self.db_path] + list(extra),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # -- 函数入口：全量导出与全量预览逐字节一致 ------------------------------

    def test_export_returns_all_rows_with_expected_records(self):
        count, text, records = self._run_export()

        # 导出返回全部数据行数（表头不计入）
        self.assertEqual(count, 5)
        self.assertEqual(records, EXPECTED_RECORDS)
        # 字段内换行使物理行数多于逻辑记录数，但逻辑记录恰为表头 + 5 行
        self.assertGreater(len(text.splitlines()), 6)
        self.assertEqual(len(records), 6)

    def test_full_preview_matches_export_byte_for_byte(self):
        count, export_text, export_records = self._run_export()
        # 预览上限超过结果数量：展示全部，仅限制展示不改变表达规则
        shown, preview_text, preview_records = self._run_preview(10)

        self.assertEqual(shown, 5)
        self.assertEqual(shown, count)
        # 同一查询下预览原文与导出文件逐字节一致
        self.assertEqual(preview_text, export_text)
        self.assertEqual(preview_records, EXPECTED_RECORDS)
        self.assertEqual(export_records, EXPECTED_RECORDS)

    # -- 函数入口：预览一行是全量导出的逻辑前缀 ------------------------------

    def test_preview_one_row_is_logical_prefix_of_export(self):
        _, _, export_records = self._run_export()
        shown, _, preview_records = self._run_preview(1)

        # 预览返回实际展示的数据行数（表头不计入）
        self.assertEqual(shown, 1)
        # 表头 + 小明一行：NULL 备注替换为标记，与全量导出前两条记录一致
        self.assertEqual(preview_records, EXPECTED_RECORDS[:2])
        self.assertEqual(preview_records, export_records[:2])
        self.assertEqual(preview_records[1], ["小明", NULL_MARKER, "0"])

    # -- NULL 标记只替换 NULL ------------------------------------------------

    def test_null_marker_leaves_empty_zero_and_marker_text_untouched(self):
        _, _, records = self._run_export()
        _, _, preview_records = self._run_preview(10)
        for actual in (records, preview_records):
            with self.subTest(records=actual):
                self.assertEqual(actual, EXPECTED_RECORDS)
                # 空字符串仍是空字段，不变成标记
                self.assertEqual(actual[3][1], "")
                # 零值原样为 "0"，不变成标记
                self.assertEqual(actual[1][2], "0")
                # 与标记同形的普通文本原样保留（小强），与 NULL 替换结果
                # （小明）同形：标记只替换 NULL，不做额外转换也不加额外标记
                self.assertEqual(actual[5][1], NULL_MARKER)
                self.assertEqual(actual[1][1], NULL_MARKER)

    def test_default_null_text_keeps_marker_like_text(self):
        # 默认空标记：NULL 成为空字段，同形普通文本 "未填写" 原样保留
        expected = [row[:] for row in EXPECTED_RECORDS]
        expected[1][1] = ""
        _, _, records = self._run_export(null_text="")
        _, _, preview_records = self._run_preview(10, null_text="")

        self.assertEqual(records, expected)
        self.assertEqual(preview_records, expected)
        # 小强的 "未填写" 是源数据普通文本，默认标记下不得被清空或改写
        self.assertEqual(records[5][1], NULL_MARKER)

    # -- 字段内换行按逻辑记录比较 --------------------------------------------

    def test_embedded_newline_is_single_logical_record_in_both_entries(self):
        _, export_text, export_records = self._run_export()
        _, preview_text, preview_records = self._run_preview(10)

        for text, records in ((export_text, export_records),
                              (preview_text, preview_records)):
            with self.subTest(text=text):
                # 物理行数因字段内换行多于 6，逻辑记录仍恰为表头 + 5 行
                self.assertGreater(len(text.splitlines()), 6)
                self.assertEqual(len(records), 6)
                # 首尾空格与字段内换行完整还原为一个字段
                self.assertEqual(records[4], ["小丽", NOTE_MULTILINE, "2"])
        # 两条入口的含换行字段逐字节一致
        self.assertEqual(export_records[4], preview_records[4])

    # -- 零行结果：两条入口都只输出表头 --------------------------------------

    def test_zero_rows_both_entries_emit_header_only(self):
        sql = (
            "SELECT p.name AS 姓名, n.note AS 备注, n.flag AS 标记 "
            "FROM people p JOIN notes n ON p.id=n.person_id "
            "WHERE p.id < 0 ORDER BY p.id"
        )
        count, export_text, export_records = self._run_export(sql=sql)
        shown, preview_text, preview_records = self._run_preview(3, sql=sql)

        self.assertEqual(count, 0)
        self.assertEqual(shown, 0)
        # 零行结果仍输出表头，且两条入口逐字节一致
        self.assertEqual(export_records, [EXPECTED_RECORDS[0]])
        self.assertEqual(preview_records, [EXPECTED_RECORDS[0]])
        self.assertEqual(preview_text, export_text)

    # -- 命令行入口：预览与导出的对照 ----------------------------------------

    def test_cli_preview_matches_cli_export(self):
        proc_export = self._run_cli(
            "--sql", JOIN_SQL, "--output", self.out_path,
            "--null-text", NULL_MARKER,
        )
        # 导出成功：退出码 0，标准错误为空，保留原成功提示
        self.assertEqual(proc_export.returncode, 0, proc_export.stderr)
        self.assertEqual(proc_export.stderr, "")
        self.assertIn("已导出 5 行数据", proc_export.stdout)
        with open(self.out_path, "r", encoding="utf-8", newline="") as f:
            export_records = self._parse_csv(f.read())
        self.assertEqual(export_records, EXPECTED_RECORDS)

        # 全量预览：标准输出仅为 CSV，逻辑记录与导出文件一致
        proc_preview = self._run_cli(
            "--sql", JOIN_SQL, "--preview", "10",
            "--null-text", NULL_MARKER,
        )
        self.assertEqual(proc_preview.returncode, 0, proc_preview.stderr)
        self.assertEqual(proc_preview.stderr, "")
        self.assertNotIn("已导出", proc_preview.stdout)
        self.assertEqual(self._parse_csv(proc_preview.stdout), EXPECTED_RECORDS)

        # 预览一行：恰为全量导出的表头 + 首条数据记录
        proc_one = self._run_cli(
            "--sql", JOIN_SQL, "--preview", "1",
            "--null-text", NULL_MARKER,
        )
        self.assertEqual(proc_one.returncode, 0, proc_one.stderr)
        self.assertEqual(proc_one.stderr, "")
        self.assertEqual(
            self._parse_csv(proc_one.stdout), EXPECTED_RECORDS[:2]
        )


if __name__ == "__main__":
    unittest.main()
