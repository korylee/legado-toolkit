# -*- coding: utf-8 -*-
"""TODO.md 的结构闸门：五档状态 + 四段分区 + 必填字段 + P0 上限。

由来：旧 TODO.md 把「已完成 / 待办 / 机制 / 决策记录」混在同一个散文流里，
优先级与依赖全靠散文（「受 AGENTS #3 钉着」、「未评估前不要动手」），于是
**没有任何机检能判断它是不是过期了**——做完的条目还留着，下次翻到会以为没做
（AGENTS #10）。这里把结构钉死，让上面那条规矩可执行。

结构约定（**唯一事实来源**，改结构先改这里，再改 TODO.md）：

- 条目：`### 条目：<ID> · <标题>`，ID 取 `[A-Za-z0-9][A-Za-z0-9._-]*`。
- 字段：**行首锚定**（`^状态：` ……），值可续行，续行必须缩进 2 空格；
  下一个字段 / 条目标题 / EOF 终止它。**不用表格**：表格列受中文、竖线、换行
  影响，切不出稳定的「条目 -> 字段」对；行首锚点可以。
- 必填：状态 / 依赖 / 优先级 / 背景 / 约束 / 验收 / 指针。
  `open` 另须 `子项：`（子项行行首 `- `）；`blocked` 另须 `阻塞于：`。
- 四段分区 + 已完成区的状态约束见 `SECTIONS`。

四条检查各自独立（一个坏结构只报一条），失败文案要能直接指到位置。
"""

from __future__ import annotations

import pathlib
import re
import unittest

TODO = pathlib.Path(__file__).resolve().parent.parent / "TODO.md"

#: 条目切分：ID 与标题都必须是「非空且合法」的，否则整行不匹配。
ENTRY = re.compile(r"^### 条目：(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\s+·\s+(?P<title>.+?)\s*$")

#: 字段名：行首锚定的字段行。值可以续行（缩进 2 空格）。
FIELDS = ("状态", "依赖", "优先级", "背景", "约束", "验收", "指针", "子项", "阻塞于")
REQUIRED = ("状态", "依赖", "优先级", "背景", "约束", "验收", "指针")
CONDITIONAL = {"open": "子项", "blocked": "阻塞于"}

STATUSES = ("open", "todo", "blocked", "doing", "done")
PRIORITIES = ("P0", "P1", "P2")

#: 分区 -> 该区允许的状态。键的顺序就是文件里的顺序。
SECTIONS = (
    ("## 0 · 现在做", ("todo", "doing", "blocked", "open")),
    ("## 1 · 排队", ("todo", "doing", "blocked")),
    ("## 2 · 按需", ("todo", "doing", "blocked", "open")),
    ("## 3 · 待决策", ("open", "blocked", "todo")),
    ("## 已完成", ("done",)),
)

#: P0 闸门：只数「现在做」区里的活跃条目（父 open 与 done 都不算）。
P0_LIMIT = 3
P0_STATUSES = ("todo", "doing", "blocked")

FIELD_RE = {name: re.compile(r"^%s：(?P<value>.*)$" % re.escape(name)) for name in FIELDS}

def parse(text):
    """把 TODO.md 切成 [{section, id, title, line, fields, bad_id}]。

    bad_id 记「以 `### 条目：` 开头但整行不匹配 ENTRY」的行——那种行必须被后续
    检查看见，不能静默当成普通正文（否则一条坏条目等于隐身）。
    """
    section = None
    entries = []
    current = None
    active = None

    def close():
        if current is not None:
            entries.append(current)

    for num, raw in enumerate(text.split(chr(10)), 1):
        line = raw.rstrip()
        if line.startswith("## "):
            close()
            current = None
            active = None
            section = line
            continue
        if line.startswith("### 条目："):
            close()
            active = None
            hit = ENTRY.match(line)
            if hit is None:
                current = {"section": section, "id": None, "title": None,
                           "line": num, "fields": {}, "field_lines": {},
                           "bad_id": True}
            else:
                current = {"section": section, "id": hit.group("id"),
                           "title": hit.group("title"), "line": num,
                           "fields": {}, "field_lines": {},
                           "bad_id": False}
            continue
        if current is None:
            continue
        for name in FIELDS:
            hit = FIELD_RE[name].match(line)
            if hit is not None:
                current["fields"].setdefault(name, []).append(hit.group("value"))
                current["field_lines"].setdefault(name, 0)
                current["field_lines"][name] += 1
                active = name
                break
        else:
            if active is None:
                continue
            if raw.startswith("  ") and raw.strip():
                current["fields"][active].append(raw.strip())
            elif active == "子项" and line.startswith("- "):
                current["fields"][active].append(line)
            else:
                active = None
    close()
    return entries


def field_value(entry, name):
    """字段值：每行去掉空白后拼回（续行靠缩进 2 空格表达）。"""
    chunks = entry["fields"].get(name)
    if not chunks:
        return None
    return chr(10).join(part.strip() for part in chunks)

def check_fields(text):
    """检查 1：条目锚点 + 字段锚点。缺字段 / 重复字段 / 坏标题都报。"""
    problems = []
    for entry in parse(text):
        where = "TODO.md:%d" % entry["line"]
        if entry["bad_id"]:
            problems.append("%s TODO 条目标题不合法（应为 `### 条目：<ID> · <标题>`）" % where)
            continue
        for name in REQUIRED:
            if name not in entry["fields"]:
                problems.append("%s 条目 %s TODO 条目缺少字段：%s" % (where, entry["id"], name))
        for name, hits in entry["field_lines"].items():
            if hits > 1:
                problems.append("%s 条目 %s TODO 条目重复字段：%s" % (where, entry["id"], name))
        status = (entry["fields"].get("状态") or [""])[0].strip()
        extra = CONDITIONAL.get(status)
        if extra and extra not in entry["fields"]:
            problems.append("%s 条目 %s TODO 条目缺少字段：%s（状态 %s）"
                            % (where, entry["id"], extra, status))
    return problems

def check_status_deps(text):
    """检查 2：状态取值 + 依赖形状 + `open` 的子项 / `blocked` 的阻塞于。"""
    problems = []
    for entry in parse(text):
        if entry["bad_id"]:
            continue
        where = "TODO.md:%d" % entry["line"]
        eid = entry["id"]
        status = field_value(entry, "状态")
        if status not in STATUSES:
            problems.append("%s 条目 %s 状态不合法：%r（应取 %s）"
                            % (where, eid, status, "|".join(STATUSES)))
            continue
        deps = field_value(entry, "依赖")
        if deps is None or not deps.strip():
            problems.append("%s 条目 %s 依赖为空（无依赖写 `无`）" % (where, eid))
        elif deps.strip() != "无":
            ids = [part.strip() for part in re.split(r"[,，]", deps) if part.strip()]
            if not ids:
                problems.append("%s 条目 %s 依赖写法不合法：%r" % (where, eid, deps))
            for bad in [i for i in ids if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", i)]:
                problems.append("%s 条目 %s 依赖里有非法 ID：%r" % (where, eid, bad))
        priority = field_value(entry, "优先级")
        if priority not in PRIORITIES:
            problems.append("%s 条目 %s 优先级不合法：%r（应取 %s）"
                            % (where, eid, priority, "|".join(PRIORITIES)))
        if status == "open":
            items = entry["fields"].get("子项")
            if not items or not any(line.startswith("- ") for line in items):
                problems.append("%s 条目 %s 状态 open 缺少子项：`子项：` 后必须有 `- ` 行"
                                % (where, eid))
        if status == "blocked" and not (field_value(entry, "阻塞于") or "").strip():
            problems.append("%s 条目 %s 状态 blocked 缺少阻塞于：" % (where, eid))
    return problems

def check_sections(text):
    """检查 3：每个条目必须落在分区里，且状态与该区允许的状态匹配。"""
    problems = []
    known = [name for name, _ in SECTIONS]
    allowed = dict(SECTIONS)
    for name in [line.rstrip() for line in text.split(chr(10)) if line.startswith("## ")]:
        if name not in known:
            problems.append("TODO.md 出现未定义分区：%s（应取 %s）" % (name, " / ".join(known)))
    for entry in parse(text):
        if entry["bad_id"]:
            continue
        where = "TODO.md:%d" % entry["line"]
        section = entry["section"]
        status = field_value(entry, "状态")
        if section not in allowed:
            problems.append("%s 条目 %s 不在任何分区内（当前区 %s）"
                            % (where, entry["id"], section))
            continue
        if status not in allowed[section]:
            problems.append("%s 条目 %s 状态 %s 不能出现在「%s」（该区只允许 %s）"
                            % (where, entry["id"], status, section,
                               "|".join(allowed[section])))
    return problems


def check_p0(text):
    """检查 4：`## 0 · 现在做` 区里活跃的 P0 条目最多 P0_LIMIT 条。"""
    head = SECTIONS[0][0]
    count, offenders = 0, []
    for entry in parse(text):
        if entry["bad_id"] or entry["section"] != head:
            continue
        status = field_value(entry, "状态")
        if status not in P0_STATUSES:
            continue
        if field_value(entry, "优先级") == "P0":
            count += 1
            offenders.append(entry["id"])
    if count > P0_LIMIT:
        return ["TODO.md 0 区 P0 活跃条目 %d 条，超过上限 %d 条：%s"
                % (count, P0_LIMIT, ", ".join(offenders))]
    return []


# ---------------------------------------------------------------
# 对真实 TODO.md 的闸门
# ---------------------------------------------------------------

class TodoStructureTests(unittest.TestCase):
    def setUp(self):
        self.text = TODO.read_text(encoding="utf-8")

    def test_entries_have_all_fields(self):
        problems = check_fields(self.text)
        self.assertEqual([], problems, chr(10).join(problems))

    def test_status_dependencies_are_legal(self):
        problems = check_status_deps(self.text)
        self.assertEqual([], problems, chr(10).join(problems))

    def test_sections_match_statuses(self):
        problems = check_sections(self.text)
        self.assertEqual([], problems, chr(10).join(problems))

    def test_p0_within_limit(self):
        problems = check_p0(self.text)
        self.assertEqual([], problems, chr(10).join(problems))

    def test_every_entry_line_has_a_legal_id(self):
        """坏标题不能靠「解析不出来」隐身。"""
        self.assertEqual([], [e["line"] for e in parse(self.text) if e["bad_id"]])

    def test_ids_are_unique(self):
        seen, dupes = set(), []
        for entry in parse(self.text):
            if entry["bad_id"]:
                continue
            if entry["id"] in seen:
                dupes.append(entry["id"])
            seen.add(entry["id"])
        self.assertEqual([], dupes)

    def test_is_lf_only(self):
        """本仓除个别 Windows 脚本外全是 LF；TODO.md 尤其不能翻成 CRLF。"""
        self.assertEqual(0, TODO.read_bytes().count(b"\r\n"), "TODO.md 被翻成了 CRLF")

    def test_all_five_sections_are_present(self):
        seen = [l.rstrip() for l in self.text.split(chr(10)) if l.startswith("## ")]
        self.assertEqual([name for name, _ in SECTIONS], seen)


# ---------------------------------------------------------------
# 边界用例：只留「合成文本才能精确构造」的那几条。真实的 TODO.md
# 覆盖不到边界（恰好 3 条 P0、父 open 不计、done 不计），而这几条
# 正是闸门被绕过的入口。其余检查逻辑由上面的真文件用例覆盖。
# ---------------------------------------------------------------

_FIELD_ORDER = ("状态", "依赖", "优先级", "子项", "阻塞于", "背景", "约束", "验收", "指针")


def _entry(eid="A1", title="样例", section="## 1 · 排队", **fields):
    """拼一个最小条目文本（默认落在排队区、全字段齐全）。"""
    rows = [section, "", "### 条目：%s · %s" % (eid, title)]
    body = {"状态": "todo", "依赖": "无", "优先级": "P1",
            "背景": "背景", "约束": "约束", "验收": "验收", "指针": "lessons §一"}
    body.update(fields)
    for name in _FIELD_ORDER:
        if name in body and body[name] is not None:
            value = body[name]
            if isinstance(value, list):
                rows.append("%s：" % name)
                rows.extend(value)
            else:
                rows.append("%s：%s" % (name, value))
    return chr(10).join(rows) + chr(10)


class FormatContractTests(unittest.TestCase):
    """标题格式与 ENTRY 正则必须自洽——两者的 `·` 分隔是唯一约定。"""

    def test_good_entry_splits_into_id_and_title(self):
        hit = ENTRY.match("### 条目：L4-network · L4 请求快照补强")
        self.assertIsNotNone(hit)
        self.assertEqual("L4-network", hit.group("id"))
        self.assertEqual("L4 请求快照补强", hit.group("title"))

    def test_bad_entries_do_not_match(self):
        for line in ("### 条目：L4-network",         # 缺中点与标题
                     "### 条目：L4-network L4 补强",  # 缺中点
                     "### 条目：· 没有ID",            # 缺 ID
                     "### 条目：-bad · 标题",         # ID 首字符非法
                     "状态：todo"):                   # 字段行不是标题
            self.assertIsNone(ENTRY.match(line), line)


class PresenceBoundaryTests(unittest.TestCase):
    """真文件里全是「合规」形态（60 条都写了验收、没有 done 落在 0 区），
    所以上面那组用例抓不住「判据被放宽」——这两条补的就是那个缺口。"""

    def test_missing_required_field_is_reported(self):
        text = _entry().replace("验收：验收" + chr(10), "")
        self.assertTrue(any("缺少字段：验收" in p for p in check_fields(text)))

    def test_open_without_subitems_is_reported(self):
        """真文件那 2 个 open 条目都写了子项，所以这条要求只能合成着测。"""
        self.assertTrue(any("open 缺少子项" in p
                            for p in check_status_deps(_entry(状态="open"))))
        ok = _entry(状态="open", 子项=["- A2", "- A3"])
        self.assertEqual([], check_status_deps(ok))

    def test_blocked_without_blocker_is_reported(self):
        """同上：真文件那 4 个 blocked 都写了阻塞于。"""
        self.assertTrue(any("blocked 缺少阻塞于" in p
                            for p in check_status_deps(_entry(状态="blocked"))))

    def test_empty_dependency_is_reported(self):
        """真文件没有空依赖（缺依赖写 `无`），所以这条只能合成着测。"""
        self.assertTrue(any("依赖为空" in p for p in check_status_deps(_entry(依赖=""))))

    def test_priority_domain_is_three_values(self):
        problems = check_status_deps(_entry(优先级="P3"))
        self.assertTrue(any("优先级不合法" in p for p in problems), problems)

    def test_unknown_status_is_reported(self):
        problems = check_status_deps(_entry(状态="wip"))
        self.assertTrue(any("状态不合法" in p for p in problems), problems)

    def test_done_outside_the_done_section_is_reported(self):
        # 每个待办区都要各自拒 done：只测一个区的话，把某个区的允许集合
        # 放宽（例如 0 区混进 done）就漏过去了。
        for section in ("## 0 · 现在做", "## 1 · 排队", "## 2 · 按需", "## 3 · 待决策"):
            entry = _entry(状态="done", section=section)
            if section == "## 0 · 现在做":
                entry = entry.replace("优先级：P1", "优先级：P0")
            self.assertTrue(check_sections(entry), "%s 里的 done 必须报" % section)


class P0BoundaryTests(unittest.TestCase):
    """闸门的三个绕过口：恰好上限、父 open、done。"""

    def test_exactly_the_limit_passes(self):
        text = chr(10).join(_entry(eid="A%d" % i, 优先级="P0", section="## 0 · 现在做")
                            for i in (1, 2, 3))
        self.assertEqual([], check_p0(text))

    def test_over_the_limit_fails_and_names_the_offenders(self):
        text = chr(10).join(_entry(eid="A%d" % i, 优先级="P0", section="## 0 · 现在做")
                            for i in (1, 2, 3, 4))
        problems = check_p0(text)
        self.assertTrue(problems)
        self.assertIn("A4", problems[0])

    def test_parent_open_p0_is_not_counted(self):
        parent = _entry(eid="P", 状态="open", 优先级="P0", section="## 0 · 现在做",
                        子项=["- A1", "- A2", "- A3"])
        kids = chr(10).join(_entry(eid="A%d" % i, 优先级="P0", section="## 0 · 现在做")
                            for i in (1, 2, 3))
        self.assertEqual([], check_p0(parent + kids))

    def test_done_p0_is_not_counted(self):
        kids = chr(10).join(_entry(eid="A%d" % i, 优先级="P0", section="## 0 · 现在做")
                            for i in (1, 2, 3))
        finished = _entry(eid="Z", 状态="done", 优先级="P0", section="## 已完成")
        self.assertEqual([], check_p0(kids + finished))


if __name__ == "__main__":
    unittest.main()
