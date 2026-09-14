# -*- coding: utf-8 -*-
"""外部书源安全导入与待审冲突的测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.registry import (
    approve_pending_source,
    approve_review,
    import_sources,
    list_pending_reviews,
)


def write_sources(path: Path, sources: list[dict]) -> None:
    path.write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")


def read_sources(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_source(url: str, search_url: str = "https://example.com/search?q={{key}}") -> dict:
    return {
        "bookSourceName": "示例书源",
        "bookSourceUrl": url,
        "searchUrl": search_url,
        "ruleSearch": {"bookList": ".book"},
        "ruleToc": {"chapterList": ".chapter"},
        "ruleContent": {"content": "#content"},
    }


class ImportSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.candidate_path = self.base_dir / "candidates.json"
        self.incoming_path = self.base_dir / "incoming.json"
        self.registry_path = self.base_dir / "book_sources.sqlite3"
        self.raw_dir = self.base_dir / "imports" / "raw"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_marks_new_url_pending_without_changing_candidate(self) -> None:
        original_candidate = [make_source("https://current.example")]
        write_sources(self.candidate_path, original_candidate)
        write_sources(self.incoming_path, [make_source("https://new.example")])

        summary = import_sources(
            str(self.candidate_path), str(self.incoming_path),
            str(self.registry_path), str(self.raw_dir),
        )

        self.assertEqual(summary.new_count, 1)
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(read_sources(self.candidate_path), original_candidate)

    def test_same_url_same_rule_is_recorded_as_duplicate(self) -> None:
        source = make_source("https://same.example/")
        write_sources(self.candidate_path, [source])
        write_sources(self.incoming_path, [make_source("https://same.example")])

        summary = import_sources(
            str(self.candidate_path), str(self.incoming_path),
            str(self.registry_path), str(self.raw_dir),
        )

        self.assertEqual(summary.duplicate_count, 1)
        self.assertEqual(summary.conflict_count, 0)
        self.assertEqual(list_pending_reviews(str(self.registry_path)), [])

    def test_changed_rule_is_queued_for_review_without_overwriting_candidate(self) -> None:
        candidate = make_source("https://conflict.example", "https://conflict.example/search?q={{key}}")
        incoming = make_source("https://conflict.example", "https://conflict.example/find?q={{key}}")
        write_sources(self.candidate_path, [candidate])
        write_sources(self.incoming_path, [incoming])

        summary = import_sources(
            str(self.candidate_path), str(self.incoming_path),
            str(self.registry_path), str(self.raw_dir),
        )

        self.assertEqual(summary.conflict_count, 1)
        reviews = list_pending_reviews(str(self.registry_path))
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].url, "https://conflict.example")
        self.assertEqual(read_sources(self.candidate_path), [candidate])

    def test_approving_review_replaces_only_the_selected_candidate(self) -> None:
        candidate = make_source("https://approve.example", "https://approve.example/search?q={{key}}")
        incoming = make_source("https://approve.example", "https://approve.example/find?q={{key}}")
        write_sources(self.candidate_path, [candidate])
        write_sources(self.incoming_path, [incoming])
        import_sources(
            str(self.candidate_path), str(self.incoming_path),
            str(self.registry_path), str(self.raw_dir),
        )

        approved = approve_review(
            str(self.registry_path), str(self.candidate_path), "https://approve.example",
        )

        self.assertTrue(approved)
        self.assertEqual(read_sources(self.candidate_path)[0]["searchUrl"], incoming["searchUrl"])
        self.assertEqual(list_pending_reviews(str(self.registry_path)), [])

    def test_approving_pending_source_appends_it_to_candidate(self) -> None:
        original_candidate = [make_source("https://current.example")]
        incoming = make_source("https://new.example")
        write_sources(self.candidate_path, original_candidate)
        write_sources(self.incoming_path, [incoming])
        import_sources(
            str(self.candidate_path), str(self.incoming_path),
            str(self.registry_path), str(self.raw_dir),
        )

        approved = approve_pending_source(
            str(self.registry_path), str(self.candidate_path), "https://new.example",
        )

        self.assertTrue(approved)
        candidates = read_sources(self.candidate_path)
        self.assertEqual(candidates[0], original_candidate[0])
        self.assertEqual(candidates[1]["bookSourceUrl"], incoming["bookSourceUrl"])
        self.assertEqual(candidates[1]["searchUrl"], incoming["searchUrl"])


class ImportCliTests(unittest.TestCase):
    def test_import_command_accepts_candidate_registry_and_raw_dir(self) -> None:
        from cli.main import build_parser

        args = build_parser().parse_args([
            "import-sources", "--candidate", "candidate.json", "-i", "incoming.json",
            "--registry", "registry.sqlite3", "--raw-dir", "imports/raw",
        ])

        self.assertEqual(args.command, "import-sources")
        self.assertEqual(args.candidate, "candidate.json")
        self.assertEqual(args.registry, "registry.sqlite3")

    def test_merge_parser_accepts_first_input_priority(self) -> None:
        from cli.main import build_parser

        args = build_parser().parse_args([
            "merge", "-i", "attachment.json", "-i", "candidate.json",
            "--prefer-input", "first",
        ])

        self.assertEqual(args.prefer_input, "first")

    def test_prepare_parser_accepts_priority_and_outputs(self) -> None:
        from cli.main import build_parser

        args = build_parser().parse_args([
            "prepare", "-i", "attachment.json", "-i", "candidate.json",
            "--prefer-input", "first", "-o", "merged.json", "--full-output", "full.json",
            "--fast-output", "fast.json",
        ])

        self.assertEqual(args.command, "prepare")
        self.assertEqual(args.prefer_input, "first")
        self.assertEqual(args.merged_output, "merged.json")
        self.assertEqual(args.full_output, "full.json")
