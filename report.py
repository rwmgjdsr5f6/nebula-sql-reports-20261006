#!/usr/bin/env python3
"""以只读方式对 SQLite 执行一条 SELECT，并将结果导出为带列名的 CSV。

用法:
    python report.py --db DB.sqlite --sql "SELECT ..." --output out.csv
    python report.py --db DB.sqlite --sql-file query.sql --output out.csv
    可在任一来源后追加可重复的 --param name=value 为查询提供文本参数

--sql 与 --sql-file 必须恰好选择一个；查询文件按 UTF-8 读取（允许开头
一个 BOM），文件内容适用与 --sql 完全相同的规则。仅接受一条 SELECT
语句（允许首尾空白与结尾分号）；WITH、PRAGMA、写入语句及多语句输入
一律拒绝。数据库以只读方式打开，缺失时不会创建；输出目标已存在时拒绝
写入，绝不覆盖或截断；查询文件只被读取，绝不改写。

--param 可重复，形如 name=value，可与任一查询来源搭配：值在第一个等号
处切分，其后的等号、空格、中文、引号与分号原样保留；空字符串也是合法
值（--param name=）。参数名区分大小写，首字符为 ASCII 字母或下划线，
其后仅含 ASCII 字母、数字或下划线。SQL 中支持 :name、@name、$name 三种
命名占位符，重复引用同名占位符使用同一个值；字符串、引号标识符与注释
中的类似文本不算占位符。参数值只作为数据绑定，不可能改变 SQL 结构；未
被查询引用的合法参数忽略。不接受位置占位符（? 与 ?1 等编号形式），也
不接受 SQLite 允许但超出本工具约定的非法名称（如 :1、:谁）；本次参数
仅为文本，数字、null、true/false 等词一律按字符串绑定，不做类型转换。
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from pathlib import Path


def die(message):
    """向标准错误输出具体原因并以退出码 1 结束。"""
    print("错误: " + message, file=sys.stderr)
    sys.exit(1)


def _skip_quoted(sql, i):
    """若 sql[i] 是引号（'/\"/`）或方括号标识符，返回跳过该段后的下标；否则返回 None。"""
    n = len(sql)
    ch = sql[i]
    if ch in ("'", '"', "`"):
        quote = ch
        i += 1
        while i < n:
            if sql[i] == quote:
                # SQL 标准：引号在自身引号内双写表示字面引号
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                return i + 1
            i += 1
        return n  # 未闭合的引号：吞到末尾，交由 SQLite 报语法错误
    if ch == "[":
        i += 1
        while i < n:
            if sql[i] == "]":
                return i + 1
            i += 1
        return n
    return None


def strip_sql_comments(sql):
    """去除 SQL 中的 -- 行注释与 C 风格块注释，引号内的注释文本保持原样。

    块注释统一替换为一个空格（而非直接删除），使其与普通空白分隔语义
    一致：SE/*x*/LECT 不会被拼成 SELECT，列别名等相邻词语也不会被改写。
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        skipped = _skip_quoted(sql, i)
        if skipped is not None:
            out.append(sql[i:skipped])
            i = skipped
            continue
        if sql[i] == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            if j == -1:
                break
            i = j
            continue
        if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                # 未闭合的块注释：替换为空格后截断，交由 SQLite 判语法
                out.append(" ")
                break
            out.append(" ")  # 已闭合块注释等价于一个空白分隔符
            i = j + 2
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


# 本工具接受的参数名：区分大小写，首字符为 ASCII 字母或下划线，
# 其后仅含 ASCII 字母、数字或下划线。字典键与 --param 名称均不带前缀。
PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# SQLite 对 :/@/$ 后名称的接受范围比本工具宽（允许数字开头、非 ASCII），
# 这里用宽松的字节集识别"看起来像占位符"的 token，再用上面的严格规则判定。
_LOOSE_NAME_CHARS = re.compile(r"[0-9A-Za-z_$:\u0080-\U0010ffff]")
_NAMED_PREFIXES = (":", "@", "$")


def scan_sql_parameters(sql):
    """扫描原始 SQL，返回其中实际出现的参数信息。

    返回 (named, positional)：named 为按首次出现顺序排列的占位符
    (前缀, 名称) 列表（重复出现的同名占位符一并保留，供名称合法性
    逐条报错）；positional 为出现位置占位符（"?" 或 "?1" 等编号形式）
    的布尔。字符串、引号标识符（'/\"/`/[…]）与注释（-- 行注释、
    /* 块注释 */）中的类似文本一律忽略。
    """
    named = []
    positional = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        skipped = _skip_quoted(sql, i)
        if skipped is not None:
            i = skipped
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch in _NAMED_PREFIXES:
            j = i + 1
            while j < n and _LOOSE_NAME_CHARS.match(sql[j]):
                j += 1
            if j > i + 1:
                named.append((ch, sql[i + 1:j]))
            i = j
            continue
        if ch == "?":
            # ? 本身，或 ?1/?123 这类编号位置占位符；名称占位符另有前缀
            positional = True
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            i = j
            continue
        i += 1
    return named, positional


def normalize_params(params):
    """校验调用方传入的 params：None 表示不带参数（保留旧式三参行为）。

    通过时返回一个全新的字符串字典副本；不合法抛 ValueError。
    键必须全部为字符串且符合参数名规则，值必须全部为字符串；空串合法。
    """
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("参数必须是字符串字典（名称到文本值的映射）")
    normalized = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise ValueError("参数名必须是字符串，收到非字符串键：%r" % (key,))
        if not PARAM_NAME_RE.match(key):
            raise ValueError(
                "非法参数名 %r：首字符须为 ASCII 字母或下划线，"
                "其后仅含 ASCII 字母、数字或下划线" % key
            )
        if not isinstance(value, str):
            raise ValueError(
                "参数 %r 的值必须是文本字符串，收到非字符串值：%r" % (key, value)
            )
        normalized[key] = value
    return normalized


def bind_parameters(sql, params):
    """把 SQL 中的命名占位符与文本参数字典对应起来，返回供绑定用的字典。

    只做参数层面的检查（SQL 结构校验由 validate_single_select 负责）：
    拒绝位置占位符、拒绝超出本工具约定的占位符名称、拒绝缺少必要参数；
    未被查询引用的合法参数忽略。返回的字典只含查询实际引用到的参数。
    """
    named, positional = scan_sql_parameters(sql)
    if positional:
        raise ValueError("不支持位置占位符（? 或 ?1 等）：请改用 :name/@name/$name")

    required = set()
    for prefix, name in named:
        if not PARAM_NAME_RE.match(name):
            raise ValueError(
                "非法占位符 %s%s：参数名首字符须为 ASCII 字母或下划线，"
                "其后仅含 ASCII 字母、数字或下划线" % (prefix, name)
            )
        required.add(name)

    missing = sorted(name for name in required if name not in params)
    if missing:
        raise ValueError("查询引用了未提供的必要参数：%s" % "、".join(missing))

    return {name: params[name] for name in required}


def split_statements(sql, keep_empty=False):
    """按分号切分语句，字符串与标识符引号内的分号不切分。

    返回去掉首尾空白后的语句列表；keep_empty 时保留空段，用于识别
    "前导分号"、"多个分号"等多语句形态。
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    while i < n:
        skipped = _skip_quoted(sql, i)
        if skipped is not None:
            buf.append(sql[i:skipped])
            i = skipped
            continue
        if sql[i] == ";":
            stmt = "".join(buf).strip()
            if stmt or keep_empty:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    tail = "".join(buf).strip()
    if tail or keep_empty:
        statements.append(tail)
    return statements


def first_token(statement):
    """取语句首个词法 token（大写）及其后的文本；左括号原样返回。"""
    s = statement.lstrip()
    if s[:1] == "(":
        return "(", s
    end = 0
    while end < len(s) and (s[end].isalnum() or s[end] == "_"):
        end += 1
    return s[:end].upper(), s[end:].lstrip()


def unwrap_full_parentheses(statement):
    """若整条语句被一对外层括号包裹，返回括号内文本；否则返回 None。"""
    s = statement.strip()
    if not s.startswith("("):
        return None
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        skipped = _skip_quoted(s, i)
        if skipped is not None:
            i = skipped
            continue
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                if s[i + 1:].strip():
                    return None  # 闭合括号后还有多余内容
                return s[1:i].strip()
        i += 1
    return None  # 括号未闭合


def validate_single_select(sql, _depth=0):
    """校验输入恰为一条 SELECT 语句，返回可执行的语句文本，不合法则抛 ValueError。"""
    if _depth > 20:
        raise ValueError("不支持的语句：括号嵌套过深")

    segments = split_statements(strip_sql_comments(sql), keep_empty=True)
    nonempty = [s for s in segments if s]
    if not nonempty:
        raise ValueError("SQL 为空：必须提供一条 SELECT 语句")
    if len(nonempty) > 1:
        raise ValueError("仅允许一条 SELECT 语句，检测到多条语句输入")
    # 只允许末尾一个分号：空段至多一个，且必须位于最后
    if len(segments) - 1 > 1 or segments[0] == "":
        raise ValueError("仅允许一条 SELECT 语句，检测到多余的分号或多条语句")

    statement = nonempty[0]
    keyword, rest = first_token(statement)

    if keyword == "(":
        inner = unwrap_full_parentheses(statement)
        if inner is None:
            raise ValueError("不支持的语句：仅允许 SELECT")
        validate_single_select(inner, _depth + 1)
        return statement  # 原文本括号合法，可直接交给 SQLite

    if keyword == "WITH":
        raise ValueError("不支持的语句：WITH/CTE 查询被拒绝，仅允许普通 SELECT")
    if keyword == "EXPLAIN":
        raise ValueError("不支持的语句：EXPLAIN 被拒绝，仅允许 SELECT")
    if keyword == "PRAGMA":
        raise ValueError("不支持的语句：PRAGMA 被拒绝，仅允许 SELECT")
    if keyword != "SELECT":
        raise ValueError("不支持的语句：仅允许 SELECT，但收到 %r" % (keyword or "非语句文本"))
    if not rest:
        raise ValueError("SQL 语法错误：SELECT 后缺少查询列")
    return statement


def read_sql_file(path):
    """读取查询文件文本：按 UTF-8 解码，允许开头恰一个 BOM；失败抛 ValueError。

    文件只被读取，绝不改写；路径可包含中文与空格。读到的文本与 --sql
    适用完全相同的 SQL 规则（由 validate_single_select 统一校验）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise ValueError("查询文件不存在：%s" % path)
    except IsADirectoryError:
        raise ValueError("查询文件路径是目录而非文件：%s" % path)
    except OSError as exc:
        raise ValueError("无法读取查询文件 %s：%s" % (path, exc))
    try:
        # utf-8-sig 仅在开头恰有一个 BOM 时将其剥除，其余字节原样解码
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("查询文件不是有效的 UTF-8 文本 %s：%s" % (path, exc))


def open_readonly(db_path):
    """以只读模式打开 SQLite；文件缺失或损坏时抛 ValueError，且绝不会创建新库。"""
    uri = Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        # 连接惰性建立；读取 sqlite_master 会强制解析文件头与首页，
        # 从而暴露"文件不存在"与"不是数据库"等问题（SELECT 1 做不到）。
        conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise ValueError("无法以只读方式打开数据库 %s：%s" % (db_path, exc))
    return conn


def export_csv(db_path, sql_text, output_path, params=None):
    """执行查询并将结果独占写入目标 CSV，返回数据行数。任何拒绝路径都不建文件。

    params 省略或为 None 时保持旧式三参调用行为；否则须为字符串字典，
    键为不带前缀的参数名，值为文本（空串合法，数字/null/布尔词不做类型
    转换），仅作为数据绑定，不能改变 SQL 结构。
    """
    param_values = normalize_params(params)
    statement = validate_single_select(sql_text)
    bindings = bind_parameters(sql_text, param_values)

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(output_dir):
        raise ValueError("输出目录不存在：%s" % output_dir)
    if os.path.exists(output_path):
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)

    conn = open_readonly(db_path)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(statement, bindings)
        except sqlite3.Error as exc:
            raise ValueError("SQL 执行失败：%s" % exc)
        if cursor.description is None:
            raise ValueError("不支持的语句：仅允许返回结果集的 SELECT")
        headers = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    finally:
        conn.close()

    # "x" = 独占新建：目标已存在则直接失败，从根本上杜绝覆盖或截断
    try:
        with open(output_path, "x", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(["" if value is None else value for value in row])
    except FileExistsError:
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)
    except (OSError, UnicodeError, TypeError) as exc:
        # 到此的文件必为本调用刚创建的半成品，清理后报错；既存文件不可能被触及
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise ValueError("无法写入输出文件 %s：%s" % (output_path, exc))

    return len(rows)


class _Parser(argparse.ArgumentParser):
    """参数用法错误也按本工具的约定使用退出码 1（argparse 默认是 2）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        die("参数错误：%s" % message)


def parse_cli_params(raw_items, parser):
    """把可重复的 --param name=value 解析为字符串字典，非法时经 parser 拒绝。

    在第一个等号处切分名称与值；值中的其余等号、空格、中文、引号与分号
    原样保留，空值合法。缺少等号、名称非法、同名重复提供一律拒绝。
    """
    params = {}
    for item in raw_items or []:
        if "=" not in item:
            parser.error("--param 需要形如 name=value，缺少等号：%r" % item)
        name, value = item.split("=", 1)
        if not PARAM_NAME_RE.match(name):
            parser.error(
                "--param 名称非法 %r：首字符须为 ASCII 字母或下划线，"
                "其后仅含 ASCII 字母、数字或下划线" % name
            )
        if name in params:
            parser.error("--param 重复提供同名参数：%s" % name)
        params[name] = value
    return params


def parse_args(argv):
    parser = _Parser(
        description="对 SQLite 执行一条只读 SELECT 并导出带列名的 CSV"
    )
    parser.add_argument("--db", required=True, help="已有 SQLite 数据库文件路径")
    parser.add_argument("--sql", help="一条 SELECT 查询文本")
    parser.add_argument(
        "--sql-file", help="包含一条 SELECT 查询的 UTF-8 文件路径（允许一个 BOM）"
    )
    parser.add_argument("--output", required=True, help="输出 CSV 路径（不得已存在）")
    parser.add_argument(
        "--param",
        action="append",
        metavar="name=value",
        help="可重复的文本查询参数，值在第一个等号后原样保留（空值合法）",
    )
    args = parser.parse_args(argv)
    # 恰好选择一个查询来源；此判定发生在读文件与开库之前
    if (args.sql is None) == (args.sql_file is None):
        parser.error("--sql 与 --sql-file 必须恰好选择一个")
    args.params = parse_cli_params(args.param, parser)
    return args


def main(argv=None):
    args = parse_args(argv)
    sql_text = args.sql
    if sql_text is None:
        # 先读查询文件：文件类失败时不接触源库与输出目标
        try:
            sql_text = read_sql_file(args.sql_file)
        except ValueError as exc:
            die(str(exc))
    try:
        row_count = export_csv(
            args.db, sql_text, args.output, params=args.params
        )
    except ValueError as exc:
        die(str(exc))
    except sqlite3.Error as exc:
        die("数据库错误：%s" % exc)
    except OSError as exc:
        die("文件错误：%s" % exc)
    print("已导出 %d 行数据：%s" % (row_count, args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
