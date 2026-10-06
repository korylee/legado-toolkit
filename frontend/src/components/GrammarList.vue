<!-- 语法速查清单（唯一一份）：编辑弹框右列与调试工作台共用。
     条目与 core/rules/replayer.py 的判定对齐（RULE_PREFIXES / VALUE_ACTIONS /
     COMMON_ATTRS / parse_rule）；本地回放不了的语法不硬凑进清单，缺口账在
     TODO syntax-gap。外壳各宿主自备（弹框是 el-card，工作台是 details）。 -->
<template>
  <ul class="muted grammar-list">
    <li>简写：class.xxx → .xxx；tag.a → a</li>
    <li>链式：class.list@tag.li@tag.a@href</li>
    <li>索引：.0 第一个、.-1 最后一个</li>
    <li>取值：@text / @textNodes / @ownText / @html / @all</li>
    <li>属性：@href / @src / @data-original 等</li>
    <li>正则：规则##正则##替换（支持 $1）</li>
    <li>类型前缀：@css: / @@ / @xpath: / @json:（没有 @html: 前缀——@html 是取值动作）</li>
    <li>接口源：$.data.list[*].name</li>
    <li>取图：tag.img@src / @data-original</li>
  </ul>
</template>

<style scoped>
.grammar-list { margin: 0; padding-left: 18px; line-height: 1.6; }
</style>
