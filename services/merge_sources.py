# -*- coding: utf-8 -*-
"""合并重复源：把**同一站点、行为指纹相同**的若干条并成一条。

为什么放在 services 而不是 core：它串起 store（读写）、dups（判据）、tags（标签
归一）完成一个业务动作——`services/add_source.py` 是同一个先例。

**判据一律在这里重算，不信调用方传来的分组**：前端只是个入口，而这一步是删源
（不可逆的资产变更）。跨站点、或行为指纹不同的，一律拒绝并说明原因——App 按
`bookSourceUrl` 认源，同域名不同端口是两个站点，规则相同也不等于同一个入口。

合并**不只是删除**，保留的那条要吸收被删条身上的东西：

  1. 用户标签取并集，`tags_added` 只装**本次真正新增**的（相对保留条原标签的差集）
     ——原样回传的话，撤销时会把用户本来就有的标签摘掉；
  2. 备注**默认不合并**（多条备注拼在一起在大多数情况下毫无用处，而在少数情况下会
     把 JS 片段/分享者签名糊成一段）；勾选时才拼，且只保留条在前、`\\n\\n` 连接、
     去空与完全重复、上限见 `COMMENT_LIMIT`；
  3. 最后**软删除**被合并条（`reason` 写明合并到哪条），可随时从回收站恢复。

顺序是刻意的：删除放最后。掉在中间的时候，坏的结果是「标签/备注改了但没删成」——
用户可以再点一次；反过来先删的话，坏结果是「删了但标签没并上」，那才是真的丢东西。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from core.dups import rule_signature, site_key
from core.loader import _normalize_url
from core.tags import extract_user_tags_from_group

#: 合并后备注的长度上限（Legado 那边没有硬限制，这里是防呆：拼出来的东西是给人看的）
COMMENT_LIMIT = 4000


class MergeRejected(ValueError):
    """判据不满足（而不是操作失败）——API 层据此返回 400，把原因原样给用户。"""


def _user_tags_of(src: Dict[str, Any]) -> List[str]:
    """一条源的**用户**标签。系统标签（类型/健康/质量）由程序生成，不参与合并。"""
    return extract_user_tags_from_group(str((src or {}).get("bookSourceGroup") or ""))


def _join_comments(comments: Sequence[str]) -> str:
    """多条备注连接：保留条在前、去空、去完全重复、`\\n\\n` 分隔、截断到上限。

    只去**完全重复**，不做归一或包含判断——备注是用户手写的内容，程序不该替他
    判断"这两段其实一样"。
    """
    out: List[str] = []
    for c in comments:
        text = str(c or "").strip()
        if text and text not in out:
            out.append(text)
    return "\n\n".join(out)[:COMMENT_LIMIT]


def merge(store, keep: str, drop: Iterable[str], merge_tags: bool = True,
          merge_comment: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """合并一组重复源。`dry_run=True` 时只返回「将发生的三件事」，不写库。

    返回 ``{merged, tags_added, prev_comment, restore_urls, drop, comment}``：
    后三项（`tags_added` / `prev_comment` / `restore_urls`）就是**撤销所需的全部信息**，
    由本函数原样给出，别让调用方从最终状态反推。
    """
    keep_src = store.get_source(keep)
    if not keep_src:
        raise MergeRejected("要保留的源不在库里")

    keep_key = _normalize_url(keep)
    drop_keys: List[str] = []
    for u in drop or []:
        k = _normalize_url(u)
        if k and k != keep_key and k not in drop_keys:
            drop_keys.append(k)
    drop_srcs: List[Tuple[str, Dict[str, Any]]] = []
    for k in drop_keys:
        src = store.get_source(k)
        if src:
            drop_srcs.append((k, src))
    if not drop_srcs:
        raise MergeRejected("没有被合并的源（都为空或已不存在）")

    # 判据重算：同站点 + 行为指纹相同
    site = site_key(str(keep_src.get("bookSourceUrl") or ""))
    sig = rule_signature(keep_src)
    for k, src in drop_srcs:
        if site_key(str(src.get("bookSourceUrl") or "")) != site:
            raise MergeRejected("跨站点不能合并：%s（App 按地址认源，不同站点是两条源）" % k)
        if rule_signature(src) != sig:
            raise MergeRejected("规则不同的源不能合并：%s（只是同名/同站，不是重复）" % k)

    keep_tags = _user_tags_of(keep_src)
    union: List[str] = list(keep_tags)
    for _k, src in drop_srcs:
        for t in _user_tags_of(src):
            if t not in union:
                union.append(t)
    # **只装真正新增的**：撤销时按它 remove，原样回传会把用户本来就有的标签摘掉
    tags_added = [t for t in union if t not in keep_tags] if merge_tags else []

    keep_comment = str(keep_src.get("bookSourceComment") or "")
    new_comment = keep_comment
    if merge_comment:
        new_comment = _join_comments(
            [keep_comment] + [str(s.get("bookSourceComment") or "") for _k, s in drop_srcs])
    comment_changed = new_comment != keep_comment

    drop_urls = [k for k, _s in drop_srcs]
    out: Dict[str, Any] = {
        "keep": keep_key,
        "drop": drop_urls,
        "tags_added": tags_added,
        "prev_comment": keep_comment if comment_changed else "",
        "comment": new_comment if comment_changed else "",
        "restore_urls": drop_urls,
        "merged": 0,
    }
    if dry_run:
        out["dry_run"] = True
        return out

    if tags_added:
        store.add_user_tags([keep_key], tags_added)
    if comment_changed:
        store.set_source_comment(keep_key, new_comment)
    merged, _snapshot = store.soft_delete(
        drop_urls, "合并到 %s" % str(keep_src.get("bookSourceUrl") or keep_key))
    out["merged"] = merged
    return out


def undo(store, keep: str, restore_urls: Sequence[str], tags_added: Sequence[str] = (),
         prev_comment: str = "") -> Dict[str, Any]:
    """撤销一次合并：恢复被删条 → 摘掉真增的标签 → 还原备注。

    `tags_added` 必须是 `merge` 返回的那一份（**只含真正新增的**）；
    `prev_comment` 只在当时确实改过备注时才有值。
    """
    keep_key = _normalize_url(keep)
    # 撤销是"把我刚删的那几条放回来"，而 merge 只记了 URL —— 先换成行 id
    # （restore 的粒度是 id：同一个 URL 在回收站里可能有多份历史版本）
    ids = store.trashed_ids([_normalize_url(u) for u in restore_urls or []])
    restored = store.restore(ids)["restored"]
    removed = 0
    if tags_added:
        # 一次调用即可：`remove_user_tags` 的语义是「从集合里移掉这些」，
        # 传进去的正是当时**真增**的那些；它返回的是受影响行数，不是标签数
        if store.remove_user_tags([keep_key], list(tags_added)):
            removed = len(tags_added)
    comment_back = False
    if prev_comment:
        comment_back = store.set_source_comment(keep_key, prev_comment) is not None
    return {"restored": restored, "tags_removed": removed, "comment_restored": comment_back}
