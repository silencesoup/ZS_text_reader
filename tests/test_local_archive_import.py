import unittest

from local_archive_import import GraphError, build_graph, clean_text, convert_section, match_scene
from reader_core import visible_events


class LocalGraphTests(unittest.TestCase):
    def test_conversion_preserves_join_without_repeating_common_text(self):
        pack = {"nodes": [
            {"id": "start", "sectionId": "s", "kind": "choice", "options": [
                {"label": "甲", "targetId": "a"}, {"label": "乙", "targetId": "b"}]},
            {"id": "a", "sectionId": "s", "kind": "line", "speaker": "甲", "text": "甲路线", "nextId": "join"},
            {"id": "b", "sectionId": "s", "kind": "line", "speaker": "乙", "text": "乙路线", "nextId": "join"},
            {"id": "join", "sectionId": "s", "kind": "return", "nextId": "common"},
            {"id": "common", "sectionId": "s", "kind": "line", "speaker": "旁白", "text": "公共正文", "nextId": None},
        ]}
        scene, _ = convert_section(pack, {"id": "s", "title": "小节", "startId": "start"})
        events, complete = visible_events(scene["events"], {"start": 1})
        self.assertTrue(complete)
        self.assertEqual([e.get("text") for e in events], [None, "乙路线", "公共正文"])

    def test_graph_revisiting_menu_requires_a_new_answer(self):
        graph = {"kind": "graph", "start": "menu", "nodes": {
            "menu": {"kind": "choice", "options": [{"label": "询问", "target": "a"}, {"label": "离开", "target": "end"}]},
            "a": {"kind": "line", "speaker": "甲", "text": "答复", "next": "menu"},
            "end": {"kind": "end"},
        }}
        events, complete = visible_events([graph], {"menu": 0})
        self.assertFalse(complete)
        self.assertEqual(events[-1]["id"], "menu:visit:2")
        events, complete = visible_events([graph], {"menu": 0, "menu:visit:2": 1})
        self.assertTrue(complete)
        self.assertEqual(len([e for e in events if e.get("text") == "答复"]), 1)

    def test_single_reply_does_not_duplicate_its_dialogue(self):
        graph = {"kind": "graph", "start": "menu", "nodes": {
            "menu": {"kind": "choice", "options": [{"label": "收到", "target": "a"}]},
            "a": {"kind": "line", "speaker": "甲", "text": "收到", "next": None},
        }}
        events, complete = visible_events([graph], {})
        self.assertTrue(complete)
        self.assertEqual(len(events), 1)

    def test_missing_target_is_rejected(self):
        with self.assertRaises(GraphError):
            convert_section({"nodes": [{"id": "a", "sectionId": "s", "kind": "line", "nextId": "missing"}]},
                            {"id": "s", "startId": "a", "title": "测试"})

    def test_archived_empty_choice_is_not_a_live_menu(self):
        graph = build_graph([
            {'id': 'old', 'kind': 'choice', 'archived': True, 'options': [], 'nextId': 'a'},
            {'id': 'a', 'kind': 'line', 'text': '<size=20>原文</size>', 'speaker': '甲', 'nextId': None},
        ], {'startId': 'old'}, 'test')
        events, complete = visible_events([graph], {})
        self.assertTrue(complete)
        self.assertEqual(events[0]['text'], '原文')

    def test_orphaned_source_text_has_an_explicit_entry(self):
        graph = build_graph([
            {'id': 'a', 'kind': 'line', 'text': '正文', 'nextId': None},
            {'id': 'b', 'kind': 'line', 'text': '另一段', 'nextId': None},
        ], {'startId': 'a'}, 'test', True)
        events, complete = visible_events([graph], {})
        self.assertFalse(complete)
        events, complete = visible_events([graph], {events[0]['id']: 1})
        self.assertTrue(complete)
        self.assertEqual(events[-1]['text'], '另一段')

    def test_stage_number_does_not_match_a_different_title(self):
        result = match_scene({'pack': {'nodes': []}}, {'section': {'id': 's', 'title': '21-14 其他内容 [主线剧情]'}},
                             [{'url': 'https://wiki.biligame.com/zspms/test', 'title': '21-14倒错迷宫', 'group': '普通剧情'}])
        self.assertIsNone(result)
