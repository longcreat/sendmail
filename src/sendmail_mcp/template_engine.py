"""模板渲染辅助模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, StrictUndefined


@dataclass(slots=True)
class RenderedContent:
    subject: str
    text_body: str | None
    html_body: str | None


class TemplateEngine:
    """使用严格未定义变量策略的 Jinja2 渲染器。"""

    def __init__(self):
        self.env = Environment(undefined=StrictUndefined, autoescape=False)

    def render(
        self,
        *,
        subject_tpl: str,
        text_tpl: str | None,
        html_tpl: str | None,
        variables: dict[str, Any],
    ) -> RenderedContent:
        subject = self.env.from_string(subject_tpl).render(**variables)
        text_body = (
            self.env.from_string(text_tpl).render(**variables) if text_tpl is not None else None
        )
        html_body = (
            self.env.from_string(html_tpl).render(**variables) if html_tpl is not None else None
        )
        return RenderedContent(subject=subject, text_body=text_body, html_body=html_body)
