# -*- coding: utf-8 -*-
"""清洗书源 JSON：把 Legado 布尔/数字/对象字段的类型违规规范化，保证 Gson 可解析。

修复规则：
- customButton / eventListener: 期望 bool，list/None → False
- concurrentRate: 在 Legado 里是 **String**（BaseSource.kt:35），不是 int。
  两种写法都合法：「次数/毫秒」如 "1/2000"，或纯毫秒数如 "1000"。
  所以**只把纯数字归一成 int、非数字串原样保留**——不能按 int 字段处理，
  见下方 clean_source 里的注释
- lastUpdateTime: 期望 int，字符串 → int
- ruleExplore: 期望 dict，list → {}
- variableComment: 期望 str，list → ''
"""
import json
import sys

def clean_source(s):
    # bool 字段
    for f in ('customButton', 'eventListener', 'enabled', 'enabledCookieJar', 'enabledExplore'):
        v = s.get(f)
        if v is not None and not isinstance(v, bool):
            # 字符串**先归一大小写**再判。原来拿原值去比 ('', [], {}, 'false')，
            # 于是 `"False"` / `"0"` 落进 `bool()` 变成 **True**——开关被反向打开，
            # 而小写 `"false"` 恰好判对，纯属巧合。非 int / 非 str 一律 False
            # （bool 已被上面那道 `not isinstance(v, bool)` 挡掉，这里的 int 是真 int）
            if isinstance(v, int):
                s[f] = bool(v)
            elif isinstance(v, str):
                s[f] = v.strip().lower() not in ('', '0', 'false')
            else:
                s[f] = False
    # int 字段
    for f in ('customOrder', 'respondTime', 'weight'):
        v = s.get(f)
        if v is not None and not isinstance(v, int):
            if isinstance(v, str):
                try:
                    s[f] = int(float(v))
                except (ValueError, TypeError):
                    s[f] = 0
            else:
                s[f] = 0
    # concurrentRate 在 Legado 里是 **String**（BaseSource.kt:35），不是 int：
    # 它支持「次数/毫秒」（"1/2000"）与纯毫秒数（"1000"）两种写法
    # （ConcurrentRateLimiter.kt:82 的注释：「并发控制为 次数/毫秒，非并发实际为 1/毫秒」）。
    #
    # 曾经它被列在上面那个 int 字段清单里，于是 "1/2" 走 int(float("1/2")) 抛异常、
    # 被兜底成 **0**——0 在 App 里等于「不限速」。也就是说：我们不但不遵守用户的限速
    # 设置，还在导入/保存时把它**静默抹掉**，再导出回 App。这类破坏会连证据一起
    # 抹掉（存量的 "1/2" 已无法从库里反推有多少），所以宁可少动：
    # 纯数字仍归一成 int（保持既有导出形状），**其余字符串一律原样保留**。
    v = s.get('concurrentRate')
    if isinstance(v, bool):
        # bool 是 int 的子类，必须先判——否则 isinstance(v, int) 为真，
        # 一个 true 会被当成合法 int 原样放过（Gson 那边转不成 String）
        s['concurrentRate'] = 0
    elif v is not None and not isinstance(v, int):
        if isinstance(v, float):
            s['concurrentRate'] = int(v)
        elif isinstance(v, str):
            try:
                s['concurrentRate'] = int(float(v))
            except (ValueError, TypeError):
                pass          # "1/2" 这类写法是合法的，别碰
        else:
            s['concurrentRate'] = 0
    # lastUpdateTime int
    v = s.get('lastUpdateTime')
    if v is not None and not isinstance(v, int):
        try:
            s['lastUpdateTime'] = int(float(v))
        except (ValueError, TypeError):
            s['lastUpdateTime'] = 0
    # dict 字段
    if not isinstance(s.get('ruleExplore'), dict):
        s['ruleExplore'] = {}
    # str 字段
    if not isinstance(s.get('variableComment'), str):
        s['variableComment'] = ''
    # bookSourceType：**合法取值只有 Legado 的 0/1/2/3**
    # （`BookSourceType.kt` 的 @IntDef）。这里原来根本不看它，于是导入外部源时
    # 对方带个 4（或 99、字符串 "4"）会被原样收下、再原样导出回 App——
    # 而 App 那边 `4` 不是任何类型，属于脏数据。实测库里有过 6 条 `4`。
    # 归 0（文本）与 UI 保存时的口径一致（`SourceEditDialog.vue` 的 sanitize）
    v = s.get('bookSourceType')
    if v is None:
        s['bookSourceType'] = 0
    else:
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = 0
        s['bookSourceType'] = n if n in (0, 1, 2, 3) else 0
    return s

def main():
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else path
    c = json.load(open(path, encoding='utf-8'))
    if not isinstance(c, list):
        c = c.get('sources', [])
    fixed = 0
    for s in c:
        before = json.dumps(s.get('customButton')) + json.dumps(s.get('eventListener')) \
                 + json.dumps(s.get('concurrentRate'))
        clean_source(s)
        after = json.dumps(s.get('customButton')) + json.dumps(s.get('eventListener')) \
                + json.dumps(s.get('concurrentRate'))
        if before != after:
            fixed += 1
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    print(f'清洗 {len(c)} 个源，修复 {fixed} 个 → {out}')

if __name__ == '__main__':
    main()