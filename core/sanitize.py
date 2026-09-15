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
            s[f] = bool(v) if isinstance(v, (int, str)) and v not in ('', [], {}, 'false') else False
            if isinstance(v, list):
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