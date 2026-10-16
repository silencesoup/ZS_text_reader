"""Download a resumable BWIKI snapshot through its public batch API."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, unquote, urlsplit
from urllib.request import Request, urlopen

from reader_core import Library, RateLimitedError, canonical_url, read_json, write_json

API = "https://wiki.biligame.com/zspms/api.php"
ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "data" / "offline-source"


class SecurityBlockedError(ValueError):
    """The site explicitly denied access; do not automatically retry."""


class Api:
    def __init__(self, directory=SNAPSHOT / "api", interval=10):
        self.directory = Path(directory)
        self.interval = interval
        self.last_request = 0

    def get(self, **parameters):
        parameters = {"format": "json", "maxlag": "5", **parameters}
        query = urlencode(parameters)
        path = self.directory / (sha256(query.encode()).hexdigest() + ".json")
        cached = read_json(path, None)
        if cached is not None:
            return cached
        for attempt in range(4):
            time.sleep(max(0, self.last_request + self.interval - time.monotonic()))
            self.last_request = time.monotonic()
            request = Request(API + "?" + query, headers={
                "User-Agent": "ZS-Local-Reader/0.1 (personal offline text reader)",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            try:
                with urlopen(request, timeout=60) as response:
                    result = json.load(response)
                if "error" in result:
                    if result["error"].get("code") in {"maxlag", "ratelimited"}:
                        raise HTTPError(API, 429, str(result["error"]), {}, None)
                    raise ValueError(result["error"])
                write_json(path, result)
                return result
            except HTTPError as error:
                error_body = error.read().decode("utf-8", errors="replace")
                write_json(self.directory / "last-error.json", {"status": error.code,
                           "time": int(time.time()), "body": error_body[:8000]})
                if error.code == 567:
                    raise SecurityBlockedError("BWIKI 安全策略拦截（HTTP 567），已停止网络请求，保留下载断点。") from error
                if error.code not in {429, 503} or attempt == 3:
                    raise
                delay = max(int(error.headers.get("Retry-After", "0")), 60 * (attempt + 1))
                print(f"网站要求暂停 (HTTP {error.code})，等待 {delay} 秒后重试 ({attempt + 1}/3)", flush=True)
                time.sleep(delay)
        raise RuntimeError("API request failed")

    def revisions(self, titles):
        result = self.get(action="query", prop="revisions", rvprop="content|ids",
                          rvslots="main", titles="|".join(titles), redirects="1")
        if "continue" in result:
            raise ValueError("Revision batch truncated; reduce batch size")
        return result["query"]["pages"].values()


def page_title(url):
    return unquote(urlsplit(url).path.removeprefix("/zspms/")).replace("_", " ")


def source_path(title):
    return SNAPSHOT / "pages" / (sha256(title.encode()).hexdigest() + ".json")


def fetch_sources(api, catalog):
    inventory = {}
    offset = 0
    while True:
        result = api.get(action="ask", query=(
            "[[分类:剧情]]|?章节|?剧情分类|?序号|limit=500|offset=" + str(offset)))
        inventory.update(result["query"]["results"])
        print(f"已列出 {len(inventory)} 个剧情小节", flush=True)
        following = result.get("query-continue-offset")
        if following is None:
            break
        if int(following) <= offset:
            raise ValueError("Inventory pagination did not advance")
        offset = int(following)
    write_json(SNAPSHOT / "inventory.json", inventory)
    titles = list(dict.fromkeys([page_title(c["url"]) for c in catalog] + list(inventory)))
    pending = [title for title in titles if not source_path(title).exists()]
    done = len(titles) - len(pending)
    for start in range(0, len(pending), 50):
        batch = pending[start:start + 50]
        pages = list(api.revisions(batch))
        by_title = {}
        for page in pages:
            if "revisions" not in page:
                raise ValueError(f"Missing source: {page}")
            revision = page["revisions"][0]
            source = {"title": page["title"], "revision": revision["revid"],
                      "wikitext": revision["slots"]["main"]["*"]}
            write_json(source_path(page["title"]), source)
            by_title[page["title"]] = source
        # Redirect aliases retain the originally requested cache key as well.
        result = api.get(action="query", prop="revisions", rvprop="content|ids",
                         rvslots="main", titles="|".join(batch), redirects="1")
        aliases = {r["from"]: r["to"] for r in result["query"].get("redirects", [])}
        aliases.update({r["from"]: r["to"] for r in result["query"].get("normalized", [])})
        for title in batch:
            resolved = title
            seen = set()
            while resolved in aliases and resolved not in seen:
                seen.add(resolved)
                resolved = aliases[resolved]
            if resolved in by_title:
                write_json(source_path(title), by_title[resolved])
            elif not source_path(title).exists():
                raise ValueError(f"API did not return {title}")
        done += len(batch)
        write_json(SNAPSHOT / "status.json", {"stage": "sources", "done": done,
                   "total": len(titles), "updated_at": int(time.time())})
        print(f"原始剧情下载 {done}/{len(titles)}", flush=True)
    return inventory


def cache_content(library, kind, url, content):
    write_json(library.path(kind, url), {"url": canonical_url(url),
               "saved_at": int(time.time()), "content": content})


def chapter_from_source(source, inventory):
    import mwparserfromhell as mw
    headers = [t for t in mw.parse(source).filter_templates()
               if str(t.name).strip() == "剧情章节"]
    if len(headers) != 1:
        raise ValueError("特殊章节结构，需要原网页解析")
    header = headers[0]
    def param(name):
        return str(header.get(name).value).strip() if header.has(name) else ""
    title, category = param("章节"), param("分类")
    categories = [(category, "普通剧情" if any(param(p) for p in (
        "切换分类", "预热剧情", "预热剧情1", "源望契约")) else "剧情")]
    for key in ("切换分类", "预热剧情", "预热剧情1", "源望契约"):
        if param(key):
            categories.append((param(key), "隐藏剧情" if key == "切换分类" else param(key)))
    result, seen = [], set()
    for category, label in categories:
        records = [r for r in inventory.values() if title in r["printouts"]["章节"]
                   and category in r["printouts"]["剧情分类"]]
        records.sort(key=lambda r: (r["printouts"]["序号"] or [float("inf")])[0])
        for record in records:
            url = canonical_url(record["fullurl"])
            if url not in seen:
                result.append({"title": record["fulltext"], "url": url, "group": label})
                seen.add(url)
    if not result:
        raise ValueError("语义清单中没有匹配的子章")
    return result


def is_direct_scene(source):
    import mwparserfromhell as mw
    return any(str(t.name).strip() == "剧情" for t in mw.parse(source).filter_templates(recursive=False))


def convert_snapshot(catalog, inventory, api, allow_network=False, reparse=False):
    from wikitext_import import parse_wikitext
    library = Library(ROOT / "data")
    failures, chapters = [], []
    network_stopped = not allow_network

    def rendered(kind, url):
        nonlocal network_stopped
        if network_stopped:
            raise ValueError("尚未下载原文")
        time.sleep(max(0, api.last_request + api.interval - time.monotonic()))
        api.last_request = time.monotonic()
        try:
            return library.load(kind, url)
        except RateLimitedError:
            network_stopped = True
            raise

    for chapter in catalog:
        url = chapter["url"]
        try:
            if library.cached("chapter", url):
                scenes = library.load("chapter", url)
            else:
                source = read_json(source_path(page_title(url)), None)
                try:
                    if source is None:
                        raise ValueError("尚未下载章节原文")
                    if is_direct_scene(source["wikitext"]):
                        scenes = [{"title": chapter["title"], "url": url, "group": "剧情"}]
                    else:
                        scenes = chapter_from_source(source["wikitext"], inventory)
                    cache_content(library, "chapter", url, scenes)
                except ValueError:
                    scenes = rendered("chapter", url)
            complete = 0
            for scene in scenes:
                try:
                    if reparse or not library.cached("scene", scene["url"]):
                        source = read_json(source_path(page_title(scene["url"])), None)
                        try:
                            if source is None:
                                raise ValueError("尚未下载小节原文")
                            content = parse_wikitext(source["title"], source["wikitext"])
                            cache_content(library, "scene", scene["url"], content)
                        except ValueError:
                            if not library.cached("scene", scene["url"]):
                                rendered("scene", scene["url"])
                    library.load("scene", scene["url"])
                    complete += 1
                except ValueError as error:
                    failures.append({"chapter": chapter["title"], "scene": scene["title"], "error": str(error)})
            chapters.append({"title": chapter["title"], "downloaded": complete, "total": len(scenes)})
        except ValueError as error:
            chapters.append({"title": chapter["title"], "downloaded": 0, "total": None})
            failures.append({"chapter": chapter["title"], "error": str(error)})
    report = {"catalog_chapters": len(catalog), "inventory_scenes": len(inventory),
              "source_pages": len(list((SNAPSHOT / "pages").glob("*.json"))),
              "complete_chapters": sum(c["total"] is not None and c["total"] == c["downloaded"] for c in chapters),
              "downloaded_scenes": sum(c["downloaded"] for c in chapters),
              "chapters": chapters, "failures": failures, "updated_at": int(time.time())}
    write_json(ROOT / "data" / "offline-download-report.json", report)
    # Only caches and the report are copied. Never touch reading progress or logs.
    required_format = read_json(ROOT / 'data/reader-format.json', {}).get('cache_format', 1)
    for data_dir in (ROOT / "dist").glob("*/data"):
        if read_json(data_dir / 'reader-format.json', {}).get('cache_format', 1) < required_format:
            continue
        for path in (ROOT / "data" / "cache").glob("*.json"):
            target = data_dir / "cache" / path.name
            previous = read_json(target, {})
            incoming = read_json(path, {})
            if not target.exists() or incoming.get("saved_at", 0) > previous.get("saved_at", 0):
                write_json(target, incoming)
        write_json(data_dir / "offline-download-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-only", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="仅转换已取得的数据，不访问网络")
    parser.add_argument("--reparse", action="store_true", help="重新解析已保存的原文，不删除原有缓存")
    args = parser.parse_args()
    catalog = Library(ROOT / "data").load("catalog")
    api = Api()
    inventory = read_json(SNAPSHOT / "inventory.json", {})
    blocked = False
    if not args.cache_only:
        try:
            inventory = fetch_sources(api, catalog)
        except (SecurityBlockedError, HTTPError, OSError, ValueError) as error:
            print(str(error), flush=True)
            inventory = read_json(SNAPSHOT / "inventory.json", inventory)
            blocked = True
    if not args.sources_only:
        report = convert_snapshot(catalog, inventory, api, not args.cache_only and not blocked, args.reparse)
        print(f"离线数据：{report['complete_chapters']}/{len(catalog)} 章完整，"
              f"{report['downloaded_scenes']} 个小节可读。详见 data/offline-download-report.json", flush=True)
    elif not blocked:
        print(f"原始数据下载完成：{len(catalog)} 章，{len(inventory)} 个小节", flush=True)
    if blocked:
        raise SystemExit(2)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
