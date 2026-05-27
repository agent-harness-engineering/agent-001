"""Parsers sub-package — one module per model output format."""

from .openai_parser import parse_openai
from .anthropic_parser import parse_anthropic
from .action_tag_parser import parse_action_tags

__all__ = ["parse_openai", "parse_anthropic", "parse_action_tags"]
