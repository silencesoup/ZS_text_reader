"""Convert the site's dialogue templates to the reader's existing HTML parser.

Only the batch importer needs mwparserfromhell; the desktop reader remains
standard-library-only. Unknown templates fail closed for a rendered-page import.
"""
from html import escape
import re

import mwparserfromhell as mw
from mwparserfromhell import nodes

from reader_core import parse_scene


class UnsupportedTemplate(ValueError):
    pass


IGNORED = {"剧情", "剧情/一览", "关卡引言", "剧情/导语", "面包屑", "目录", "TOC", "无目录", "视频引用", "引用"}
COLORS = {"灰色", "红色", "蓝色", "绿色", "橙色", "黄色", "白色", "黑色", "紫色"}


def render(code):
    if isinstance(code, str):
        code = mw.parse(code)
    output = []
    for node in code.nodes:
        if isinstance(node, nodes.Template):
            name = str(node.name).strip().replace("Template:", "").replace("模板:", "")

            def arg(key, default=""):
                return render(node.get(str(key)).value) if node.has(str(key)) else default

            if name in IGNORED:
                continue
            if name == "剧情/对话":
                speaker = arg(1).strip()
                head = f'<div class="zs-DialogHead">{speaker}</div>' if speaker else ""
                output.append(f'<div class="zs-DialogBox">{head}<div class="zs-DialogContent">{arg(2)}</div></div>')
            elif name == "人物对话":
                side = arg(1).strip()
                if side in {"开始", "结束"}:
                    continue
                if side == "可用选项":
                    output.append(arg(2))
                elif side in {"左", "右", "左表情", "右表情"}:
                    text = arg(3)
                    if side.endswith("表情"):
                        text = '[表情：' + text + ']'
                    output.append(f'<div class="zs-DialogBox"><div class="zs-DialogHead">{arg(2)}</div>'
                                  f'<div class="zs-DialogContent">{text}</div></div>')
                else:
                    raise UnsupportedTemplate("人物对话/" + side)
            elif name == "剧情/战斗提示":
                main, sub, hint = arg("大标题"), arg("小标题"), arg("提示")
                acts = ''.join(f'<div class="zs-DialogAct">{text}</div>' for text in (main, sub) if text.strip())
                if acts:
                    output.append(f'<div class="zs-DialogBox">{acts}</div>')
                if hint.strip():
                    output.append(f'<div class="zs-DialogActLeft">{hint}</div>')
            elif name == "剧情/选择":
                positional = {int(str(p.name).strip()): p.value for p in node.params
                              if str(p.name).strip().isdigit()}
                labels, panes = [], []
                for index in range(1, max(positional, default=0) + 1, 2):
                    if index not in positional or index + 1 not in positional:
                        raise ValueError("Incomplete choice pair")
                    labels.append(f'<li class="tab_li">{render(positional[index])}</li>')
                    panes.append(f'<div class="tab_con">{render(positional[index + 1])}</div>')
                output.append('<div class="TabContainer"><ul>' + ''.join(labels) + '</ul>' + ''.join(panes) + '</div>')
            elif name in COLORS:
                output.append(arg(1))
            elif name == "颜色":
                output.append(arg(2))
            elif name == "颜色引用":
                output.append(arg(2))
            elif name in {"修正", "黑幕", "特效字", "引言框"}:
                output.append(arg(1))
            elif name == "提示":
                # Page notices are separate from the actual dialogue transcript.
                continue
            elif name == "图片查看按钮":
                output.append('[图片：' + arg(1) + ']')
            elif name == "图标":
                output.append(arg(2))
            elif name == "标注":
                output.append(arg(1) + arg(2))
            elif name == "!":
                output.append('|')
            else:
                raise UnsupportedTemplate(name)
        elif isinstance(node, nodes.Comment):
            continue
        elif isinstance(node, nodes.Tag):
            tag = str(node.tag).lower()
            if tag in {"ref", "references", "style", "script"}:
                continue
            if tag == "nowiki":
                output.append(escape(str(node.contents or "")))
            elif node.wiki_markup:
                output.append(render(node.contents) if node.contents is not None else "")
            elif node.self_closing:
                output.append(str(node))
            else:
                attrs = ''.join(' ' + str(a) for a in node.attributes)
                output.append(f'<{tag}{attrs}>' + render(node.contents) + f'</{tag}>')
        elif isinstance(node, nodes.Wikilink):
            title = str(node.title)
            if title.lower().startswith(("file:", "image:", "文件:", "分类:", "category:")):
                continue
            output.append(render(node.text if node.text is not None else node.title))
        elif isinstance(node, nodes.ExternalLink):
            output.append(render(node.title) if node.title is not None else '')
        elif isinstance(node, nodes.Heading):
            output.append(render(node.title))
        elif isinstance(node, nodes.Argument):
            raise UnsupportedTemplate("unexpanded argument")
        else:
            output.append(str(node))
    return ''.join(output)


def parse_wikitext(title, source):
    code = mw.parse(source)
    headers = [t for t in code.filter_templates() if str(t.name).strip() == "剧情"]
    if headers and headers[0].has("标题"):
        title_html = render(headers[0].get("标题").value)
    else:
        title_html = escape(title.split('/')[-1])
    html = '<div class="mw-parser-output"><div class="zs-DialogTitle">' + title_html + '</div>' + render(code) + '</div>'
    return parse_scene(html)
