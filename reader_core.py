"""BWIKI text import, local storage, and dialogue navigation. Standard library only."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from uuid import uuid4
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = "https://wiki.biligame.com/zspms/"
INDEX_URL = BASE + quote("剧情回顾")


class RateLimitedError(ValueError):
    """The source asks us to pause fetching more pages."""


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def has(self, cls):
        return cls in self.attrs.get("class", "").split()

    def find_all(self, cls):
        for child in self.children:
            if isinstance(child, Node):
                if child.has(cls):
                    yield child
                yield from child.find_all(cls)

    def first(self, cls):
        return next(self.find_all(cls), None)

    def hidden(self):
        return bool(re.search(r"display\s*:\s*none", self.attrs.get("style", ""), re.I))

    def text(self):
        if self.tag in {"style", "script"} or self.hidden():
            return ""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == "br":
                parts.append("\n")
            else:
                parts.append(child.text())
                if child.tag in {"p", "div"}:
                    parts.append("\n")
        return "\n".join(re.sub(r"[\t \xa0]+", " ", line).strip()
                         for line in "".join(parts).splitlines() if line.strip())


class DOM(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def content_root(html):
    root = DOM(html).root.first("mw-parser-output")
    if root is None:
        raise ValueError("未找到 WIKI 正文。网站可能暂时不可用，请稍后重试。")
    return root


def canonical_url(url):
    parts = urlsplit(urljoin(BASE, url.strip()))
    if parts.scheme != "https" or parts.hostname != "wiki.biligame.com" or not parts.path.startswith("/zspms/"):
        raise ValueError("请使用战双 BWIKI 的 https://wiki.biligame.com/zspms/ 链接。")
    return urlunsplit((parts.scheme, parts.netloc, quote(unquote(parts.path), safe="/:"), parts.query, ""))


def ancestors(node):
    while node.parent is not None:
        node = node.parent
        yield node


def usable(node):
    return not node.hidden() and not any(n.hidden() for n in ancestors(node))


def anchors(node):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == "a":
                yield child
            else:
                yield from anchors(child)


def link_record(anchor):
    if "new" in anchor.attrs.get("class", "").split():
        return None
    href = anchor.attrs.get("href", "")
    if not href or href.startswith("#"):
        return None
    try:
        url = canonical_url(href)
    except ValueError:
        return None
    if "action=" in url or "/文件:" in unquote(url):
        return None
    title = anchor.attrs.get("title") or anchor.text().replace("\n", " ")
    return {"title": title, "url": url} if title else None


def tab_parts(node):
    panes = [n for n in node.children if isinstance(n, Node) and n.has("tab_con")]
    labels = []
    for child in node.children:
        if isinstance(child, Node) and child.tag == "ul":
            labels = [n.text() for n in child.children if isinstance(n, Node) and n.has("tab_li")]
            break
    return labels, panes


def parse_catalog(html):
    root = content_root(html)
    result, seen = [], set()
    for card in root.find_all("zs-btnDialogCBox"):
        if not usable(card):
            continue
        record = next((r for a in anchors(card) if (r := link_record(a))), None)
        if not record or record["url"] in seen:
            continue
        groups = []
        for parent in ancestors(card):
            if parent.has("tab_con") and parent.parent:
                labels, panes = tab_parts(parent.parent)
                if parent in panes:
                    index = panes.index(parent)
                    if index < len(labels):
                        groups.append(labels[index].replace("\n", " "))
        record["group"] = " / ".join(reversed(groups)) or "其他剧情"
        seen.add(record["url"])
        result.append(record)
    if not result:
        raise ValueError("没有识别到剧情目录，WIKI 的页面结构可能已改变。")
    return result


def parse_chapter(html):
    root = content_root(html)
    labels = [n.text() for n in root.find_all("zs-cswitchbtn")]
    scenes = []
    seen = set()
    for index, group in enumerate(root.find_all("zs-clist")):
        if not usable(group):
            continue
        for anchor in anchors(group):
            if not any(True for _ in anchor.find_all("zs-btnDialogPlay")):
                continue
            record = link_record(anchor)
            if record and record["url"] not in seen:
                record["group"] = labels[index] if index < len(labels) else "剧情"
                scenes.append(record)
                seen.add(record["url"])
    if not scenes:
        raise ValueError("没有识别到子章。可以粘贴具体关卡的链接直接导入。")
    return scenes


def parse_scene(html):
    root = content_root(html)
    counter = 0

    def walk(node):
        nonlocal counter
        events = []
        for child in node.children:
            if not isinstance(child, Node) or child.hidden() or child.tag in {"script", "style"}:
                continue
            if child.has("TabContainer"):
                labels, panes = tab_parts(child)
                options = []
                for i, pane in enumerate(panes):
                    branch = walk(pane)
                    # An empty branch is still a valid reply or movement action.
                    options.append({"label": labels[i] if i < len(labels) else f"选项 {i + 1}", "events": branch})
                if len(options) == 1:
                    events.append({"kind": "note", "speaker": "情境", "text": options[0]["label"]})
                    events.extend(options[0]["events"])
                elif options:
                    counter += 1
                    events.append({"kind": "choice", "id": str(counter), "options": options})
            elif child.has("zs-DialogBox"):
                body = child.first("zs-DialogContent")
                head = child.first("zs-DialogHead")
                acts = [n.text() for n in child.find_all("zs-DialogAct")]
                text = body.text() if body else "\n".join(text for text in acts if text)
                if text:
                    events.append({"kind": "line" if head else "note", "speaker": head.text() if head else "旁白", "text": text})
            elif child.has("zs-DialogActLeft"):
                if child.text():
                    events.append({"kind": "note", "speaker": "情境", "text": child.text()})
            else:
                events.extend(walk(child))
        return events

    events = walk(root)
    if not events:
        raise ValueError("未识别到角色对话，当前页面可能不是具体关卡或尚未收录。")
    title = root.first("zs-DialogTitle")
    return {"title": title.text() if title else "剧情", "events": events}


def visible_events(events, choices):
    """Stop at an unanswered choice; never mix mutually exclusive branches."""
    if len(events) == 1 and events[0].get("kind") == "graph":
        return visible_graph_events(events[0], choices)
    result = []

    def visit(items):
        for event in items:
            result.append(event)
            if event["kind"] == "choice":
                selected = choices.get(event["id"])
                if not isinstance(selected, int) or not 0 <= selected < len(event["options"]):
                    return False
                if not visit(event["options"][selected]["events"]):
                    return False
        return True

    complete = visit(events)
    return result, complete


def visible_graph_events(graph, choices):
    """Follow a local pack's explicit edges, asking again on a revisited menu."""
    nodes, key = graph["nodes"], graph["start"]
    result, visits = [], {}
    for _ in range(50000):
        if key is None:
            return result, True
        node = nodes[key]
        kind = node["kind"]
        if kind == "end":
            return result, True
        if kind == "choice":
            options = node["options"]
            if len(options) == 1:
                option = options[0]
                target = nodes[option["target"]]
                label = option["label"]
                if re.sub(r"\s+", "", label) != re.sub(r"\s+", "", target.get("text", "")):
                    result.append({"kind": "note", "speaker": "情境", "text": label})
                key = option["target"]
                continue
            visits[key] = visits.get(key, 0) + 1
            choice_id = key if visits[key] == 1 else f"{key}:visit:{visits[key]}"
            event = {"kind": "choice", "id": choice_id,
                     "options": [{"label": option["label"], "events": []} for option in options]}
            result.append(event)
            selected = choices.get(choice_id)
            if not isinstance(selected, int) or not 0 <= selected < len(options):
                return result, False
            key = options[selected]["target"]
            continue
        if kind in {"line", "note"}:
            result.append({"kind": kind, "speaker": node["speaker"], "text": node["text"]})
        key = node.get("next")
    raise ValueError("剧情跳转超过单次阅读上限，请返回此前的选项。")


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Library:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, kind, url):
        return self.directory / "cache" / f"{kind}-{sha256(canonical_url(url).encode()).hexdigest()[:24]}.json"

    def cached(self, kind, url):
        return self.path(kind, url).exists()

    def load(self, kind, url=INDEX_URL, refresh=False):
        url = canonical_url(url)
        path = self.path(kind, url)
        cached = read_json(path, None)
        if cached is not None and not refresh:
            return cached["content"]
        request = Request(url, headers={"User-Agent": "ZS-Local-Reader/0.1 (personal text reader)", "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            with urlopen(request, timeout=25) as response:
                html = response.read(16 * 1024 * 1024).decode("utf-8-sig")
            content = {"catalog": parse_catalog, "chapter": parse_chapter, "scene": parse_scene}[kind](html)
        except Exception as exc:
            if isinstance(exc, HTTPError) and exc.code in {429, 567}:
                raise RateLimitedError("WIKI 暂时限制了网页请求，请稍后再试。已下载内容仍可离线阅读。") from exc
            raise ValueError(f"读取失败：{exc}\n已下载的关卡仍可离线阅读。") from exc
        write_json(path, {"url": url, "saved_at": int(time.time()), "content": content})
        return content

    def seed(self, kind, url, html):
        content = {"catalog": parse_catalog, "chapter": parse_chapter, "scene": parse_scene}[kind](html)
        write_json(self.path(kind, url), {"url": canonical_url(url), "saved_at": int(time.time()), "content": content})
        return content
