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
from core.models import HEALTH_NAMES
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
    """``GET /api/sources/tags/meta`` 的 health_names：前端词表副本的唯一替代。"""

    def test_health_names_match_models_verbatim(self):
        meta = tags_meta()
        self.assertEqual([(x["value"], x["label"]) for x in meta["health_names"]],
                         list(HEALTH_NAMES.items()))


if __name__ == "__main__":
    unittest.main()
