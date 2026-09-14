# -*- coding: utf-8 -*-
"""清洗书源 JSON：把 Legado 布尔/数字/对象字段的类型违规规范化，保证 Gson 可解析。

修复规则：
- customButton / eventListener: 期望 bool，list/None → False
- concurrentRate: 期望 int，'' → 0，字符串数字 → int
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
    for f in ('concurrentRate', 'customOrder', 'respondTime', 'weight'):
        v = s.get(f)
        if v is not None and not isinstance(v, int):
            if isinstance(v, str):
                try:
                    s[f] = int(float(v))
                except (ValueError, TypeError):
                    s[f] = 0
            else:
                s[f] = 0
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