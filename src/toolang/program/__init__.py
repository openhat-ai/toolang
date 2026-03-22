"""Program facade.

This package owns parsed Toolang programs. Its public surface stays narrow:
`Program` represents one parsed program document and `parse()` validates source
text into that model.
"""

from .ast import Program
from .parser import parse

__all__ = ["Program", "parse"]
