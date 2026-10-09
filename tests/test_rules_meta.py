# -*- coding: utf-8 -*-
"""下发给前端的**结构事实**钉子：规则组→步骤映射、健康度词表。

这两个口存在的理由都是「前端不再各抄一份」（下发之前前端各有一份会静默漂的
副本）：后端改了这里的值，前端行为跟着变——所以改动必须是有意的，测试把
「顺手改了没意识到」拦住。
"""

import asyncio
import unittest

from backend.api.rules import rules_meta
from backend.api.sources import tags_meta
from core.models import HEALTH_NAMES, HEALTH_NEEDS_ACTION, Health
from core.verify import RULE_GROUP_TO_STEPS


class RulesMetaTests(unittest.TestCase):
    """``GET /api/rules/meta``：新鲜度判定吃的映射。"""

    def test_meta_serves_the_verify_constant(self):
        meta = asyncio.run(rules_meta())
        self.assertEqual(meta["rule_group_to_steps"], RULE_GROUP_TO_STEPS)

    def test_mapping_covers_all_four_rule_groups(self):
        self.assertEqual(set(RULE_GROUP_TO_STEPS),
                         {"ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent"})

    def test_book_url_evaluates_on_search_page(self):
        """proj-3-bookurl 的领域事实；改求值位置必须连同这条一起改，
        否则前端的新鲜度判定（改 bookUrl 不让 search 过期）静默判错。"""
        self.assertIn("search", RULE_GROUP_TO_STEPS["ruleSearch"])
        self.assertIn("bookUrl", RULE_GROUP_TO_STEPS["ruleSearch"])


class TagsMetaHealthTests(unittest.TestCase):
    """``GET /api/sources/tags/meta`` 的 health 词表与「需要动手」的档：
    前端健康判据的唯一替代（前端不再各抄一份）。"""

    def test_health_names_match_models_verbatim(self):
        meta = tags_meta()
        self.assertEqual([(x["value"], x["label"]) for x in meta["health_names"]],
                         list(HEALTH_NAMES.items()))

    def test_needs_action_matches_models_verbatim(self):
        meta = tags_meta()
        self.assertEqual(meta["health_needs_action"], list(HEALTH_NEEDS_ACTION))

    def test_needs_action_excludes_ok_and_pending(self):
        """判据本身要被钉住：PENDING 是「我们没结论」（没校验过 / 超时），算成坏档
        就是把一次请求都没发过的源凭空标成坏的；OK 更不该进来。前端「这批变坏了
        哪几条」直接吃这张表，多一个或少一个档都是**不报错**的少报/多报。"""
        self.assertIn(Health.OK, HEALTH_NAMES)
        self.assertNotIn(Health.OK, HEALTH_NEEDS_ACTION)
        self.assertNotIn(Health.PENDING, HEALTH_NEEDS_ACTION)
        for health in HEALTH_NEEDS_ACTION:
            self.assertIn(health, HEALTH_NAMES)


if __name__ == "__main__":
    unittest.main()
