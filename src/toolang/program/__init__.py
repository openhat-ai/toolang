"""Program facade.

This package owns parsed Toolang programs. Its public surface stays narrow:
`Program` represents one parsed program document, including file-backed source
editing and persistence, and `parse()` turns source text into that model.
"""

from .ast import Program
from .parser import parse

__all__ = ["Program", "parse"]
