"""Syntax facade.

This package owns Toolang parsing, syntax data structures, and syntax-level
validation of parsed programs.
"""

from .analyze import analyze_program
from .ast import DeclBlock, ParamDecl, Program, SourceSpan, Thunk, UseDecl
from .parser import parse_program

__all__ = [
    "DeclBlock",
    "ParamDecl",
    "Program",
    "SourceSpan",
    "Thunk",
    "UseDecl",
    "analyze_program",
    "parse_program",
]
