import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from offline_import import Api, SecurityBlockedError, chapter_from_source, convert_snapshot
from reader_core import BASE, Library, write_json

TEST_TEMP = Path(__file__).resolve().parents[1] / ".scratch"

try:
    import mwparserfromhell
except ImportError:
    mwparserfromhell = None


class ApiTests(unittest.TestCase):
    def test_security_block_stops_without_retry(self):
        TEST_TEMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            api = Api(Path(directory), interval=0)
            error = HTTPError(BASE, 567, "blocked", {}, None)
            with patch("offline_import.urlopen", side_effect=error) as request:
                with self.assertRaises(SecurityBlockedError):
                    api.get(action="query")
            self.assertEqual(request.call_count, 1)
            report = json.loads((Path(directory) / "last-error.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], 567)


@unittest.skipIf(mwparserfromhell is None, "optional import dependency not installed")
class SnapshotTests(unittest.TestCase):
    def test_chapter_order_and_groups_follow_semantic_properties(self):
        def record(title, category, order):
            return {"fulltext": title, "fullurl": BASE + title,
                    "printouts": {"章节": ["测试"], "剧情分类": [category], "序号": [order]}}
        inventory = {"b": record("b", "主线剧情", 2), "a": record("a", "主线剧情", 1),
                     "h": record("h", "隐藏剧情", 3)}
        result = chapter_from_source("{{剧情章节|章节=测试|分类=主线剧情|切换分类=隐藏剧情}}", inventory)
        self.assertEqual([r["title"] for r in result], ["a", "b", "h"])
        self.assertEqual([r["group"] for r in result], ["普通剧情", "普通剧情", "隐藏剧情"])

    def test_special_chapter_requires_rendered_page(self):
        with self.assertRaises(ValueError):
            chapter_from_source("{{剧情章节233|章节=测试}}", {})

    def test_cache_only_sync_preserves_progress_and_uses_no_network(self):
        TEST_TEMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            library = Library(root / "data")
            chapter_url, scene_url = BASE + "chapter", BASE + "scene"
            write_json(library.path("chapter", chapter_url), {"url": chapter_url, "content": [
                {"title": "scene", "url": scene_url, "group": "剧情"}]})
            write_json(library.path("scene", scene_url), {"url": scene_url, "content": {
                "title": "scene", "events": [{"kind": "note", "speaker": "旁白", "text": "正文"}]}})
            target = root / "dist" / "reader" / "data"
            target.mkdir(parents=True)
            progress = target / "progress.json"
            progress.write_bytes(b'{"position":42}')
            with patch("offline_import.ROOT", root), patch("offline_import.SNAPSHOT", root / "source"), \
                    patch("offline_import.urlopen", side_effect=AssertionError("unexpected network")):
                report = convert_snapshot([{"title": "chapter", "url": chapter_url}], {}, Api(), False)
            self.assertEqual(report["complete_chapters"], 1)
            self.assertEqual(progress.read_bytes(), b'{"position":42}')
            self.assertTrue(Library(target).cached("scene", scene_url))
