"""Read text indexes and dialogue graphs directly from local voice-pack ZIPs."""
from __future__ import annotations

import argparse
from collections import Counter, deque
import csv
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import time
import unicodedata
from zipfile import ZipFile
from urllib.parse import quote, urlencode

from reader_core import BASE, INDEX_URL, Library, canonical_url, read_json, visible_events, write_json

ROOT = Path(__file__).resolve().parent
LOCAL_DATA = ROOT / "data" / "local-import"


class GraphError(ValueError):
    pass


def text_key(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def title_key(title):
    text = unicodedata.normalize("NFKC", title)
    text = re.sub(r"^(?:剧情/)?(?:第\d+章|\d+[_ .·-]+)", "", text)
    text = re.sub(r"\(角色.*?整合\)", "", text)
    return re.sub(r"[\s_·]+", "", text)


def scene_key(title):
    title = re.sub(r"^(?:普通|隐藏)\s*[·:：]\s*", "", title)
    title = re.sub(r"\s*\[[^\]]+\]\s*$", "", title)
    title = re.sub(r"\d+", lambda m: str(int(m[0])), title)
    return text_key(title).replace('·', '').replace('/', '')


def clean_text(text):
    from html import unescape
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</?(?:size|color|b|i|u|s|font|sup|sub|small|big|center|span|align|mark|voffset|cspace|mspace|indent|pos|sprite|link|style)(?:[ =][^>]*)?>", "", text, flags=re.I)
    return unescape(text).strip()


def build_graph(nodes, section, prefix, include_unreachable=False):
    """Keep original jumps, including cycles, without expanding a graph to a tree."""
    source = {n["id"]: n for n in nodes}
    graph_nodes = {}
    def key(value):
        return prefix + ':' + value if value is not None else None
    for node in nodes:
        kind = node["kind"]
        if node.get("archived"):
            event = {"kind": "jump", "next": key(node.get("nextId"))}
        elif kind == "choice":
            event = {"kind": "choice", "options": [{"label": clean_text(o["label"]), "target": key(o["targetId"])}
                                                       for o in node.get("options", [])]}
            if not event["options"]:
                raise GraphError("选项节点为空")
        elif kind == "end":
            event = {"kind": "end"}
        elif kind in {"merge", "gap", "return"}:
            event = {"kind": "jump", "next": key(node.get("nextId"))}
        elif kind == "line":
            text = clean_text(node.get("rawText") or node.get("text"))
            speaker = clean_text(node.get("speaker")) or "旁白"
            if text:
                event = {"kind": "note" if speaker in {"旁白", "动作提示", "情境"} else "line",
                         "speaker": "情境" if speaker == "动作提示" else speaker, "text": text,
                         "next": key(node.get("nextId"))}
            else:
                event = {"kind": "jump", "next": key(node.get("nextId"))}
        else:
            raise GraphError("未知节点类型：" + kind)
        graph_nodes[key(node["id"])] = event
    def successors(event):
        return [o["target"] for o in event["options"]] if event["kind"] == "choice" else ([event["next"]] if event.get("next") else [])
    for event in graph_nodes.values():
        if any(target not in graph_nodes for target in successors(event)):
            raise GraphError("剧情节点的目标缺失")
    start = key(section["startId"])
    if start not in graph_nodes:
        raise GraphError("起始节点缺失")
    # Every automatic path must eventually reach an end or a menu requiring input.
    automatic = {k: ([] if e["kind"] == "choice" and len(e["options"]) > 1 else successors(e)) for k, e in graph_nodes.items()}
    incoming = Counter(t for targets in automatic.values() for t in targets)
    queue = deque(k for k in graph_nodes if incoming[k] == 0)
    count = 0
    while queue:
        k = queue.popleft(); count += 1
        for target in automatic[k]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if count != len(graph_nodes):
        raise GraphError("存在没有选项出口的自动循环")

    def reachable(entry):
        seen, queue = set(), [entry]
        while queue:
            k = queue.pop()
            if k not in seen:
                seen.add(k); queue.extend(successors(graph_nodes[k]))
        return seen

    entries = [("正文", start)]
    covered = reachable(start)
    if include_unreachable:
        for node in nodes:
            k = key(node["id"])
            if k not in covered and node["kind"] == "line" and not node.get("archived") and graph_nodes[k]["kind"] in {"line", "note"}:
                entries.append(("补充片段 · " + graph_nodes[k]["text"].replace('\n', ' ')[:24], k))
                covered.update(reachable(k))
    if len(entries) > 1:
        start = prefix + ':reader-entry'
        graph_nodes[start] = {"kind": "choice", "options": [{"label": label, "target": target} for label, target in entries]}
    return {"kind": "graph", "start": start, "nodes": graph_nodes}


def section_groups(pack):
    """Screen-position bookmarks duplicate existing dialogue, so keep chapter roots."""
    groups = []
    for chapter in pack["chapters"]:
        roots = [s for s in chapter["sections"] if not s["title"].endswith((' · 当前画面定位', ' · 条件路线'))]
        records = [{"chapter": chapter, "section": s, "alternatives": []} for s in roots]
        for section in chapter["sections"]:
            if section["title"].endswith(' · 条件路线'):
                title = section["title"].removesuffix(' · 条件路线')
                matches = [r for r in records if r['section']['title'] == title]
                if len(matches) == 1:
                    matches[0]['alternatives'].append(section)
                else:
                    records.append({"chapter": chapter, "section": section, "alternatives": []})
        groups.extend(records)
    return groups


def group_content(pack, group):
    by_section = {}
    for node in pack["nodes"]:
        by_section.setdefault(node['sectionId'], []).append(node)
    section = group['section']
    graph = build_graph(by_section[section['id']], section, section['id'], True)
    if group['alternatives']:
        entries = [{"label": "正文", "target": graph['start']}]
        for number, alternate in enumerate(group['alternatives'], 1):
            branch = build_graph(by_section[alternate['id']], alternate, alternate['id'])
            graph['nodes'].update(branch['nodes'])
            entries.append({"label": f"条件路线 {number}", "target": branch['start']})
        graph['start'] = section['id'] + ':reader-route'
        graph['nodes'][graph['start']] = {"kind": "choice", "options": entries}
    events, complete = visible_events([graph], {})
    if not events:
        raise GraphError("小节没有可阅读的台词")
    return {"title": section['title'], "events": [graph]}


def load_archives(directory):
    directory = Path(directory)
    manifest = read_json(directory / "压缩包清单.json", None)
    if manifest is None:
        raise ValueError("没有找到压缩包清单.json")
    entries = manifest["archives"]
    result = []
    for number, entry in enumerate(entries, 1):
        relative = entry["relative_zip"]
        archive_path = (directory / relative).resolve()
        if not archive_path.is_relative_to(directory.resolve()):
            raise ValueError("清单中的压缩包路径越界")
        stat = archive_path.stat()
        snapshot = LOCAL_DATA / "metadata" / (sha256(relative.encode()).hexdigest()[:24] + ".json")
        saved = read_json(snapshot, {})
        fingerprint = [stat.st_size, stat.st_mtime_ns]
        if saved.get("fingerprint") != fingerprint:
            with ZipFile(archive_path, "r") as archive:
                def member(suffix, optional=False):
                    matches = [i for i in archive.infolist() if i.filename.endswith('/' + suffix) or i.filename == suffix]
                    if not matches and optional:
                        return None
                    if len(matches) != 1:
                        raise ValueError(f"{relative}: {suffix} 数量不是 1")
                    if matches[0].file_size > 128 * 1024 * 1024:
                        raise ValueError("文本元数据过大")
                    return archive.read(matches[0])
                pack_bytes = member("pack.json")
                pack = json.loads(pack_bytes.decode("utf-8-sig"))
                csv_bytes = member("台词音频索引.csv", optional=True)
                rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""))) if csv_bytes else []
            keep = {"id", "sectionId", "kind", "text", "rawText", "speaker", "pathId", "nextId", "archived", "source"}
            option_keep = {"id", "label", "targetId", "returnId", "pathId"}
            slim_nodes = []
            for node in pack["nodes"]:
                slim = {k: v for k, v in node.items() if k in keep}
                if "options" in node:
                    slim["options"] = [{k: v for k, v in option.items() if k in option_keep} for option in node["options"]]
                slim_nodes.append(slim)
            saved = {"archive": relative, "fingerprint": fingerprint, "category": entry["category"],
                     "archive_title": entry["title"], "pack_sha256": sha256(pack_bytes).hexdigest(),
                     "csv_present": csv_bytes is not None,
                     "pack": {"id": pack["id"], "title": pack["title"], "chapters": pack["chapters"], "nodes": slim_nodes},
                     "csv": [{k: row.get(k, "") for k in ("章节", "小节", "小节内索引序号", "角色", "台词", "分支", "台词编号")} for row in rows]}
            write_json(snapshot, saved)
        result.append(saved)
        if number % 20 == 0 or number == len(entries):
            print(f"已读取 {number}/{len(entries)} 个压缩包的文本索引", flush=True)
    return result


def section_graph(pack, section):
    nodes = {}
    for node in pack["nodes"]:
        if node["sectionId"] == section["id"]:
            if node["id"] in nodes:
                raise GraphError("重复的剧情节点 ID")
            nodes[node["id"]] = node
    start = section["startId"]
    if start not in nodes:
        raise GraphError("起始节点缺失")
    edges = {}
    for key, node in nodes.items():
        if node["kind"] == "choice":
            targets = [o["targetId"] for o in node.get("options", [])]
        elif node["kind"] == "end":
            targets = []
        elif node["kind"] in {"line", "gap", "merge", "return"}:
            targets = [node["nextId"]] if node.get("nextId") else []
        else:
            raise GraphError("未知节点类型：" + node["kind"])
        if any(t not in nodes for t in targets):
            raise GraphError("节点指向当前小节外或缺失的目标")
        edges[key] = list(dict.fromkeys(targets))
    reachable, pending = set(), [start]
    while pending:
        key = pending.pop()
        if key not in reachable:
            reachable.add(key)
            pending.extend(edges[key])
    incoming = Counter(target for key in reachable for target in edges[key])
    queue = deque(key for key in nodes if key in reachable and incoming[key] == 0)
    order = []
    while queue:
        key = queue.popleft()
        order.append(key)
        for target in edges[key]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if len(order) != len(reachable):
        raise GraphError("小节含循环跳转，不能直接转换为树状对话")
    return nodes, edges, order, reachable


def convert_section(pack, section):
    nodes, edges, order, reachable = section_graph(pack, section)
    # Postdominators find the actual common continuation of every reply.
    # Bitsets avoid quadratic Python-set overhead on long chapters.
    index = {key: i for i, key in enumerate(order)}
    terminal = len(order)
    postdom = {None: 1 << terminal}
    for key in reversed(order):
        successors = edges[key] or [None]
        common = postdom[successors[0]]
        for target in successors[1:]:
            common &= postdom[target]
        postdom[key] = common | (1 << index[key])
    emitted = 0

    def visit(key, stop=None):
        nonlocal emitted
        events = []
        while key is not None and key != stop:
            emitted += 1
            if emitted > max(len(order) * 20, 5000):
                raise GraphError("分支展开超过大小限制")
            node = nodes[key]
            kind = node["kind"]
            if kind == "end":
                break
            if kind == "choice":
                options = node.get("options", [])
                if not options:
                    raise GraphError("选项节点没有可选入口")
                candidates = postdom[key] & ~(1 << index[key])
                nearest = (candidates & -candidates).bit_length() - 1
                join = order[nearest] if nearest < terminal else None
                if len(options) == 1:
                    option = options[0]
                    target = nodes[option["targetId"]]
                    if text_key(option["label"]) != text_key(target.get("rawText") or target.get("text")):
                        events.append({"kind": "note", "speaker": "情境", "text": option["label"], "source_id": key})
                    events.extend(visit(option["targetId"], join))
                else:
                    branches = [{"label": o["label"], "events": visit(o["targetId"], join)} for o in options]
                    events.append({"kind": "choice", "id": key, "options": branches})
                key = join
                continue
            if kind == "line":
                text = node.get("rawText") or node.get("text") or ""
                if text.strip():
                    speaker = node.get("speaker") or "旁白"
                    note = speaker in {"旁白", "动作提示", "情境"}
                    events.append({"kind": "note" if note else "line", "speaker": "情境" if speaker == "动作提示" else speaker,
                                   "text": text, "source_id": key})
            # merge/gap nodes are playback bookkeeping; follow their explicit edge.
            key = node.get("nextId")
        return events

    events = visit(section["startId"])
    if not events:
        raise GraphError("小节没有正文")
    return {"title": section["title"], "events": events}, {
        "reachable": len(reachable), "unreachable_lines": [n["id"] for n in nodes.values()
            if n["id"] not in reachable and n["kind"] == "line" and not n.get("archived")],
        "archived_lines": sum(n.get("archived", False) and n["kind"] == "line" for n in nodes.values()),
    }


def flatten(events):
    for event in events:
        if event["kind"] == "choice":
            for option in event["options"]:
                yield from flatten(option["events"])
        else:
            yield event


def audit(archives):
    summary = {"archives": len(archives), "csv_rows": 0, "csv_mismatches": [], "csv_missing": [], "kinds": Counter(),
               "graph_errors": [], "sections": 0, "converted_sections": 0, "source_sections": 0,
               "csv_protection_prefixes": 0, "position_bookmarks": 0, "condition_routes": 0}
    for saved in archives:
        pack = saved["pack"]
        nodes = {n["id"]: n for n in pack["nodes"]}
        if not saved.get("csv_present", True):
            summary["csv_missing"].append(saved["archive"])
        summary["kinds"].update(n["kind"] for n in pack["nodes"])
        for row in saved["csv"]:
            summary["csv_rows"] += 1
            node = nodes.get(row["台词编号"])
            csv_text = row["台词"]
            if csv_text.startswith("'") and len(csv_text) > 1 and csv_text[1] in "=+-@":
                csv_text = csv_text[1:]
                summary['csv_protection_prefixes'] += 1
            if not node or text_key(csv_text) != text_key(node.get("text")) or row["角色"] != node.get("speaker", ""):
                summary["csv_mismatches"].append({"archive": saved["archive"], "id": row["台词编号"]})
        for chapter in pack['chapters']:
            summary['source_sections'] += len(chapter['sections'])
            summary['position_bookmarks'] += sum(s['title'].endswith(' · 当前画面定位') for s in chapter['sections'])
            summary['condition_routes'] += sum(s['title'].endswith(' · 条件路线') for s in chapter['sections'])
        for group in section_groups(pack):
            summary["sections"] += 1
            try:
                group_content(pack, group)
                summary["converted_sections"] += 1
            except (GraphError, RecursionError) as error:
                summary["graph_errors"].append({"archive": saved["archive"], "section": group['section']["title"], "error": str(error)})
    write_json(LOCAL_DATA / "audit.json", summary)
    return summary


def wiki_records(chapter, library, inventory):
    cached = read_json(library.path('chapter', chapter['url']), {})
    records = list(cached.get('content', []))
    seen = {r['url'] for r in records}
    for record in inventory.values():
        if not any(title_key(t) == title_key(chapter['title'].removeprefix('剧情/')) for t in record['printouts']['章节']):
            continue
        url = canonical_url(record['fullurl'])
        if url not in seen:
            categories = record['printouts']['剧情分类']
            group = '隐藏剧情' if any('隐藏' in c for c in categories) else '普通剧情' if chapter['group'] == '主线剧情' else '剧情'
            records.append({'title': record['fulltext'], 'url': url, 'group': group})
            seen.add(url)
    if chapter['group'] == '主线剧情':
        records = [{**r, 'group': '普通剧情' if r.get('group') == '剧情' else r['group']} for r in records]
    return records


def match_scene(saved, group, records):
    section = group['section']
    urls = {canonical_url(n['source']) for n in saved['pack']['nodes']
            if n['sectionId'] == section['id'] and n.get('source', '').startswith(BASE)}
    if len(urls) == 1:
        found = [r for r in records if r['url'] in urls]
        if len(found) == 1:
            return found[0]
    key = scene_key(clean_text(section['title']))
    matches = [r for r in records if scene_key(clean_text(r['title'])) == key]
    if len(matches) == 1:
        return matches[0]
    def stem(title):
        value = scene_key(clean_text(title))
        while True:
            following = re.sub(r'^(?:[A-Z]+-?)?\d+(?:-\d+)?', '', value, flags=re.I)
            if following == value:
                return value
            value = following
    matches = [r for r in records if stem(r['title']) == stem(section['title'])]
    if len(matches) == 1:
        return matches[0]
    # Older WIKI headings contain occasional naming corrections. An unambiguous
    # chapter/ordinary-or-hidden/stage number still identifies the same scene.
    explicit_group = '隐藏剧情' if '隐藏' in section['title'] else '普通剧情' if '[主线剧情]' in section['title'] else None
    number = re.match(r'((?:[A-Z]+-?)?\d+(?:-\d+)?)', scene_key(clean_text(section['title'])), re.I)
    if explicit_group and number:
        matches = [r for r in records if r['group'] == explicit_group and
                   (m := re.match(r'((?:[A-Z]+-?)?\d+(?:-\d+)?)', scene_key(clean_text(r['title'])), re.I)) and m[1] == number[1]]
        if len(matches) == 1 and SequenceMatcher(None, stem(matches[0]['title']), stem(section['title'])).ratio() >= .7:
            return matches[0]
    return None


def stage_import(archives):
    library = Library(ROOT / 'data')
    catalog = read_json(library.path('catalog', INDEX_URL), {})['content']
    inventory = read_json(ROOT / 'data/offline-source/inventory.json', {})
    stage = Library(LOCAL_DATA / 'staged')
    generated = set()
    report = {'archives': len(archives), 'chapters': [], 'new_scenes': 0, 'preserved_scenes': 0,
              'errors': [], 'wiki_comparisons': [], 'created_at': int(time.time())}
    by_title = {}
    for chapter in catalog:
        by_title.setdefault(title_key(chapter['title'].removeprefix('剧情/')), []).append(chapter)
    for number, saved in enumerate(archives, 1):
        candidates = by_title.get(title_key(saved['pack']['title']), []) or by_title.get(title_key(saved['archive_title']), [])
        if len(candidates) == 1:
            chapter = candidates[0]
        elif not candidates:
            category = {'主线': '主线剧情', '空中花园': '空中花园 / 角色日常'}.get(saved['category'], saved['category'])
            chapter = {'title': saved['pack']['title'], 'url': BASE + quote(saved['pack']['title']), 'group': category}
            catalog.append(chapter)
            by_title.setdefault(title_key(chapter['title']), []).append(chapter)
        else:
            report['errors'].append({'archive': saved['archive'], 'error': '章节名存在歧义'})
            continue
        records = wiki_records(chapter, library, inventory)
        imported, urls_used = [], set()
        archive_errors = []
        for group in section_groups(saved['pack']):
            section = group['section']
            matched = match_scene(saved, group, records)
            if matched:
                record = dict(matched)
            else:
                title = clean_text(section['title'])
                group_label = '隐藏剧情' if '隐藏' in title or section['id'].startswith('YC') else '普通剧情' if saved['category'] == '主线' else '剧情'
                record = {'title': title, 'group': group_label, 'url': chapter['url'] + '?' + urlencode({'local_scene': section['id']})}
            if record['url'] in urls_used:
                record = {**record, 'url': record['url'] + ('&' if '?' in record['url'] else '?') + urlencode({'local_part': section['id']}),
                          'title': clean_text(section['title']) + '（段 2）'}
            urls_used.add(record['url'])
            try:
                content = group_content(saved['pack'], group)
                content['title'] = record['title']
                content['local_source'] = {'archive': saved['archive'], 'section_id': section['id'], 'pack_sha256': saved['pack_sha256']}
                previous = read_json(library.path('scene', record['url']), None)
                if previous and not previous['content'].get('local_source'):
                    report['preserved_scenes'] += 1
                    # Compare original graph utterances to the downloaded transcript.
                    try:
                        tree, _ = convert_section(saved['pack'], section)
                        graph_lines = [text_key(clean_text(e['text'])) for e in flatten(tree['events']) if e.get('speaker') != '情境']
                        actions = {text_key(clean_text(n.get('text'))) for n in saved['pack']['nodes']
                                   if n['sectionId'] == section['id'] and n.get('speaker') == '动作提示'}
                        wiki_lines = [text_key(clean_text(e['text'])) for e in flatten(previous['content']['events'])
                                      if e.get('speaker') != '情境' and text_key(clean_text(e['text'])) not in actions]
                        common = sum(b.size for b in SequenceMatcher(None, wiki_lines, graph_lines, autojunk=False).get_matching_blocks())
                        report['wiki_comparisons'].append({'section': record['title'], 'wiki_lines': len(wiki_lines),
                            'local_lines': len(graph_lines), 'ordered_matches': common, 'archive': saved['archive']})
                    except GraphError:
                        pass
                else:
                    write_json(stage.path('scene', record['url']), {'url': record['url'], 'saved_at': int(time.time()), 'content': content})
                    generated.add(stage.path('scene', record['url']).name)
                    report['new_scenes'] += 1
                imported.append(record)
            except (GraphError, ValueError, RecursionError) as error:
                issue = {'archive': saved['archive'], 'section': section['title'], 'error': str(error)}
                archive_errors.append(issue); report['errors'].append(issue)
        imported_keys = {scene_key(clean_text(r['title'])) for r in imported}
        unmatched = [r for r in records if r['url'] not in urls_used and scene_key(clean_text(r['title'])) not in imported_keys]
        imported.extend(unmatched)
        # Keep ordinary and hidden stories contiguous for automatic chapter advance.
        group_order = list(dict.fromkeys(r['group'] for r in imported))
        imported = [r for label in group_order for r in imported if r['group'] == label]
        write_json(stage.path('chapter', chapter['url']), {'url': chapter['url'], 'saved_at': int(time.time()), 'content': imported})
        generated.add(stage.path('chapter', chapter['url']).name)
        available = sum(library.cached('scene', r['url']) or stage.cached('scene', r['url']) for r in imported)
        report['chapters'].append({'title': chapter['title'], 'archive': saved['archive'], 'sections': len(imported),
            'readable': available, 'not_in_archive': [r['title'] for r in unmatched if not library.cached('scene', r['url'])],
            'source_groups': len(section_groups(saved['pack'])), 'errors': len(archive_errors)})
        if number % 20 == 0 or number == len(archives):
            print(f"已转换 {number}/{len(archives)} 个章节包", flush=True)
    write_json(stage.path('catalog', INDEX_URL), {'url': INDEX_URL, 'saved_at': int(time.time()), 'content': catalog})
    generated.add(stage.path('catalog', INDEX_URL).name)
    for path in stage.directory.joinpath('cache').glob('*.json'):
        if path.name not in generated:
            path.unlink()
    write_json(LOCAL_DATA / 'import-report.json', report)
    return report


def apply_staged():
    from zipfile import ZIP_DEFLATED
    backup = LOCAL_DATA / 'before-local-import.zip'
    if not backup.exists():
        with ZipFile(backup, 'w', compression=ZIP_DEFLATED) as archive:
            for path in (ROOT / 'data/cache').glob('*.json'):
                archive.write(path, str(path.relative_to(ROOT / 'data')))
            progress = ROOT / 'data/progress.json'
            if progress.exists():
                archive.write(progress, 'progress.json')
    for path in (LOCAL_DATA / 'staged/cache').glob('*.json'):
        write_json(ROOT / 'data/cache' / path.name, read_json(path, None))
    write_json(ROOT / 'data/reader-format.json', {'cache_format': 2, 'description': 'Local dialogue graphs with explicit jumps'})
    refresh_availability()


def refresh_availability():
    library = Library(ROOT / 'data')
    catalog = read_json(library.path('catalog', INDEX_URL), {})['content']
    summaries, failures, available_urls = [], [], set()
    for chapter in catalog:
        records = read_json(library.path('chapter', chapter['url']), {}).get('content', [])
        available = 0
        for record in records:
            if library.cached('scene', record['url']):
                available += 1
                available_urls.add(record['url'])
            else:
                failures.append({'chapter': chapter['title'], 'scene': record['title'], 'error': '未在本地包中匹配，且没有已下载的网页正文'})
        if not records:
            failures.append({'chapter': chapter['title'], 'error': '本地包未包含此章节，且没有已下载的小节清单'})
        summaries.append({'title': chapter['title'], 'total': len(records) if records else None, 'downloaded': available})
    imported = read_json(LOCAL_DATA / 'import-report.json', {})
    report = {'catalog_chapters': len(catalog), 'downloaded_scenes': len(available_urls),
              'complete_chapters': sum(c['total'] is not None and c['total'] == c['downloaded'] for c in summaries),
              'local_archives': imported.get('archives', 0), 'local_archive_sections': sum(c['source_groups'] for c in imported.get('chapters', [])),
              'chapters': summaries, 'failures': failures, 'updated_at': int(time.time())}
    write_json(ROOT / 'data/offline-download-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument('--stage', action='store_true', help='验证并生成阅读器缓存，暂不替换现有目录')
    parser.add_argument('--apply', action='store_true', help='生成缓存并导入，保留现有网页正文与阅读进度')
    args = parser.parse_args()
    archives = load_archives(args.directory)
    if args.stage or args.apply:
        report = stage_import(archives)
        if report['errors']:
            raise SystemExit('转换存在错误，未应用缓存。请查看 data/local-import/import-report.json')
        if args.apply:
            apply_staged()
        print(f"本地正文：{report['new_scenes']} 个小节；保留 {report['preserved_scenes']} 个已有网页小节", flush=True)
    else:
        report = audit(archives)
        print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in report.items()}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
