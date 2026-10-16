"""Batch import checks; skipped when the optional import dependency is absent."""
import unittest

try:
    from wikitext_import import UnsupportedTemplate, parse_wikitext
except ImportError:
    parse_wikitext = None


@unittest.skipIf(parse_wikitext is None, "optional mwparserfromhell not installed")
class WikitextImportTests(unittest.TestCase):
    def test_nested_choices_remain_separate(self):
        scene = parse_wikitext("测试", """{{剧情|标题=别名}}
{{剧情/对话|甲|开场}}
{{剧情/选择|路线甲|{{剧情/对话|乙|甲路线}}
{{剧情/选择|内甲|{{剧情/对话||内甲正文}}|内乙|{{剧情/对话||内乙正文}}}}
|路线乙|{{剧情/对话|丙|乙路线}}}}
{{剧情/对话||结束}}""")
        self.assertEqual(scene["title"], "别名")
        self.assertEqual(len(scene["events"]), 3)
        outer = scene["events"][1]
        inner = outer["options"][0]["events"][1]
        self.assertNotEqual(inner["id"], outer["id"])
        self.assertEqual(outer["options"][1]["events"][0]["text"], "乙路线")

    def test_formatting_entities_links_and_comments(self):
        scene = parse_wikitext("测试", "{{剧情/对话||'''强调'''<br>[[目标|显示]] &amp; {{灰色|灰字}}<!--备注-->}}")
        self.assertEqual(scene["events"][0]["text"], "强调\n显示 & 灰字")

    def test_numeric_choice_keys_and_single_option(self):
        scene = parse_wikitext("测试", "{{剧情/选择|折叠=1|1=回应|2={{剧情/对话|甲|内容}}}}")
        self.assertEqual([e["text"] for e in scene["events"]], ["回应", "内容"])

    def test_unknown_template_is_reported_instead_of_dropped(self):
        with self.assertRaises(UnsupportedTemplate):
            parse_wikitext("测试", "{{剧情/对话||{{特殊台词|内容}}}}")

    def test_hidden_duplicate_not_imported(self):
        scene = parse_wikitext("测试", '<div style="display:none">{{剧情/对话||备份}}</div>{{剧情/对话||正文}}')
        self.assertEqual([e["text"] for e in scene["events"]], ["正文"])

    def test_inline_corrections_do_not_drop_dialogue(self):
        scene = parse_wikitext("测试", "{{颜色引用|灰|{{剧情/对话|甲|{{修正|正确|原文=错误}}的{{黑幕|词}}}}}}")
        self.assertEqual(scene["events"][0]["text"], "正确的词")

    def test_empty_movement_action_and_chat_message(self):
        scene = parse_wikitext("测试", "{{剧情/选择|前往二楼|}}{{人物对话|开始}}{{人物对话|左|甲|收到}}{{人物对话|结束}}")
        self.assertEqual([e["text"] for e in scene["events"]], ["前往二楼", "收到"])
