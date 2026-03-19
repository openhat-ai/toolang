"""Toolang package."""

from .ast import Program
from .parser import parse_program

__all__ = ["Program", "parse_program"]
__version__ = "0.1.0"
