"""Rendu Markdown → HTML des rapports.

Les rapports contiennent du texte venu de pages web (titres, citations) : le HTML brut est
désactivé, donc tout est échappé, et markdown-it refuse les liens `javascript:` / `data:`.
"""

from __future__ import annotations

from markdown_it import MarkdownIt


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
    return self.renderToken(tokens, idx, options, env)


_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_MD.add_render_rule("link_open", _link_open)


def markdown_to_html(text: str) -> str:
    return _MD.render(text)
