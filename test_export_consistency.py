#!/usr/bin/env python3
"""export_csv 与 export_html 的结果表格内容一致性回归测试。

同一查询、同一参数分别走 CSV 与 HTML 两条导出入口后，比较报告中的结果
表格内容：列名、列顺序、行顺序、逐单元格文本与返回数据行数必须一致。
各格式保留自己的结构与转义规则——CSV 按逻辑记录（csv.reader，字段内
换行属于一个单元格）解析，HTML 用 HTMLParser 还原字符引用后取表格
单元格文本，再把两边还原出的纯文本矩阵逐字段对照。

固定样例为 people/notes 两表（按 id 与 person_id 一对一关联）：
备注依次为 SQL NULL、空字符串、普通文本“未填写”、
含 &、<、>、双引号、单引号与“<script>”形态的中文文本、
以及首尾各一个空格且内部含一次换行的文本；flag 依次为 0..4。
查询按 id 升序输出“姓名、备注、标记”三列，并用命名参数 :start 筛选
id >= :start。

只依赖 Python 标准库；每个用例在独立临时目录建库，每次导出都使用新的
未占用目标路径；用例结束后通过新的只读连接重读两表结构及全部数据，
确认与准备时一致，随后清理全部临时文件。

在项目根目录执行：
    python -m unittest discover
或单独运行：
    python -m unittest test_export_consistency
"""

import csv
import io
import os
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser

import report

# 按 id 升序的三列联表查询：:start 筛选 id >= :start
JOIN_SQL = (
    "SELECT p.name AS 姓名, n.note AS 备注, n.flag AS 标记 "
    "FROM people p JOIN notes n ON p.id = n.person_id "
    "WHERE p.id >= :start ORDER BY p.id ASC"
)

HEADER = ["姓名", "备注", "标记"]
NULL_MARKER = "未填写"
# 小丽的备注：中文加逗号与全部五种 HTML 特殊字符，并呈 <script> 形态
NOTE_SPECIAL = '中文,&<script>"\''
# 小强的备注：首尾各一个空格，内部含一次字段内换行
NOTE_MULTILINE = " 首行\n次行 "

NAMES = ["小明", "小红", "小华", "小丽", "小强"]

# 默认空值标记（NULL -> 空字段/空单元格）下，由固定样例写出的期望矩阵：
# 首行为列名，其后五行按 id 升序；NULL 对应字段为空，源数据中的空字符串
# （小红）同样为空但来自非 NULL 值，普通文本“未填写”（小华）原样保留
EXPECTED_DEFAULT = [
    HEADER,
    ["小明", "", "0"],
    ["小红", "", "1"],
    ["小华", NULL_MARKER, "2"],
    ["小丽", NOTE_SPECIAL, "3"],
    ["小强", NOTE_MULTILINE, "4"],
]

# null_text="未填写" 时的期望矩阵：只有小明的 NULL 被替换；
# 小红的空字符串仍为空，零值仍为 "0"，小华同形普通文本不做额外转换
EXPECTED_WITH_MARKER = [
    HEADER,
    ["小明", NULL_MARKER, "0"],
    ["小红", "", "1"],
    ["小华", NULL_MARKER, "2"],
    ["小丽", NOTE_SPECIAL, "3"],
    ["小强", NOTE_MULTILINE, "4"],
]


class _TableExtractor(HTMLParser):
    """收集页面中每张表格的单元格：(标签名 th/td, 还原字符引用后的文本)。

    convert_charrefs=True 使 &amp;、&lt;、&#x27; 等字符引用在回调前
    已还原为原文字符；单元格内的首尾空格与换行原样保留，不做规整。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell = [tag, []]

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[1].append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self._cell is not None:
            self._row.append((self._cell[0], "".join(self._cell[1])))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


class ExportCsvHtmlConsistencyTestCase(unittest.TestCase):
    """同一查询下 export_csv 与 export_html 结果表格内容的逐项对照。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "sample.sqlite")
        self._path_counter = 0
        self._prepare_db()
        # 准备完成时的结构与数据基线，tearDown 中用新的只读连接核对
        self._baseline = self._snapshot_db()

    def tearDown(self):
        # 每个用例（无论成功或失败）结束后，通过新的只读连接重读两表
        # 结构及全部数据，确认与准备时完全一致
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
                "person_id INTEGER, flag INTEGER, note TEXT)"
            )
            conn.executemany(
                "INSERT INTO people (id, name) VALUES (?, ?)",
                list(enumerate(NAMES, start=1)),
            )
            # 备注依次为：NULL、空字符串、“未填写”、特殊字符中文、
            # 首尾空格加字段内换行；flag 依次为 0..4
            conn.executemany(
                "INSERT INTO notes (person_id, flag, note) VALUES (?, ?, ?)",
                [
                    (1, 0, None),
                    (2, 1, ""),
                    (3, 2, NULL_MARKER),
                    (4, 3, NOTE_SPECIAL),
                    (5, 4, NOTE_MULTILINE),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _snapshot_db(self):
        """以新的只读连接读取两表结构及全部数据。"""
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
                "SELECT person_id, flag, note FROM notes "
                "ORDER BY person_id"
            ).fetchall()
        finally:
            conn.close()
        return {"schema": schema, "people": people, "notes": notes}

    def _fresh_path(self, suffix):
        """每次返回一个不同的、当前未被占用的报告路径。"""
        self._path_counter += 1
        path = os.path.join(
            self.tmpdir, "report_%d%s" % (self._path_counter, suffix)
        )
        self.assertFalse(os.path.exists(path))
        return path

    @staticmethod
    def _read_csv_records(path):
        """按 CSV 逻辑记录读取：引号内换行属于单元格，不产生额外记录。"""
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        return text, list(csv.reader(io.StringIO(text)))

    @staticmethod
    def _read_html_table(path):
        """读取 HTML 页面，返回 (原文, 唯一结果表的 (标签, 文本) 行列表)。"""
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        parser = _TableExtractor()
        parser.feed(text)
        parser.close()
        assert len(parser.tables) == 1, "页面应恰有一张结果表"
        return text, parser.tables[0]

    @staticmethod
    def _text_matrix(tagged_rows):
        """丢掉 th/td 标签，只留还原后的单元格文本矩阵。"""
        return [[text for _tag, text in row] for row in tagged_rows]

    def _export_both(self, params, null_text=""):
        """分别导出 CSV 与 HTML，返回两种格式解析出的全部结果。"""
        csv_path = self._fresh_path(".csv")
        html_path = self._fresh_path(".html")
        csv_count = report.export_csv(
            self.db_path, JOIN_SQL, csv_path,
            params=params, null_text=null_text,
        )
        html_count = report.export_html(
            self.db_path, JOIN_SQL, html_path,
            params=params, null_text=null_text,
        )
        csv_raw, csv_records = self._read_csv_records(csv_path)
        html_raw, html_tagged = self._read_html_table(html_path)
        return {
            "csv_count": csv_count,
            "html_count": html_count,
            "csv_raw": csv_raw,
            "html_raw": html_raw,
            "csv": csv_records,
            "html_tagged": html_tagged,
            "html": self._text_matrix(html_tagged),
            "csv_path": csv_path,
            "html_path": html_path,
        }

    def _assert_matrices_equal_field_by_field(self, csv_rows, html_rows):
        """列名、行数、行顺序与逐单元格文本在两种格式间完全一致。"""
        self.assertEqual(len(csv_rows), len(html_rows))
        for r, (csv_row, html_row) in enumerate(zip(csv_rows, html_rows)):
            self.assertEqual(len(csv_row), len(html_row), r)
            for c, (csv_cell, html_cell) in enumerate(zip(csv_row, html_row)):
                # 全部空格与换行在比较中保留
                self.assertEqual(
                    csv_cell, html_cell, "第 %d 行第 %d 列不一致" % (r, c)
                )

    # -- 默认空值标记：两种导出逐字段一致且符合固定样例 --------------------

    def test_default_marker_start_1_returns_five_equal_rows(self):
        result = self._export_both({"start": "1"})

        # 两种入口返回的数据行数一致，均为 5（不含表头）
        self.assertEqual(result["csv_count"], 5)
        self.assertEqual(result["html_count"], 5)
        # CSV 为六条逻辑记录（表头 + 5 行），HTML 还原后为六行
        self.assertEqual(len(result["csv"]), 6)
        self.assertEqual(len(result["html"]), 6)

        # 各自符合由固定样例写出的期望值
        self.assertEqual(result["csv"], EXPECTED_DEFAULT)
        self.assertEqual(result["html"], EXPECTED_DEFAULT)
        # 再逐字段核对一次两种格式的列名、行列顺序与单元格文本
        self._assert_matrices_equal_field_by_field(
            result["csv"], result["html"]
        )
        # 显式核对列名与列顺序
        self.assertEqual(result["csv"][0], HEADER)
        self.assertEqual(result["html"][0], HEADER)

    def test_whitespace_newline_and_html_escaping_preserved(self):
        result = self._export_both({"start": "1"})

        # 字段内换行只属于一个单元格：CSV 物理行多于 6，逻辑记录仍恰为 6
        self.assertGreater(len(result["csv_raw"].splitlines()), 6)
        self.assertEqual(len(result["csv"]), 6)
        # 首尾空格与内部换行在两种格式中完整保留为同一文本
        self.assertEqual(result["csv"][5][1], NOTE_MULTILINE)
        self.assertEqual(result["html"][5][1], NOTE_MULTILINE)

        # HTML 中的 <script> 只显示为文字：页面不产生脚本元素
        self.assertNotIn("<script", result["html_raw"])
        self.assertNotIn("</script", result["html_raw"])
        # 五种特殊字符在原文中都以字符引用形态出现
        for escaped in ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;"):
            self.assertIn(escaped, result["html_raw"])
        # 还原字符引用后，该单元格文本与 CSV 字段逐字相等
        self.assertEqual(result["html"][4][1], NOTE_SPECIAL)
        self.assertEqual(result["csv"][4][1], NOTE_SPECIAL)

    def test_default_marker_empties_only_null_cell(self):
        result = self._export_both({"start": "1"})

        for matrix in (result["csv"], result["html"]):
            with self.subTest(matrix=matrix):
                # 只有小明的 NULL 备注对应空字段
                self.assertEqual(matrix[1][1], "")
                # 源数据中的空字符串（小红）仍为空，来源不是 NULL 替换
                self.assertEqual(matrix[2][1], "")
                # 零值原样为 "0"
                self.assertEqual(matrix[1][2], "0")
                # 同形普通文本“未填写”（小华）保持不变
                self.assertEqual(matrix[3][1], NULL_MARKER)
        # 空单元格在 HTML 原文中确为空 td；NULL 与空字符串都无文字
        self.assertEqual(result["html_tagged"][1][1], ("td", ""))
        self.assertEqual(result["html_tagged"][2][1], ("td", ""))

    # -- null_text="未填写"：只替换 NULL，其余原样 -------------------------

    def test_null_text_marker_replaces_only_null_in_both_formats(self):
        result = self._export_both({"start": "1"}, null_text=NULL_MARKER)

        self.assertEqual(result["csv_count"], 5)
        self.assertEqual(result["html_count"], 5)
        self.assertEqual(result["csv"], EXPECTED_WITH_MARKER)
        self.assertEqual(result["html"], EXPECTED_WITH_MARKER)
        self._assert_matrices_equal_field_by_field(
            result["csv"], result["html"]
        )

        for matrix in (result["csv"], result["html"]):
            with self.subTest(matrix=matrix):
                # 只有 NULL（小明）被替换为标记
                self.assertEqual(matrix[1][1], NULL_MARKER)
                # 源数据空字符串（小红）不被替换
                self.assertEqual(matrix[2][1], "")
                # 零值不被替换
                self.assertEqual(matrix[1][2], "0")
                # 与标记同形的普通文本（小华）原样保留
                self.assertEqual(matrix[3][1], NULL_MARKER)

    # -- start=99：零行，两种导出都只保留三列表头 --------------------------

    def test_start_99_returns_zero_rows_with_header_only(self):
        result = self._export_both({"start": "99"})

        self.assertEqual(result["csv_count"], 0)
        self.assertEqual(result["html_count"], 0)
        # 各自只保留三列表头
        self.assertEqual(result["csv"], [HEADER])
        self.assertEqual(result["html"], [HEADER])
        self.assertEqual(len(result["csv"][0]), 3)
        self.assertEqual(len(result["html"][0]), 3)
        # HTML 表头单元格全部是 th，没有任何数据 td 行
        self.assertEqual(
            [tag for tag, _text in result["html_tagged"][0]],
            ["th", "th", "th"],
        )
        self.assertEqual(len(result["html_tagged"]), 1)
        # CSV 物理上也只有表头一行
        self.assertEqual(len(result["csv_raw"].splitlines()), 1)
        self._assert_matrices_equal_field_by_field(
            result["csv"], result["html"]
        )

    # -- 缺少 start：两入口都抛 ValueError 且不创建目标文件 ---------------

    def test_missing_start_raises_and_creates_no_file(self):
        for missing_params in (None, {}):
            for exporter, suffix in (
                (report.export_csv, ".csv"),
                (report.export_html, ".html"),
            ):
                with self.subTest(
                    exporter=exporter.__name__, params=missing_params
                ):
                    # 每个分支都使用新的未占用报告路径
                    path = self._fresh_path(
                        "_missing_%s%s" % (exporter.__name__, suffix)
                    )
                    with self.assertRaises(ValueError) as ctx:
                        exporter(
                            self.db_path, JOIN_SQL, path,
                            params=missing_params,
                        )
                    message = str(ctx.exception)
                    # 错误消息包含缺少参数的原因与参数名 start
                    self.assertIn("缺少", message)
                    self.assertIn("start", message)
                    # 失败后不创建目标文件
                    self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
