#!/usr/bin/env python3
"""export_csv 与 export_html 的结果一致性回归测试。

同一查询换成另一种报告格式后，结果表格内容必须一致：列名、行列顺序、
单元格文本与返回行数相同。比较对象是报告中的结果表格——CSV 按逻辑记录
（csv.reader）解析，HTML 按表格行（HTMLParser，字符引用还原为文字）解析；
两种格式各自保留自己的结构与转义规则，不在字节层面互相比较。

样例数据覆盖：SQL NULL、空字符串、与 null_text 同形的普通文本、含
&<script>"' 的特殊字符文本、首尾空格与字段内换行、零值标记。字段内换行
只属于一个单元格；HTML 中的 <script> 只显示为文字，不产生脚本元素。

只依赖 Python 标准库；每个用例在临时目录自行建库，每次导出使用不同的
未占用报告路径，用例结束（无论成功或失败）后以新的只读连接重读两表结构
及全部数据，确认与准备时一致，并清理全部临时文件。不依赖外部数据库。

在项目根目录执行：
    python -m unittest discover
"""

import csv
import io
import os
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser

import report

# 固定排序的联表查询：:start 按文本绑定，CAST 后按数值筛选 id；
# 列名、列顺序与行顺序在两种格式间必须一致
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注, n.flag AS 标记 "
    "FROM people p JOIN notes n ON p.id=n.person_id "
    "WHERE p.id >= CAST(:start AS INTEGER) ORDER BY p.id"
)

NULL_MARKER = "未填写"
# 小丽的备注：同时包含中文、逗号、&、<script>、双引号与单引号
NOTE_SPECIAL = "中文,&<script>\"'"
# 小强的备注：首尾各一个空格、内部含一次换行
NOTE_MULTILINE = " 首行\n次行 "

# 默认空值标记（空字符串）时两种格式都应得到的完整结果表格：
# 小明 NULL->空；小红空字符串原样；小华 "未填写" 是与标记同形的普通文本，
# 原样保留；零值 "0" 原样
EXPECTED_DEFAULT = [
    ["姓名", "备注", "标记"],
    ["小明", "", "0"],
    ["小红", "", "1"],
    ["小华", NULL_MARKER, "2"],
    ["小丽", NOTE_SPECIAL, "3"],
    ["小强", NOTE_MULTILINE, "4"],
]

# null_text="未填写" 时仅小明的 NULL 被替换为标记，其余单元格不变
EXPECTED_MARKER = [row[:] for row in EXPECTED_DEFAULT]
EXPECTED_MARKER[1][1] = NULL_MARKER


class _TableParser(HTMLParser):
    """提取 HTML 报告中的结果表格：按 tr 收集 th/td 文本。

    convert_charrefs=True 使字符引用（&amp; &lt; &quot; &#x27; 等）在
    handle_data 前还原为文字，解析结果即单元格的显示文本；同时记录
    出现过的全部标签名，用于确认 <script> 只作为文字存在。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.tags = []
        self._in_table = False
        self._current_row = None
        self._current_cell = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._current_row = []
        elif self._in_table and tag in ("th", "td"):
            self._current_cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif self._in_table and tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
        elif (self._in_table and tag in ("th", "td")
              and self._current_cell is not None):
            self._current_row.append("".join(self._current_cell))
            self._current_cell = None

    def handle_data(self, data):
        if self._current_cell is not None:
            self._current_cell.append(data)


class FormatConsistencyTestCase(unittest.TestCase):
    """同一查询下 export_csv 文件与 export_html 页面的结果表格对照。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中核对源库未被改写
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，以新的只读连接重读源库，
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
                "person_id INTEGER PRIMARY KEY, flag INTEGER, note TEXT)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                [(1, "小明"), (2, "小红"), (3, "小华"), (4, "小丽"), (5, "小强")],
            )
            conn.executemany(
                "INSERT INTO notes (person_id, flag, note) VALUES (?, ?, ?)",
                [
                    (1, 0, None),             # NULL 备注，零值标记
                    (2, 1, ""),               # 空字符串
                    (3, 2, NULL_MARKER),      # 与 null_text 同形的普通文本
                    (4, 3, NOTE_SPECIAL),     # 含 &<script>"' 的特殊字符
                    (5, 4, NOTE_MULTILINE),   # 首尾空格与字段内换行
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以新的只读连接重新读取源库的表结构与两表全部数据。"""
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
                "SELECT person_id, flag, note FROM notes ORDER BY person_id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "people": people, "notes": notes}

    def _fresh_path(self, name):
        """返回临时目录中一个尚未占用的报告路径（每次导出使用不同路径）。"""
        path = os.path.join(self.tmpdir, name)
        self.assertFalse(os.path.exists(path))
        return path

    @staticmethod
    def _parse_csv(text):
        """按 CSV 逻辑记录解析：字段内换行属于字段，不产生额外记录。"""
        return list(csv.reader(io.StringIO(text)))

    @staticmethod
    def _parse_html_table(text):
        """按表格行解析 HTML 报告，返回 (行列表, 出现过的标签名列表)。"""
        parser = _TableParser()
        parser.feed(text)
        parser.close()
        return parser.rows, parser.tags

    def _run_csv(self, name, params, null_text=""):
        """调用 export_csv，返回 (数据行数, 文件原文, 逻辑记录)。"""
        path = self._fresh_path(name)
        count = report.export_csv(
            self.db_path, JOIN_SQL, path, params=params, null_text=null_text
        )
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return count, text, self._parse_csv(text)

    def _run_html(self, name, params, null_text=""):
        """调用 export_html，返回 (数据行数, 页面原文, 表格行, 标签名列表)。"""
        path = self._fresh_path(name)
        count = report.export_html(
            self.db_path, JOIN_SQL, path, params=params, null_text=null_text
        )
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        rows, tags = self._parse_html_table(text)
        return count, text, rows, tags

    # -- 默认空值标记：两种格式的结果表格逐字段一致 --------------------------

    def test_csv_and_html_agree_with_default_null_text(self):
        csv_count, csv_text, csv_records = self._run_csv(
            "default.csv", {"start": "1"}
        )
        html_count, _, html_rows, _ = self._run_html(
            "default.html", {"start": "1"}
        )

        # 两种导出均返回 5（表头不计入）
        self.assertEqual(csv_count, 5)
        self.assertEqual(html_count, 5)
        # 各自符合由固定样例写出的期望值
        self.assertEqual(csv_records, EXPECTED_DEFAULT)
        self.assertEqual(html_rows, EXPECTED_DEFAULT)
        # CSV 六条逻辑记录与 HTML 还原字符引用后的六行逐字段相等
        self.assertEqual(csv_records, html_rows)
        # 字段内换行使 CSV 物理行数多于 6，逻辑记录仍恰为表头 + 5 行
        self.assertGreater(len(csv_text.splitlines()), 6)
        self.assertEqual(len(csv_records), 6)
        self.assertEqual(len(html_rows), 6)

    # -- null_text="未填写"：只替换 NULL -------------------------------------

    def test_csv_and_html_agree_with_null_marker(self):
        csv_count, _, csv_records = self._run_csv(
            "marker.csv", {"start": "1"}, null_text=NULL_MARKER
        )
        html_count, _, html_rows, _ = self._run_html(
            "marker.html", {"start": "1"}, null_text=NULL_MARKER
        )

        self.assertEqual(csv_count, 5)
        self.assertEqual(html_count, 5)
        self.assertEqual(csv_records, EXPECTED_MARKER)
        self.assertEqual(html_rows, EXPECTED_MARKER)
        self.assertEqual(csv_records, html_rows)
        for records in (csv_records, html_rows):
            with self.subTest(records=records):
                # 小明的 NULL 被替换为标记
                self.assertEqual(records[1][1], NULL_MARKER)
                # 小红的空字符串仍是空字段，不变成标记
                self.assertEqual(records[2][1], "")
                # 零值原样为 "0"，不变成标记
                self.assertEqual(records[1][2], "0")
                # 小华的 "未填写" 是源数据普通文本，原样保留（与 NULL 替换
                # 结果同形）：标记只替换 NULL，不做额外转换
                self.assertEqual(records[3][1], NULL_MARKER)

    # -- 特殊字符与字段内换行：各格式保留自己的转义规则 ----------------------

    def test_script_text_is_escaped_and_newline_stays_in_one_cell(self):
        _, _, csv_records = self._run_csv("special.csv", {"start": "1"})
        _, html_text, html_rows, tags = self._run_html(
            "special.html", {"start": "1"}
        )

        # HTML 中 <script> 只显示为文字：页面不产生脚本元素，
        # 原文中也找不到未转义的 "<script" 标签
        self.assertNotIn("script", tags)
        self.assertNotIn("<script", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        # 还原字符引用后，单元格文本与源数据逐字符一致
        self.assertEqual(html_rows[4][1], NOTE_SPECIAL)
        self.assertIn("<script>", html_rows[4][1])
        # 字段内换行只属于一个单元格：首尾空格与换行在两种格式中完整保留
        self.assertEqual(csv_records[5], ["小强", NOTE_MULTILINE, "4"])
        self.assertEqual(html_rows[5], ["小强", NOTE_MULTILINE, "4"])
        self.assertEqual(csv_records[5][1], html_rows[5][1])

    # -- 零行结果：两种格式都只保留三列表头 ----------------------------------

    def test_zero_rows_keep_three_column_header(self):
        csv_count, _, csv_records = self._run_csv(
            "empty.csv", {"start": "99"}
        )
        html_count, _, html_rows, _ = self._run_html(
            "empty.html", {"start": "99"}
        )

        self.assertEqual(csv_count, 0)
        self.assertEqual(html_count, 0)
        # 零行结果仍保留三列表头，且两种格式的表头一致
        self.assertEqual(csv_records, [EXPECTED_DEFAULT[0]])
        self.assertEqual(html_rows, [EXPECTED_DEFAULT[0]])
        self.assertEqual(csv_records, html_rows)
        self.assertEqual(csv_records[0], ["姓名", "备注", "标记"])

    # -- 缺少 start 参数：两入口均抛 ValueError 且不创建目标文件 -------------

    def test_missing_start_param_raises_and_creates_no_file(self):
        csv_path = self._fresh_path("missing.csv")
        html_path = self._fresh_path("missing.html")

        for entry, path in ((report.export_csv, csv_path),
                            (report.export_html, html_path)):
            with self.subTest(entry=entry.__name__):
                with self.assertRaises(ValueError) as ctx:
                    entry(self.db_path, JOIN_SQL, path)
                message = str(ctx.exception)
                # 消息包含缺少参数的原因与参数名 start
                self.assertIn("缺少", message)
                self.assertIn("start", message)
                # 拒绝路径不创建目标文件
                self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
