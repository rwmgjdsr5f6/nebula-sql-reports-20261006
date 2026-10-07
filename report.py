#!/usr/bin/env python3
"""以只读方式对 SQLite 执行一条 SELECT，并将结果导出为带列名的 CSV 或 HTML 报告。

用法:
    python report.py --db DB.sqlite --sql "SELECT ..." --output out.csv
    python report.py --db DB.sqlite --sql-file query.sql --output out.csv
    python report.py --db DB.sqlite --sql-file query.sql --output out.csv \
        --param who=小红 --param tag=a=b
    python report.py --db DB.sqlite --sql-file query.sql --output out.csv \
        --params-file params.json
    python report.py --db DB.sqlite --describe notes
    python report.py --db sample.sqlite --tables

--describe NAME 为只读查看单张用户表列结构的独立模式：仅与 --db 搭配，
不需要 --sql/--sql-file 或 --output，也不与任何其他现有选项同用。成功
时只向标准输出写入带表头的 CSV，退出码 0，标准错误为空，不追加提示、
不创建任何文件。首行为固定表头 cid,name,type,notnull,dflt_value,pk，
其后每个声明列一条记录，按数据库中的列序号升序。cid 从 0 开始；
notnull 保留 SQLite 返回的 0/1，不因主键身份改写；pk 为 0 表示不属
主键，非零值保留主键中的序号。无声明类型时 type 为空字段；无默认值
时 dflt_value 为空字段，显式 DEFAULT NULL 保留文本 NULL，其他默认
表达式保留元数据原文、不求值。列名等文本中的中文、空格、逗号与引号
按现有 CSV 规则保留。表名按保存名称精确匹配（区分大小写、不去除首尾
空白、中文/空格/引号按完整文本绑定查找，绝不作为 SQL 执行）；不
存在的表、视图及 sqlite_ 开头的内部表统一拒绝。该模式只读取元数据，
结构与数据保持不变；本轮不覆盖生成列、隐藏列、索引或外键展示。

--tables 为只读列出可查看用户表名的独立模式：仅与 --db 搭配，不需要
SQL 或 --output，也不与任何其他现有选项（包括显式指定的 --format csv）
同用。成功时只向标准输出写入带表头的 CSV，退出码 0，标准错误为空，不
追加提示、不创建任何文件。首行为固定单列表头 name，其后每张表一条记录，
返回值为表的数量（不含表头）。列举范围与 --describe 一致：仅主库
sqlite_master 中 type 为 table 且保存名称不以 sqlite_ 开头的对象；视图、
索引、触发器与 sqlite_sequence 均不出现。结果按保存名称的 SQLite
BINARY 顺序升序（区分大小写、不按创建顺序、不去除首尾空白），中文、
空格、逗号、双引号与换行按原始名称保留，经 CSV 解析可得到完整名称，
并可直接交给 --describe 按现有精确匹配语义使用。没有可列举的表时仍
输出 name 表头、返回 0。该模式只读取元数据，不读取表内数据，不输出
行数或列信息；源库以只读连接打开且保持不变，缺失或损坏时不创建任何
文件。

--sql 与 --sql-file 必须恰好选择一个；查询文件按 UTF-8 读取（允许开头
一个 BOM），文件内容适用与 --sql 完全相同的规则。仅接受一条 SELECT
语句（允许首尾空白与结尾分号）；WITH、PRAGMA、写入语句及多语句输入
一律拒绝。数据库以只读方式打开，缺失时不会创建；输出目标已存在时拒绝
写入，绝不覆盖或截断；查询文件只被读取，绝不改写。

--param name=value 可重复提供，为查询中的命名参数（:name、@name、$name）
提供文本值：在第一个等号处分开名称与值，值的其余等号、空格、中文、
引号和分号原样保留；空字符串是有效值。参数名区分大小写，首字符须为
ASCII 字母或下划线，后续仅含 ASCII 字母、数字或下划线。值一律按文本
绑定（数字、null、布尔词不转换类型），只作为数据参与查询，绝不拼接进
SQL 文本。字符串与注释中的类似文本不算参数；未被查询引用的合法参数
忽略；缺少查询引用的参数、或使用 ?、?1 位置占位符，均拒绝。

--params-file PATH 指定一个本地 JSON 参数文件，使同一份参数可复用于
CSV 导出、HTML 导出与终端预览：与 --sql 或 --sql-file 恰选一个的查询
来源配合，不改变其互斥规则。该选项只能提供一次且路径不能为空；与
--tables、--describe 混用，或重复提供、缺少路径时，在参数解析阶段即
拒绝，且绝不读取参数文件。文件按 UTF-8 读取（允许开头恰一个 BOM），
内容只接受一个 JSON 对象：键遵循与 --param 相同的参数名规则并区分
大小写，值必须全部为 JSON 字符串；空对象与空字符串值有效，数字、布尔
值、null、数组与嵌套对象一律拒绝，对象中的重复键也拒绝。字符串解码后
的中文、首尾空格、换行、等号、引号和分号原样保留，不推断类型，也不
拼接进 SQL。文件参数先与命令行 --param 合并再按既有规则绑定：同名时
命令行值覆盖文件值（命令行空字符串同样覆盖非空文件值），与选项在命令
行上的先后顺序无关；文件中的非法条目会被独立校验，不能借命令行覆盖
绕过。合并后缺少查询引用的参数或出现位置占位符仍按原规则失败，未被
引用的合法参数仍忽略。参数文件的读取与校验在读取查询文件、打开源库与
创建报告之前完成；文件不存在、路径是目录、不可读、编码或 JSON 错误及
上述结构、内容错误，均以退出码 1、标准输出为空、标准错误以"错误: "
开头并说明原因结束，失败不创建任何文件。

--null-text MARKER 可指定 SQL NULL 在输出中的导出标记，默认空字符串
（CSV 中即空字段，HTML 中即空单元格）。标记作为文本原样写入，可为空，
可包含中文、首尾空格、逗号、双引号与换行，遵循目标格式的常规规则；
只替换数据单元格中的 NULL，不影响列名、列顺序、行顺序、非空值与数据
行数。源数据中的空字符串仍写为空值，恰好与标记相同的普通文本不做额外
转换，也不保证输出能反向还原值类型。

--format 选择输出格式：csv（默认）或 html。输出类型只由该选项决定，
与输出文件扩展名无关。html 生成独立的 UTF-8 页面：声明字符编码，标题
为"查询报告"，含一张表格（表头 th、数据 td），列名、列顺序、行顺序与
查询结果一致；零行结果仍保留表头。单元格文本中的 &、<、>、双引号与
单引号按 HTML 转义为文字显示，不产生额外标签或脚本；中文、首尾空格与
换行完整保留。页面不依赖任何外部资源，直接打开即可查看。

--description TEXT 为 HTML 报告附加一段纯文本查询说明（如筛选口径），
显示在主标题之后、结果表格之前的独立文本区域；说明不进入表头、数据行、
浏览器标题或 SQL，也不改变数据行数。说明中的中文、首尾空格、连续空格与
换行在页面中保留，&、<、>、双引号与单引号按文字转义，类似 <script>
的内容只显示为文字。该选项仅与 --format html 搭配使用；显式提供
--description（即使为空）而选择 csv 或省略格式时按参数错误拒绝。
省略说明或传入空字符串时，HTML 输出与未提供说明时逐字节一致。

--title TEXT 为 HTML 报告指定自定义标题：同时用作浏览器页面标题与表格前
的主标题，默认"查询报告"。标题先去除首尾空白，中文与内部空白保留；
&、<、>、双引号与单引号按文字转义，不产生额外标签或脚本。标题不进入
SQL、查询说明、表头或数据行，也不改变数据行数、列名、列顺序与行顺序。
该选项仅与 --format html 的文件导出搭配使用；显式提供 --title 而选择
csv、省略格式或使用 --preview 时按参数错误拒绝，标题缺少文本值或去除
首尾空白后为空同样拒绝。省略标题时使用默认"查询报告"，输出与未提供标题
时逐字节一致；显式提供该默认标题也得到相同文件。

--preview N 在终端预览前 N 行：仍需 --db 并在 --sql 与 --sql-file 中恰选
一个来源，可继续使用 --param 和 --null-text，但不要求 --output；同时
提供 --preview 与 --output 按参数错误拒绝。N 为正整数，缺少值、零、
负数或非整数均拒绝。预览仅支持默认 CSV 或显式 --format csv，选择 html
或显式提供 --description（即使为空）、--title 按参数错误拒绝。成功时标准输出
仅包含带列名的 CSV（表头始终输出，数据行最多 N 条，不足 N 条全部
显示，零行只显示表头），退出码为 0，标准错误为空，不追加成功提示；
列名、列顺序、行顺序与查询结果一致，筛选、排序和 LIMIT 语义保留。
预览不创建报告或临时文件，源库和查询文件保持不变。
"""

import argparse
import csv
import html
import json
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


# 参数名：首字符为 ASCII 字母或下划线，后续仅含 ASCII 字母、数字或下划线；
# 区分大小写；字典键不带 :、@、$ 占位符前缀
PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_name_char(ch):
    """参数名字符：ASCII 字母、数字或下划线（非 ASCII 字符一律不算）。"""
    return ch.isascii() and (ch.isalnum() or ch == "_")


def validate_param_name(name):
    """校验单个参数名，非法时抛 ValueError；合法则原样返回。"""
    if not isinstance(name, str) or PARAM_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "非法参数名：%r（首字符须为 ASCII 字母或下划线，"
            "后续仅含 ASCII 字母、数字或下划线）" % (name,)
        )
    return name


def validate_params(params):
    """校验参数字典：None 原样返回；否则必须是 str->str 字典且键为合法参数名。

    值一律按文本处理：非字符串值（数字、None、布尔等）拒绝，字符串值
    （包括 "123"、"null"、"true" 与空字符串）原样保留，不做任何类型转换。
    """
    if params is None:
        return None
    if not isinstance(params, dict):
        raise ValueError(
            "参数必须是 名称->文本 的字符串字典，收到 %s" % type(params).__name__
        )
    for name, value in params.items():
        validate_param_name(name)
        if not isinstance(value, str):
            raise ValueError(
                "参数 %r 的值必须是字符串，收到 %s" % (name, type(value).__name__)
            )
    return dict(params)


def find_placeholders(statement):
    """扫描语句中的参数占位符，返回 (命名参数名列表, 是否含位置占位符)。

    字符串字面量、引号标识符与注释中的类似文本不算参数。命名参数支持
    :name、@name、$name 三种前缀，返回的名字不含前缀、按出现顺序排列
    （重复引用会重复出现）；? 与 ?N 归为位置占位符。
    """
    named = []
    positional = False
    i = 0
    n = len(statement)
    while i < n:
        skipped = _skip_quoted(statement, i)
        if skipped is not None:
            i = skipped
            continue
        ch = statement[i]
        # 语句文本通常已是 strip_sql_comments 的输出；此处仍跳过注释，
        # 保证本函数可独立用于任意 SQL 文本
        if ch == "-" and i + 1 < n and statement[i + 1] == "-":
            j = statement.find("\n", i + 2)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and statement[i + 1] == "*":
            j = statement.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "?":
            positional = True
            i += 1
            while i < n and statement[i].isdigit():
                i += 1
            continue
        if ch in ":@$" and i + 1 < n and _is_name_char(statement[i + 1]):
            j = i + 2
            while j < n and _is_name_char(statement[j]):
                j += 1
            named.append(statement[i + 1:j])
            i = j
            continue
        i += 1
    return named, positional


def bind_params(statement, params):
    """核对语句占位符与参数字典，返回可交给 sqlite3 的绑定字典或 None。

    位置占位符（?、?1）一律拒绝；查询引用而字典未提供的参数拒绝；
    未被引用的合法参数忽略。返回 None 表示语句没有任何占位符，
    调用方按无参数方式执行（保持原有行为）。
    """
    named, positional = find_placeholders(statement)
    if positional:
        raise ValueError(
            "不支持位置占位符 ? 或 ?N：请改用 :name、@name 或 $name 命名参数"
        )
    if not named:
        return None
    provided = params or {}
    missing = []
    for name in named:
        if name not in provided and name not in missing:
            missing.append(name)
    if missing:
        raise ValueError("缺少查询引用的参数：%s" % "、".join(missing))
    return provided


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


class _DuplicateJsonObjectKey(Exception):
    """object_pairs_hook 内部信号：JSON 对象中出现重复键。"""


def _reject_duplicate_json_keys(pairs):
    """json.loads 的 object_pairs_hook：重复键直接拒绝，否则还原为普通字典。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKey(key)
        result[key] = value
    return result


def _json_value_kind(value):
    """参数文件值的 JSON 类型中文说明，用于非字符串值的拒绝信息。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, int) or isinstance(value, float):
        return "数字"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "嵌套对象"
    return type(value).__name__


def read_params_file(path):
    """读取 --params-file 参数文件，返回 名称->文本 字典；任何不合法均抛 ValueError。

    文件按 UTF-8 解码，允许开头恰一个 BOM（与 read_sql_file 同一约定）；
    文件只被读取，绝不改写。内容必须恰为一个 JSON 对象：

    - 顶层不是对象（数组、数字、布尔、null、字符串等）一律拒绝；
    - 键遵循与 --param 相同的参数名规则（区分大小写），非法键名拒绝；
    - 值必须全部为 JSON 字符串，数字、布尔值、null、数组与嵌套对象拒绝；
      空对象与空字符串值有效，字符串解码后的中文、首尾空格、换行、等号、
      引号和分号原样保留，不做任何类型推断；
    - 对象中的重复键拒绝（在 JSON 解析阶段即失败，不会留到合并阶段）。

    文件不存在、路径是目录、不可读、编码或 JSON 语法错误同样抛
    ValueError，由调用方按统一的“错误: …”失败约定处理。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise ValueError("参数文件不存在：%s" % path)
    except IsADirectoryError:
        raise ValueError("参数文件路径是目录而非文件：%s" % path)
    except OSError as exc:
        raise ValueError("无法读取参数文件 %s：%s" % (path, exc))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("参数文件不是有效的 UTF-8 文本 %s：%s" % (path, exc))
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJsonObjectKey as exc:
        raise ValueError("参数文件 %s 中存在重复的键：%r" % (path, exc.args[0]))
    except json.JSONDecodeError as exc:
        raise ValueError("参数文件 %s 不是合法的 JSON：%s" % (path, exc))
    if not isinstance(obj, dict):
        raise ValueError(
            "参数文件 %s 的内容必须是一个 JSON 对象，收到 %s"
            % (path, _json_value_kind(obj))
        )
    params = {}
    for name, value in obj.items():
        try:
            validate_param_name(name)
        except ValueError as exc:
            raise ValueError("参数文件 %s 中的参数名非法：%s" % (path, exc))
        if not isinstance(value, str):
            raise ValueError(
                "参数文件 %s 中参数 %r 的值必须是 JSON 字符串，收到 %s"
                % (path, name, _json_value_kind(value))
            )
        params[name] = value
    return params


def merge_params(file_params, cli_params):
    """合并文件参数与命令行参数，同名时命令行值覆盖文件值。

    与选项在命令行上的先后顺序无关：先放入文件中的全部条目，再以命令行
    --param 条目整体覆盖，因此命令行空字符串也能覆盖文件中的非空值。
    两边都为空（None 或空字典）时返回 None，与未提供任何参数的既有调用
    形态保持一致；否则返回合并后的新字典，不修改传入的两个字典。
    """
    merged = dict(file_params or {})
    if cli_params:
        merged.update(cli_params)
    return merged or None


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


def _prepare_statement(sql_text, params, null_text):
    """汇集三个公开入口共用的输入校验，返回 (可执行语句文本, 绑定字典或 None)。

    空值标记类型、参数字典、SQL 文本与占位符绑定这四步规则原先在
    export_csv/export_html 与 preview_csv 中各维护一份，现集中在此：
    null_text 必须是字符串；params 为 None 或 名称->文本 的合法字典，
    值一律按文本绑定；SQL 必须恰为一条 SELECT；位置占位符与缺少查询
    引用的参数拒绝，未被引用的合法参数忽略。任一失败抛 ValueError，
    报告顺序固定为 空值标记类型、参数字典、SQL、占位符。各入口的专属
    校验（预览行数、HTML 说明类型）与文件导出的输出目标预查不在此列，
    由调用方按各自既有顺序安排在本函数之前或之后。
    """
    if not isinstance(null_text, str):
        raise ValueError(
            "null_text 必须是字符串，收到 %s" % type(null_text).__name__
        )
    params = validate_params(params)
    statement = validate_single_select(sql_text)
    bound = bind_params(statement, params)
    return statement, bound


def _ensure_output_available(output_path):
    """文件导出的目标预查：输出目录必须存在、目标必须尚未被占用。

    在打开源库之前完成：目录缺失或目标已存在时绝不接触源库，也不会
    打开输出文件；既存文件的原字节随后另由独占新建写入保证不被覆盖。
    """
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(output_dir):
        raise ValueError("输出目录不存在：%s" % output_dir)
    if os.path.exists(output_path):
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)


def _run_readonly(db_path, statement, bound):
    """以只读方式打开源库执行已校验语句并完整取回结果，返回 (列名列表, 行列表)。

    export_csv、export_html 与 preview_csv 共用同一份"打开只读连接 →
    执行并完整求值 → 关闭连接"流程：无论成功或失败，返回前连接都已
    释放。源库缺失或损坏在打开阶段抛 ValueError（只读 URI 绝不会创建
    新库）；execute 与 fetchall 阶段的 sqlite3.Error 由 _fetch_rows
    统一归类为 ValueError。调用方保证在本函数成功返回前尚未产生任何
    输出，因此部分结果即使已可用也不会被当作成功写出。
    """
    conn = open_readonly(db_path)
    try:
        return _fetch_rows(conn, statement, bound)
    finally:
        conn.close()


def _fetch_rows(conn, statement, bound):
    """在已打开的只读连接上执行语句并取回全部结果，返回 (列名列表, 数据行列表)。

    结果可能惰性求值：即便 execute 成功，fetchall 期间仍可能因求值
    （如整数溢出）抛出 sqlite3.Error。与开始执行阶段统一归类为
    ValueError，调用方无需按错误时点分别处理两种异常；调用方保证在
    失败时尚未产生任何输出，即使部分结果已可用也不会被当作成功写出。
    """
    cursor = conn.cursor()
    try:
        if bound is None:
            cursor.execute(statement)
        else:
            cursor.execute(statement, bound)
    except sqlite3.Error as exc:
        raise ValueError("SQL 执行失败：%s" % exc)
    if cursor.description is None:
        raise ValueError("不支持的语句：仅允许返回结果集的 SELECT")
    headers = [desc[0] for desc in cursor.description]
    try:
        rows = cursor.fetchall()
    except sqlite3.Error as exc:
        raise ValueError("SQL 执行失败：%s" % exc)
    return headers, rows


def _write_output_file(output_path, write_content):
    """以独占新建方式打开输出目标并交给 write_content 写入，统一失败约定。

    "x" = 独占新建：目标已存在则直接失败，从根本上杜绝覆盖或截断。
    写入过程中的 OSError、UnicodeError、TypeError 一律转为 ValueError，
    并清理本次新建的半成品文件；既存文件不可能被触及。write_content
    接收已打开的文本文件对象，负责格式相关的内容写出。
    """
    try:
        with open(output_path, "x", encoding="utf-8", newline="") as f:
            write_content(f)
    except FileExistsError:
        raise ValueError("输出目标已存在，拒绝覆盖：%s" % output_path)
    except (OSError, UnicodeError, TypeError) as exc:
        # 到此的文件必为本调用刚创建的半成品，清理后报错；既存文件不可能被触及
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise ValueError("无法写入输出文件 %s：%s" % (output_path, exc))


def _csv_records(headers, rows, null_text):
    """把查询结果展开为 CSV 逻辑记录序列：首条为列名，其后每行数据一条记录。

    只有 NULL 替换为 null_text；空字符串、零值及与标记同形的普通文本
    原样保留，列名、列顺序与查询返回的行顺序不变。export_csv 与
    preview_csv 共用这一份表达规则，保证两条入口对同一查询输出一致。
    """
    yield list(headers)
    for row in rows:
        yield [null_text if value is None else value for value in row]


def _write_csv_records(fileobj, headers, rows, null_text):
    """按共用 CSV 规则把表头与数据记录写入已打开的文本流（标准 csv 方言）。"""
    csv.writer(fileobj).writerows(_csv_records(headers, rows, null_text))


def export_csv(db_path, sql_text, output_path, params=None, null_text=""):
    """执行查询并将结果独占写入目标 CSV，返回数据行数。任何拒绝路径都不建文件。

    params 为可选的 名称->文本 参数字典（键不带占位符前缀），为查询中的
    :name、@name、$name 命名参数提供值；省略或传入 None 时与不提供参数
    的原有调用行为完全一致。值只作为绑定数据参与查询，绝不拼进 SQL 文本。

    null_text 为可选的 SQL NULL 导出标记：数据单元格为 NULL 时写入该文本，
    默认空字符串（即空字段，与原行为一致）。必须是字符串，非字符串值
    （None、数字等）抛 ValueError 且不创建输出文件。空字符串是有效标记；
    标记不影响列名、列顺序、行顺序、非空值与数据行数，源数据中的空字符串
    仍写为空字段。
    """
    # 三个入口共用同一份查询准备：空值标记类型、参数字典、SQL 与占位符
    statement, bound = _prepare_statement(sql_text, params, null_text)
    # 输入校验通过后先预查输出目录与目标占用，再以只读方式打开源库
    _ensure_output_available(output_path)
    headers, rows = _run_readonly(db_path, statement, bound)

    _write_output_file(
        output_path, lambda f: _write_csv_records(f, headers, rows, null_text)
    )

    return len(rows)


def preview_csv(db_path, sql_text, limit, params=None, null_text=""):
    """执行查询并把前 limit 行以带列名的 CSV 写到标准输出，返回写出的数据行数。

    与 export_csv、export_html 共用全部输入校验与只读取数流程（空值
    标记类型、参数字典、SQL 文本、占位符绑定、源库打开、查询执行与结果
    求值），任何失败都抛 ValueError 且此时标准输出尚未写入任何内容，
    不会留下部分预览。预览不创建报告或临时文件，源库与查询文件保持
    不变。

    limit 为正整数：表头始终输出，数据行最多 limit 条；结果不足 limit
    条时全部显示，零行结果只显示表头。列名、列顺序与查询返回的行顺序
    保持一致，查询的筛选、排序和 LIMIT 语义原样保留。SQL NULL 写
    null_text（默认空字段），非空值不额外替换；中文、逗号、引号及换行
    由 _write_csv_records 按与文件导出完全相同的 CSV 规则写出。
    """
    # 行数是预览入口的专属校验，仍最先拒绝；之后共用同一份查询准备
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("preview 行数必须是正整数，收到 %r" % (limit,))
    statement, bound = _prepare_statement(sql_text, params, null_text)
    headers, rows = _run_readonly(db_path, statement, bound)

    # 全部结果求值成功后才开始写标准输出：失败路径不会留下部分预览
    shown = rows[:limit]
    _write_csv_records(sys.stdout, headers, shown, null_text)
    return len(shown)


def render_html(headers, rows, null_text, description="", title="查询报告"):
    """把查询结果渲染为独立 HTML 页面文本（UTF-8，不依赖外部资源）。

    列名与单元格文本中的 &、<、>、双引号、单引号一律转义为字符引用，
    只作为文字显示；NULL 单元格显示 null_text，其余值按 str 转为文本。

    description 为非空字符串时，在主标题之后、结果表格之前输出一个独立
    文本区域：说明同样按文字转义，并以 white-space:pre-wrap 保留中文、
    首尾空格、连续空格与换行；空字符串（默认）不输出该区域，页面与未
    提供说明时逐字节一致。

    title 同时用作浏览器页面标题与表格前的主标题，默认"查询报告"。标题
    先去除首尾空白（中文与内部空白保留），再按文字转义，不产生额外标签
    或脚本；不进入表头、数据行或说明区域。默认标题不含 HTML 特殊字符，
    省略标题时页面与既往版本逐字节一致。
    """
    title_text = html.escape(title.strip())
    lines = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>%s</title>" % title_text,
        "<style>",
        "table{border-collapse:collapse}",
        "th,td{border:1px solid #999;padding:4px 8px;"
        "white-space:pre-wrap;text-align:left;vertical-align:top}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>%s</h1>" % title_text,
    ]
    if description != "":
        # 独立文本区域：内联 pre-wrap 样式，使省略说明时样式表与页面其余
        # 部分保持原有字节；说明只作为文字，转义后不产生标签或脚本
        lines.append(
            '<p style="white-space:pre-wrap">%s</p>' % html.escape(description)
        )
    lines += [
        "<table>",
        "<thead>",
        "<tr>" + "".join("<th>%s</th>" % html.escape(h) for h in headers) + "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for row in rows:
        cells = "".join(
            "<td>%s</td>" % html.escape(null_text if value is None else str(value))
            for value in row
        )
        lines.append("<tr>" + cells + "</tr>")
    lines += ["</tbody>", "</table>", "</body>", "</html>", ""]
    return "\n".join(lines)


def export_html(db_path, sql_text, output_path, params=None, null_text="",
                description="", title="查询报告"):
    """执行查询并将结果独占写入目标 HTML 报告，返回数据行数（不含表头）。

    输入与可选参数和 export_csv 完全相同，拒绝路径（源库缺失或无效、SQL
    或参数不合法、查询执行或求值失败、输出目录不存在、目标已存在）同样
    抛 ValueError 且不创建文件；写入失败清理本次新建的半成品后抛
    ValueError，既有文件原字节不变。

    输出为独立 UTF-8 页面：声明字符编码，标题"查询报告"，一张表格，
    表头 th、数据 td，列名、列顺序、行顺序与查询结果一致；零行结果仍
    保留表头并返回 0。SQL NULL 显示 null_text（默认空单元格）；空字符串
    和与标记同形的普通文本不额外转换，其他值按 str 转为文本。文本中的
    HTML 特殊字符转义为文字显示，不产生额外标签或脚本。

    description 为可选的纯文本查询说明：非空时显示在主标题之后、结果
    表格之前的独立文本区域，不进入表头、数据行、浏览器标题或 SQL，也
    不改变数据行数；说明中的中文、首尾空格、连续空格与换行完整保留，
    HTML 特殊字符按文字转义。必须是字符串，非字符串值抛 ValueError 且
    不创建输出文件；省略或传入空字符串时，输出与未提供说明逐字节一致。

    title 为可选的报告标题：同时用作浏览器页面标题与表格前的主标题，
    默认"查询报告"。标题先去除首尾空白，中文与内部空白保留，HTML 特殊
    字符按文字转义；不进入 SQL、查询说明、表头或数据行，也不改变返回
    的数据行数、列名、列顺序与行顺序。必须是字符串且去除首尾空白后非空，
    否则抛 ValueError 且不创建输出文件；省略标题或显式传入默认标题时，
    输出与未提供标题逐字节一致。
    """
    # 说明与标题的类型/空标题校验是 HTML 入口的专属校验，仍最先报告；
    # 之后与另外两个入口共用同一份查询准备（空值标记类型、参数字典、
    # SQL、占位符）
    if not isinstance(description, str):
        raise ValueError(
            "description 必须是字符串，收到 %s" % type(description).__name__
        )
    if not isinstance(title, str):
        raise ValueError(
            "title 必须是字符串，收到 %s" % type(title).__name__
        )
    title = title.strip()
    if title == "":
        raise ValueError("title 去除首尾空白后不能为空")
    statement, bound = _prepare_statement(sql_text, params, null_text)
    # 输入校验通过后先预查输出目录与目标占用，再以只读方式打开源库
    _ensure_output_available(output_path)
    headers, rows = _run_readonly(db_path, statement, bound)
    page = render_html(headers, rows, null_text, description, title)

    _write_output_file(output_path, lambda f: f.write(page))

    return len(rows)


# --describe 模式输出的固定表头；列顺序与 PRAGMA table_info 的字段一致
DESCRIBE_HEADER = ("cid", "name", "type", "notnull", "dflt_value", "pk")


def describe_table(db_path, table_name):
    """只读读取单张用户表的声明列结构，以带表头的 CSV 写到标准输出。

    返回写出的声明列数据行数（不含表头）。table_name 按保存名称精确
    匹配：作为绑定参数提供，绝不拼入 SQL 文本；区分大小写（对
    sqlite_master.name 显式使用 BINARY 排序规则）、不去除首尾空白，
    中文、空格与引号按完整文本查找。只有 sqlite_master 中 type 为
    "table" 的对象可查看；不存在的名字、视图（PRAGMA 对视图同样返回
    列信息，须在此排除）以及 sqlite_ 开头的内部表（如 sqlite_sequence
    在 sqlite_master 中同样登记为 table）统一按"未找到可查看的用户
    表"拒绝。

    列信息来自表值 PRAGMA pragma_table_info，按 cid 升序：cid 从 0
    开始；notnull 与 pk 保留 SQLite 返回的整数，不因主键身份自行改写；
    type 为空串时输出空字段（无声明类型）；dflt_value 为 None 时输出
    空字段（无默认值），显式 DEFAULT NULL 在元数据中即为文本 "NULL"，
    其他默认表达式原样保留、不求值。源库缺失或损坏时由 open_readonly
    抛 ValueError 且绝不创建新库；任何失败都在写入标准输出之前抛出，
    不会留下半截 CSV。本模式只读取元数据，不创建任何文件，源库结构
    与数据保持不变。
    """
    if not isinstance(table_name, str):
        raise ValueError(
            "describe 表名必须是字符串，收到 %s" % type(table_name).__name__
        )
    conn = open_readonly(db_path)
    try:
        # 显式 COLLATE BINARY：SQLite 标识符默认 NOCASE 比较，精确匹配
        # 要求 "Notes" 与 "notes" 不等；空白与中文按字符原值比较
        row = conn.execute(
            "SELECT type FROM sqlite_master "
            "WHERE name = ? COLLATE BINARY AND type = 'table'",
            (table_name,),
        ).fetchone()
        if row is None or table_name.startswith("sqlite_"):
            raise ValueError("未找到可查看的用户表：%s" % table_name)
        try:
            columns = conn.execute(
                'SELECT cid, name, type, "notnull", dflt_value, pk '
                'FROM pragma_table_info(?, "main") ORDER BY cid',
                (table_name,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("元数据读取失败：%s" % exc)
    finally:
        conn.close()

    records = [list(DESCRIBE_HEADER)]
    for cid, name, ctype, notnull, dflt, pk in columns:
        # type 为空串（无声明类型）与 dflt 为 None（无默认值）都输出空
        # 字段；显式 DEFAULT NULL 在元数据中是文本 "NULL"，原样保留
        records.append([
            cid, name, ctype or "", notnull,
            dflt if dflt is not None else "", pk,
        ])

    # 全部元数据取回成功后才写标准输出：失败路径不会产生半截 CSV。
    # 复用现有标准 csv 方言，中文、空格、逗号与引号的处理与查询导出一致
    writer = csv.writer(sys.stdout)
    writer.writerows(records)
    return len(columns)


# --tables 模式输出的固定单列表头
TABLES_HEADER = ("name",)


def list_tables(db_path):
    """只读列出可查看的用户表名，以带表头的单列 CSV 写到标准输出。

    返回写出的表名记录数（不含表头）。列举范围与 describe_table 可查看
    的对象完全一致：仅主库 sqlite_master 中 type 为 "table" 且保存名称
    不以 "sqlite_" 开头的对象；视图、索引、触发器与 sqlite_sequence
    （AUTOINCREMENT 自动生成、同样登记为 table）都不出现。

    结果按保存名称的 SQLite BINARY 顺序升序：区分大小写、不按创建顺序、
    不去除首尾空白；中文、空格、逗号、双引号与换行按原始名称保留，经
    CSV 解析后可得到完整名称，并可直接交给 describe_table 按现有精确
    匹配语义使用。没有可列举的表时仍输出 name 表头并返回 0。本模式只
    读取 sqlite_master 元数据、不读取表内数据，不输出行数或列信息；
    源库以只读连接打开且保持不变，缺失或损坏由 open_readonly 抛
    ValueError 且绝不创建新库；任何失败都在写入标准输出之前抛出，不会
    留下半截 CSV。
    """
    conn = open_readonly(db_path)
    try:
        try:
            # 只取主库 sqlite_master 中登记为 table 的对象，并按名称的
            # BINARY 排序规则升序（区分大小写、按字节序、不按创建顺序）。
            # sqlite_ 开头的内部表在 Python 侧按区分大小写的前缀排除，
            # 与 describe_table 的 startswith("sqlite_") 判定保持一致
            # （LIKE 对 ASCII 大小写不敏感，不能用于此前缀判定）
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "ORDER BY name COLLATE BINARY"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("元数据读取失败：%s" % exc)
    finally:
        conn.close()

    names = [name for (name,) in rows if not name.startswith("sqlite_")]

    records = [list(TABLES_HEADER)]
    records.extend([name] for name in names)

    # 全部元数据取回成功后才写标准输出：失败路径不会产生半截 CSV。
    # 复用现有标准 csv 方言，名称中的中文、空格、逗号、双引号与换行的
    # 处理与查询导出及 --describe 完全一致
    csv.writer(sys.stdout).writerows(records)
    return len(names)


class _Parser(argparse.ArgumentParser):
    """参数用法错误也按本工具的约定使用退出码 1（argparse 默认是 2）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        die("参数错误：%s" % message)


def parse_param_options(items, parser):
    """把 --param name=value 列表解析为参数字典；非法输入经 parser.error 拒绝。

    在第一个等号处分开名称与值：值的其余等号、空格、中文、引号和分号
    原样保留；空字符串是有效值。同名选项重复提供即拒绝。
    """
    if not items:
        return None
    params = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            parser.error("--param 需要 name=value 形式（缺少等号）：%r" % item)
        try:
            validate_param_name(name)
        except ValueError as exc:
            parser.error(str(exc))
        if name in params:
            parser.error("重复提供同名参数：%r" % name)
        params[name] = value
    return params


def parse_args(argv):
    parser = _Parser(
        description="对 SQLite 执行一条只读 SELECT 并导出带列名的 CSV 或 HTML 报告"
    )
    parser.add_argument("--db", required=True, help="已有 SQLite 数据库文件路径")
    parser.add_argument(
        "--describe",
        metavar="NAME",
        default=None,
        help="只读查看单张用户表的列结构：CSV 写到标准输出，不需要 SQL 或输出路径；"
        "仅与 --db 搭配，不与其他任何选项同用",
    )
    parser.add_argument(
        "--tables",
        action="store_true",
        default=False,
        help="只读列出可查看的用户表名：单列 CSV（表头 name）写到标准输出，"
        "不需要 SQL 或输出路径；仅与 --db 搭配，不与其他任何选项同用",
    )
    parser.add_argument("--sql", help="一条 SELECT 查询文本")
    parser.add_argument(
        "--sql-file", help="包含一条 SELECT 查询的 UTF-8 文件路径（允许一个 BOM）"
    )
    parser.add_argument("--output", help="输出文件路径（不得已存在）；预览模式下不可提供")
    parser.add_argument(
        "--preview",
        type=int,
        metavar="N",
        default=None,
        help="在终端预览前 N 行（正整数）：CSV 写到标准输出，不创建输出文件",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "html"],
        default=None,
        help="输出格式：csv（默认）或 html；输出类型只由该选项决定",
    )
    parser.add_argument(
        "--param",
        action="append",
        metavar="name=value",
        help="查询命名参数的文本值，可重复；值在第一个等号后原样保留",
    )
    parser.add_argument(
        "--params-file",
        action="append",
        metavar="PATH",
        help="包含一个 JSON 对象的 UTF-8 参数文件路径（允许一个 BOM）："
        "键为参数名、值全部为字符串；只能提供一次，与 --param 合并，"
        "同名时命令行 --param 覆盖文件值",
    )
    parser.add_argument(
        "--null-text",
        metavar="MARKER",
        default=None,
        help="SQL NULL 在输出中的导出标记，默认为空；标记原样写入，可含空格、逗号、引号与换行",
    )
    parser.add_argument(
        "--description",
        metavar="TEXT",
        default=None,
        help="HTML 报告主标题后的纯文本查询说明；仅与 --format html 搭配使用",
    )
    parser.add_argument(
        "--title",
        metavar="TEXT",
        default=None,
        help="HTML 报告的自定义标题（浏览器标题与主标题，默认“查询报告”）；"
        "仅与 --format html 搭配使用",
    )
    args = parser.parse_args(argv)
    if args.tables:
        # 表名列举是独立模式：除帮助外仅接受 --db 与 --tables，
        # 与 --describe 及其他任何现有选项（含显式 --format csv）同用均
        # 按参数错误拒绝；此判定先于读文件与开库，混用时绝不接触它们
        extras = []
        if args.describe is not None:
            extras.append("--describe")
        if args.sql is not None:
            extras.append("--sql")
        if args.sql_file is not None:
            extras.append("--sql-file")
        if args.output is not None:
            extras.append("--output")
        if args.preview is not None:
            extras.append("--preview")
        if args.format is not None:
            extras.append("--format")
        if args.param:
            extras.append("--param")
        if args.params_file:
            extras.append("--params-file")
        if args.null_text is not None:
            extras.append("--null-text")
        if args.description is not None:
            extras.append("--description")
        if args.title is not None:
            extras.append("--title")
        if extras:
            parser.error(
                "--tables 仅与 --db 搭配，不能与其他选项同用：%s"
                % " ".join(extras)
            )
        return args
    if args.describe is not None:
        # 表结构查看是独立模式：除帮助外仅接受 --db 与 --describe，
        # 与其他任何现有选项同用均按参数错误拒绝（此判定先于模式各自
        # 的必填校验，缺参与混用并存时一律报混用）
        extras = []
        if args.sql is not None:
            extras.append("--sql")
        if args.sql_file is not None:
            extras.append("--sql-file")
        if args.output is not None:
            extras.append("--output")
        if args.preview is not None:
            extras.append("--preview")
        if args.format is not None:
            extras.append("--format")
        if args.param:
            extras.append("--param")
        if args.params_file:
            extras.append("--params-file")
        if args.null_text is not None:
            extras.append("--null-text")
        if args.description is not None:
            extras.append("--description")
        if args.title is not None:
            extras.append("--title")
        if extras:
            parser.error(
                "--describe 仅与 --db 搭配，不能与其他选项同用：%s"
                % " ".join(extras)
            )
        if args.describe == "":
            parser.error("--describe 表名不能为空")
        return args
    # 查询/导出/预览模式：未显式提供 --format 与 --null-text 时使用默认值；
    # describe 模式下二者保持 None，main 不会触及这些属性
    if args.format is None:
        args.format = "csv"
    if args.null_text is None:
        args.null_text = ""
    # 恰好选择一个查询来源；此判定发生在读文件与开库之前
    if (args.sql is None) == (args.sql_file is None):
        parser.error("--sql 与 --sql-file 必须恰好选择一个")
    if args.preview is not None:
        # 预览模式：只写标准输出，与导出选项互斥
        if args.preview < 1:
            parser.error("--preview 需要正整数，收到 %d" % args.preview)
        if args.output is not None:
            parser.error("--preview 与 --output 不能同时使用：预览不创建输出文件")
        if args.format != "csv":
            parser.error("--preview 仅支持 CSV 格式，不接受 --format html")
        if args.description is not None:
            parser.error("--preview 不支持 --description：预览不包含查询说明文本")
        if args.title is not None:
            parser.error("--preview 不支持 --title：预览不包含报告标题")
    elif args.output is None:
        parser.error("缺少 --output：导出模式必须提供输出文件路径")
    # 显式提供 --description（即使为空）时只允许 HTML 格式；
    # 以 None 区分"未提供"与"提供空字符串"
    if args.description is not None and args.format != "html":
        parser.error(
            "--description 仅支持 --format html：CSV 报告不包含查询说明文本"
        )
    # 显式提供 --title 时只允许 HTML 格式；空白标题（去除首尾空白后为空）
    # 同样拒绝，与 export_html 的函数级校验一致
    if args.title is not None:
        if args.format != "html":
            parser.error(
                "--title 仅支持 --format html：CSV 报告不包含自定义标题"
            )
        if args.title.strip() == "":
            parser.error("--title 的标题去除首尾空白后不能为空")
    args.params = parse_param_options(args.param, parser)
    # --params-file 的结构性规则在读取任何文件之前判定：只能提供一次，
    # 路径不能为空。重复提供或缺路径属参数错误，此时绝不读取参数文件。
    # 文件本身的读取与内容校验由 main 在读取查询文件、打开源库之前完成
    if args.params_file is not None:
        if len(args.params_file) > 1:
            parser.error("--params-file 只能提供一次，收到 %d 次" % len(args.params_file))
        if args.params_file[0] == "":
            parser.error("--params-file 路径不能为空")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.tables:
        # 表名列举：独立模式，标准输出只写 CSV，不读取 SQL、不建文件、
        # 成功时不追加任何提示；失败原因由 list_tables 以 ValueError 给出
        try:
            list_tables(args.db)
        except ValueError as exc:
            die(str(exc))
        except sqlite3.Error as exc:
            die("数据库错误：%s" % exc)
        except OSError as exc:
            die("文件错误：%s" % exc)
        return 0
    if args.describe is not None:
        # 表结构查看：独立模式，标准输出只写 CSV，不读取 SQL、不建文件、
        # 成功时不追加任何提示；失败原因由 describe_table 以 ValueError 给出
        try:
            describe_table(args.db, args.describe)
        except ValueError as exc:
            die(str(exc))
        except sqlite3.Error as exc:
            die("数据库错误：%s" % exc)
        except OSError as exc:
            die("文件错误：%s" % exc)
        return 0
    # 参数文件的读取与结构/内容校验最先完成：先于读取查询文件、打开源库
    # 与创建报告，失败时不读取查询文件、不接触源库、不创建任何文件。
    # 文件参数与命令行 --param 合并后再按既有规则绑定：同名时命令行值
    # 覆盖文件值（含命令行空字符串覆盖非空文件值），与选项顺序无关
    file_params = None
    if args.params_file is not None:
        try:
            file_params = read_params_file(args.params_file[0])
        except ValueError as exc:
            die(str(exc))
    args.params = merge_params(file_params, args.params)
    sql_text = args.sql
    if sql_text is None:
        # 再读查询文件：文件类失败时不接触源库与输出目标
        try:
            sql_text = read_sql_file(args.sql_file)
        except ValueError as exc:
            die(str(exc))
    try:
        if args.preview is not None:
            # 预览：标准输出只写 CSV 本身，成功时不追加任何提示
            preview_csv(
                args.db,
                sql_text,
                args.preview,
                params=args.params,
                null_text=args.null_text,
            )
            return 0
        if args.format == "csv":
            row_count = export_csv(
                args.db,
                sql_text,
                args.output,
                params=args.params,
                null_text=args.null_text,
            )
        else:
            row_count = export_html(
                args.db,
                sql_text,
                args.output,
                params=args.params,
                null_text=args.null_text,
                # 未提供 --description 时为 None，与空字符串同样不输出说明区域
                description=args.description or "",
                # 未提供 --title 时为 None，由 export_html 使用默认“查询报告”
                title=args.title if args.title is not None else "查询报告",
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
