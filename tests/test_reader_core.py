from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from reader_core import BASE, INDEX_URL, Library, RateLimitedError, canonical_url, parse_catalog, parse_chapter, parse_scene, read_json, visible_events, write_json


def page(body):
    return f'<div class="mw-parser-output">{body}</div>'


def line(speaker, text):
    return f'<div class="zs-DialogBox"><div class="zs-DialogHead">{speaker}</div><div class="zs-DialogContent">{text}</div></div>'


def choice(*branches):
    labels = ''.join(f'<li class="tab_li">回应{i}</li>' for i in range(len(branches)))
    panes = ''.join(f'<div class="tab_con">{branch}</div>' for branch in branches)
    return f'<div class="TabContainer"><ul>{labels}</ul>{panes}</div>'


class ImportTests(unittest.TestCase):
    def test_empty_reply_branch_is_preserved(self):
        html = page(choice(line('甲', '答复'), '') + line('旁白', '继续'))
        result = parse_scene(html)['events']
        self.assertEqual(len(result[0]['options']), 2)
        self.assertEqual(result[0]['options'][1]['events'], [])
        visible, complete = visible_events(result, {result[0]['id']: 1})
        self.assertTrue(complete)
        self.assertEqual(visible[-1]['text'], '继续')

    def test_single_empty_action_and_combat_subtitle(self):
        html = page(choice('') + '<div class="zs-DialogBox">'
                    '<div class="zs-DialogAct">战斗开始</div>'
                    '<div class="zs-DialogAct">保护装置</div></div>')
        result = parse_scene(html)['events']
        self.assertEqual(result[0]['text'], '回应0')
        self.assertEqual(result[1]['text'], '战斗开始\n保护装置')

    def test_hidden_duplicate_and_title_id_are_excluded(self):
        html = page('<div class="zs-DialogTitle">测试<span style="display: none">12345</span></div>'
                    '<div style="display:none">' + line('重复', '备份文本') + '</div>'
                    + line('角色', '第一行<br/>第二行 &amp; 第三行'))
        scene = parse_scene(html)
        self.assertEqual(scene['title'], '测试')
        self.assertEqual(scene['events'], [{'kind': 'line', 'speaker': '角色', 'text': '第一行\n第二行 & 第三行'}])

    def test_choice_branches_never_mix(self):
        events = parse_scene(page(line('甲', '之前') + choice(line('乙', '路线A'), line('丙', '路线B')) + line('甲', '之后')))['events']
        visible, complete = visible_events(events, {})
        self.assertFalse(complete)
        self.assertEqual(len(visible), 2)
        selected, complete = visible_events(events, {events[1]['id']: 1})
        self.assertTrue(complete)
        self.assertEqual([e.get('text') for e in selected], ['之前', None, '路线B', '之后'])

    def test_nested_choice_stops_outer_continuation(self):
        nested = choice(line('甲', '内一'), line('甲', '内二'))
        events = parse_scene(page(choice(nested, line('甲', '外二')) + line('甲', '结尾')))['events']
        outer_id = events[0]['id']
        visible, complete = visible_events(events, {outer_id: 0})
        self.assertFalse(complete)
        self.assertEqual([e['kind'] for e in visible], ['choice', 'choice'])
        self.assertNotEqual(visible[0]['id'], visible[1]['id'])

    def test_single_tab_is_context_not_choice(self):
        events = parse_scene(page(choice(line('甲', '互动对白'))))['events']
        self.assertEqual([e['kind'] for e in events], ['note', 'line'])

    def test_narration_and_battle_notes_stay_in_sequence(self):
        html = page('<div class="zs-DialogBox"><div class="zs-DialogContent noHead">旁白</div></div>'
                    '<div class="zs-DialogActBox zs-DialogBox"><div class="zs-DialogAct main">战斗</div></div>'
                    '<div class="zs-DialogActLeft">继续前进</div>')
        events = parse_scene(html)['events']
        self.assertEqual([e['text'] for e in events], ['旁白', '战斗', '继续前进'])

    def test_chapter_normal_and_hidden_groups(self):
        anchor = '<a title="{0}" href="/zspms/{0}"><span class="zs-btnDialogPlay">{0}</span></a>'
        html = page('<div class="zs-cswitchbtn">普通剧情</div><div class="zs-cswitchbtn">隐藏剧情</div>'
                    '<div class="zs-clist">' + anchor.format('1-1普通') + '</div>'
                    '<div class="zs-clist">' + anchor.format('1-1隐藏') + '</div>')
        scenes = parse_chapter(html)
        self.assertEqual([s['group'] for s in scenes], ['普通剧情', '隐藏剧情'])

    def test_catalog_deduplicates_backup_and_excludes_red_links(self):
        card = '<div class="zs-btnDialogCBox"><a href="/zspms/01_Test" title="01 Test">01 Test</a></div>'
        red = '<div class="zs-btnDialogCBox"><a class="new" href="/zspms/Missing">未收录</a></div>'
        html = page('<div style="display:none">' + card + '</div><div class="TabContainer"><ul><li class="tab_li">主线剧情</li></ul><div class="tab_con">' + card + card + red + '</div></div>')
        catalog = parse_catalog(html)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['group'], '主线剧情')

    def test_invalid_choice_does_not_skip_to_later_text(self):
        events = parse_scene(page(choice(line('甲', '一'), line('乙', '二')) + line('丙', '后续')))['events']
        for value in (-1, 100, '0', None):
            result, complete = visible_events(events, {events[0]['id']: value})
            self.assertFalse(complete)
            self.assertEqual(len(result), 1)

    def test_non_scene_fails_explicitly(self):
        with self.assertRaises(ValueError):
            parse_scene(page('<p>还没收录</p>'))

    def test_urls_are_restricted_and_canonicalized(self):
        self.assertEqual(canonical_url('/zspms/序章#片段'), BASE + '%E5%BA%8F%E7%AB%A0')
        for url in ('https://example.com/zspms/a', 'file:///etc/passwd', 'https://wiki.biligame.com/other/a'):
            with self.assertRaises(ValueError):
                canonical_url(url)


class StorageTests(unittest.TestCase):
    def test_server_throttling_has_a_specific_error(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            library = Library(directory)
            with patch('reader_core.urlopen', side_effect=HTTPError(BASE + 'test', 567, 'limited', {}, None)):
                with self.assertRaises(RateLimitedError):
                    library.load('scene', BASE + 'test')
            self.assertFalse(library.cached('scene', BASE + 'test'))

    def test_cached_scene_never_needs_network(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            library = Library(directory)
            expected = library.seed('scene', BASE + 'test', page(line('甲', '离线文本')))
            with patch('reader_core.urlopen', side_effect=AssertionError('offline')):
                self.assertEqual(library.load('scene', BASE + 'test'), expected)

    def test_progress_round_trip_and_corruption_fallback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'progress.json'
            progress = {'scenes': {'例子': {'position': 7, 'choices': {'1': 0}}}}
            write_json(path, progress)
            self.assertEqual(read_json(path, {}), progress)
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(read_json(path, {}), {})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
