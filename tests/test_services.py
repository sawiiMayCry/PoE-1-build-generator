import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services


class GameContextTests(unittest.TestCase):
    def setUp(self):
        services._cache.clear()

    def tearDown(self):
        services._cache.clear()

    def test_tree_download_is_pinned_to_the_checked_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "GameVersions.lua").write_text(
                'treeVersionList = { "3_29" }', encoding="utf-8")
            tree = {"nodes": {str(index): {"group": 1, "out": [], "in": []}
                              for index in range(1000)}}
            installed = {"version": "3_29",
                         "nodes": {str(index): {"links": [], "masteryEffects": []}
                                   for index in range(1000)},
                         "masteryEffects": {}}
            replies = iter((
                {"tag_name": "3.29.1"},
                [{"id": "test-league", "name": "Test League"}],
                tree,
            ))
            with patch("services.find_pob_installation", return_value=home), \
                    patch("services.json_get", side_effect=lambda _: next(replies)) as get, \
                    patch("services.get_worker") as get_worker:
                get_worker.return_value.request.return_value = installed
                context = services.game_context()

        pinned = "https://raw.githubusercontent.com/grindinggear/skilltree-export/3.29.1/data.json"
        self.assertEqual(get.call_args_list[-1].args[0], pinned)
        self.assertEqual(context["treeSource"], pinned)
        self.assertTrue(context["treeConsistency"]["passed"])

    def test_tree_comparison_reports_connections_and_mastery_mismatches(self):
        official = {"1": {"group": 1, "out": ["2"], "in": [],
                           "masteryEffects": [{"effect": 7, "name": "Life", "stats": ["5% life"]}]},
                    "2": {"group": 2, "out": [], "in": ["1"]}}
        installed = {"nodes": {"1": {"links": [], "masteryEffects": [7]},
                               "2": {"links": ["1"], "masteryEffects": []}},
                     "masteryEffects": {"7": {"name": "Life", "stats": ["5% life"]}}}
        result = services.compare_tree_metadata(official, installed)
        self.assertFalse(result["passed"])
        self.assertEqual(result["connectionMismatchNodeIds"], ["1"])


if __name__ == "__main__":
    unittest.main()
