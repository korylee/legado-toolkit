# -*- coding: utf-8 -*-
"""BeautifulSoup 解析器选择：优先 lxml，缺失时回退 html.parser。"""

from __future__ import annotations

from typing import Optional

_PARSER: Optional[str] = None


def _best_parser() -> str:
    global _PARSER
    if _PARSER is None:
        from bs4 import BeautifulSoup
        for parser in ("lxml", "html.parser"):
            try:
                BeautifulSoup("<p>x</p>", parser)
                _PARSER = parser
                break
            except Exception:
                continue
        if _PARSER is None:
            _PARSER = "html.parser"
    return _PARSER


def make_soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", _best_parser())
