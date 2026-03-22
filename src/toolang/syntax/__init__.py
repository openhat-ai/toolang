"""Syntax facade.

This package owns Toolang parsing, syntax data structures, and syntax-level
validation of parsed programs, including authored source edits.
"""

from .analyze import analyze_program
from .ast import DeclBlock, ParamDecl, Program, SourceSpan, Thunk, UseDecl
from .parser import parse_program
from .source_ops import add_cap_ref, cap_name_from_ref, remove_cap_ref

__all__ = [
    "DeclBlock",
    "ParamDecl",
    "Program",
    "SourceSpan",
    "Thunk",
    "UseDecl",
    "add_cap_ref",
    "analyze_program",
    "cap_name_from_ref",
    "parse_program",
    "remove_cap_ref",
]
