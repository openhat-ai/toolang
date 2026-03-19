from __future__ import annotations

import ctypes
import hashlib
import importlib.resources as resources
import os
import platform
import shutil
import subprocess
import sysconfig
import tempfile
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter import Language, Parser, Tree

from toolang.errors import ToolangError

_GRAMMAR_PACKAGE = "toolang._vendor.tree_sitter_toolang"
_LANGUAGE_SYMBOL = "tree_sitter_toolang"


@dataclass(frozen=True, slots=True)
class LoadedTreeSitter:
    library: ctypes.CDLL
    language: Language


def parse_tree(source: bytes) -> Tree:
    runtime = load_toolang_language()
    parser = Parser(runtime.language)
    return parser.parse(source)


@lru_cache(maxsize=1)
def load_toolang_language() -> LoadedTreeSitter:
    parser_resource = resources.files(_GRAMMAR_PACKAGE) / "parser.c"
    header_resource = resources.files(_GRAMMAR_PACKAGE) / "tree_sitter"

    with resources.as_file(parser_resource) as parser_path, resources.as_file(
        header_resource
    ) as header_path:
        shared_library = _ensure_shared_library(parser_path, header_path)

    library = ctypes.CDLL(str(shared_library))
    symbol = getattr(library, _LANGUAGE_SYMBOL)
    symbol.restype = ctypes.c_void_p

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="int argument support is deprecated",
            category=DeprecationWarning,
        )
        language = Language(symbol())

    return LoadedTreeSitter(library=library, language=language)


def _ensure_shared_library(parser_path: Path, header_path: Path) -> Path:
    output_path = _shared_library_path(parser_path, header_path)
    if output_path.exists():
        return output_path

    compiler = shutil.which("cc")
    if compiler is None:
        raise ToolangError("Unable to load Toolang grammar: no C compiler named 'cc' found.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f"{output_path.name}.{os.getpid()}.tmp")

    try:
        subprocess.run(
            [
                compiler,
                "-shared",
                "-fPIC",
                "-O2",
                "-I",
                str(parser_path.parent),
                str(parser_path),
                "-o",
                str(temporary_output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        raise ToolangError(
            f"Unable to compile bundled Toolang grammar: {stderr or exc}"
        ) from exc

    temporary_output.replace(output_path)
    return output_path


def _shared_library_path(parser_path: Path, header_path: Path) -> Path:
    digest = hashlib.sha256()
    digest.update(parser_path.read_bytes())
    digest.update((header_path / "parser.h").read_bytes())
    digest.update(platform.system().encode("utf-8"))
    digest.update(platform.machine().encode("utf-8"))

    suffix = sysconfig.get_config_var("SHLIB_SUFFIX") or ".so"
    filename = f"toolang-{digest.hexdigest()[:16]}{suffix}"
    return Path(tempfile.gettempdir()) / "toolang" / "tree-sitter" / filename
