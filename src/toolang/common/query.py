"""Typed queries over ordered collection snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType, UnionType
from typing import (
    Any,
    Generic,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from .errors import ToolangError

QueryKind = Literal[
    "bool", "text", "enum", "integer", "float", "decimal", "date", "datetime"
]
QueryOperator = Literal["=", "!=", "~=", "!~=", "<", "<=", ">", ">=", "in", "not in"]
SetOperator = Literal["=", "+=", "-="]
FieldSource = Literal["item", "overlay"]
ColumnFormatter = Literal[
    "text",
    "bool",
    "integer",
    "join",
    "identity",
    "identity-component",
    "bool-labels",
    "pair",
    "ratio",
    "env",
    "truncate",
]
ScalarValue = str | bool | int | float | Decimal | date | datetime | None

_T = TypeVar("_T")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_BARE_IDENTITY_RE = re.compile(r"^[^\s\[\],;()\"]+$")
_BARE_LITERAL_RE = re.compile(r"^[^\s\[\],;()\"]+$")
_COMPARATORS = ("!~=", "<=", ">=", "!=", "~=", "=", "<", ">")
_NEGATIVE_OPERATORS = frozenset({"!=", "!~=", "not in"})


@dataclass(frozen=True, slots=True)
class IdentitySpec:
    """Public identity components and matching rules for one collection."""

    paths: tuple[str, ...]
    labels: tuple[str, ...]
    separator: str | None = None
    bound: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        paths = tuple(self.paths)
        labels = tuple(self.labels)
        bound = tuple(self.bound)
        if not paths:
            raise ToolangError("collection identity must declare at least one path")
        if len(labels) != len(bound) + len(paths):
            raise ToolangError(
                "collection identity labels must describe bound and item components"
            )
        if len(labels) > 1 and not self.separator:
            raise ToolangError("multi-component identity requires a separator")
        if self.separator == "":
            raise ToolangError("identity separator cannot be empty")
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "bound", bound)


@dataclass(frozen=True, slots=True)
class QueryField:
    """One compiled, typed public query field."""

    name: str
    path: tuple[str, ...]
    kind: QueryKind
    nullable: bool = False
    multiple: bool = False
    choices: tuple[ScalarValue, ...] = ()
    source: FieldSource = "item"
    description: str = ""

    def __post_init__(self) -> None:
        if not _FIELD_RE.fullmatch(self.name):
            raise ToolangError(f"invalid query field name: {self.name!r}")
        if not self.path:
            raise ToolangError(f"query field {self.name!r} has no value path")
        if self.kind == "enum" and not self.choices:
            raise ToolangError(f"enum query field {self.name!r} has no choices")
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "choices", tuple(self.choices))

    @property
    def operators(self) -> tuple[QueryOperator, ...]:
        """Operators accepted by this field in deterministic display order."""

        if self.kind == "bool":
            return ("=", "!=", "in", "not in")
        if self.kind == "text":
            return ("=", "!=", "~=", "!~=", "in", "not in")
        if self.kind == "enum":
            return ("=", "!=", "in", "not in")
        return ("=", "!=", "<", "<=", ">", ">=", "in", "not in")

    @property
    def type_name(self) -> str:
        """Stable human and machine-readable field type name."""

        value = self.kind
        if self.multiple:
            value = f"sequence[{value}]"
        if self.nullable:
            value = f"{value} | null"
        return value


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One human table column and all public fields backing it."""

    label: str
    fields: tuple[str, ...]
    formatter: ColumnFormatter = "text"

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ToolangError("collection column label cannot be empty")
        if not self.fields:
            raise ToolangError(
                f"collection column {self.label!r} has no backing fields"
            )
        allowed = {
            "text",
            "bool",
            "integer",
            "join",
            "identity",
            "identity-component",
            "bool-labels",
            "pair",
            "ratio",
            "env",
            "truncate",
        }
        if self.formatter not in allowed:
            raise ToolangError(
                f"collection column {self.label!r} has unknown formatter "
                f"{self.formatter!r}"
            )
        object.__setattr__(self, "fields", tuple(self.fields))


@dataclass(frozen=True, slots=True)
class QueryPredicate:
    """One validated typed predicate."""

    field: QueryField
    operator: QueryOperator
    values: tuple[ScalarValue, ...]


@dataclass(frozen=True, slots=True)
class QuerySelector:
    """One identity alternative with conjunctive predicates."""

    identity_pattern: str = "*"
    identity_exact: bool = False
    predicates: tuple[QueryPredicate, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionQuery:
    """A validated disjunction of collection selectors."""

    selectors: tuple[QuerySelector, ...]

    def __post_init__(self) -> None:
        if not self.selectors:
            raise ToolangError("collection query must contain at least one selector")
        object.__setattr__(self, "selectors", tuple(self.selectors))


@dataclass(frozen=True, slots=True)
class CollectionSchema(Generic[_T]):
    """Compiled public query contract for one collection item type."""

    name: str
    item_type: type[_T]
    key_paths: tuple[tuple[str, ...], ...]
    identity: IdentitySpec
    fields: Mapping[str, QueryField]
    columns: tuple[ColumnSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ToolangError("collection schema name cannot be empty")
        key_paths = tuple(tuple(path) for path in self.key_paths)
        if not key_paths or any(not path for path in key_paths):
            raise ToolangError(f"collection {self.name!r} must declare item-key paths")
        ordered_fields = dict(self.fields)
        for name, field in ordered_fields.items():
            if name != field.name:
                raise ToolangError(
                    f"query field mapping key {name!r} does not match {field.name!r}"
                )
        for column in self.columns:
            unknown = [name for name in column.fields if name not in ordered_fields]
            if unknown:
                raise ToolangError(
                    f"column {column.label!r} references unknown query fields: "
                    + ", ".join(unknown)
                )
            _validate_column(column, ordered_fields, identity=self.identity)
        object.__setattr__(self, "key_paths", key_paths)
        object.__setattr__(self, "fields", MappingProxyType(ordered_fields))
        object.__setattr__(self, "columns", tuple(self.columns))

    @classmethod
    def from_type(
        cls,
        name: str,
        item_type: type[_T],
        *,
        key: str | Sequence[str],
        identity: IdentitySpec,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        rename: Mapping[str, str] | None = None,
        finite_paths: Mapping[str, Any] | None = None,
        overlay_types: Mapping[str, Any] | None = None,
        columns: Sequence[ColumnSpec] = (),
    ) -> CollectionSchema[_T]:
        """Compile query fields from an explicitly public typed item view."""

        excluded = set(exclude)
        compiled = _compile_public_fields(
            item_type,
            finite_paths=finite_paths or {},
            excluded=excluded,
        )
        if include is not None:
            requested = tuple(include)
            missing = [field for field in requested if field not in compiled]
            if missing:
                raise ToolangError(
                    f"collection {name!r} includes unknown typed fields: "
                    + ", ".join(missing)
                )
            selected = [(field, compiled[field]) for field in requested]
        else:
            selected = list(compiled.items())
        selected = [(field, spec) for field, spec in selected if field not in excluded]
        renamed = dict(rename or {})
        unknown_renames = sorted(
            set(renamed).difference(field for field, _ in selected)
        )
        if unknown_renames:
            raise ToolangError(
                f"collection {name!r} renames unknown typed fields: "
                + ", ".join(unknown_renames)
            )
        query_fields: dict[str, QueryField] = {}
        for source_name, spec in selected:
            public_name = renamed.get(source_name, source_name)
            if public_name in query_fields:
                raise ToolangError(
                    f"collection {name!r} declares duplicate query field "
                    f"{public_name!r}"
                )
            query_fields[public_name] = QueryField(
                name=public_name,
                path=tuple(source_name.split(".")),
                kind=spec.kind,
                nullable=spec.nullable,
                multiple=spec.multiple,
                choices=spec.choices,
            )
        for overlay_name, annotation in (overlay_types or {}).items():
            if overlay_name in query_fields:
                raise ToolangError(
                    f"collection {name!r} declares duplicate query field "
                    f"{overlay_name!r}"
                )
            spec = _compile_scalar_annotation(annotation, field=overlay_name)
            query_fields[overlay_name] = QueryField(
                name=overlay_name,
                path=tuple(overlay_name.split(".")),
                kind=spec.kind,
                nullable=spec.nullable,
                multiple=spec.multiple,
                choices=spec.choices,
                source="overlay",
            )
        key_values = (key,) if isinstance(key, str) else tuple(key)
        return cls(
            name=name,
            item_type=item_type,
            key_paths=tuple(tuple(value.split(".")) for value in key_values),
            identity=identity,
            fields=query_fields,
            columns=tuple(columns),
        )

    def parse(self, values: str | Sequence[str]) -> CollectionQuery:
        """Parse one or more repeated query values as alternatives."""

        raw_values = (values,) if isinstance(values, str) else tuple(values)
        if not raw_values:
            raise ToolangError("collection query cannot be empty")
        selectors: list[QuerySelector] = []
        for raw in raw_values:
            if not isinstance(raw, str):
                raise TypeError("collection query values must be strings")
            selectors.extend(_parse_query_value(raw, self))
        return CollectionQuery(tuple(selectors))

    def item_key(self, item: _T) -> Hashable:
        """Return the configured stable key for an item."""

        values = tuple(_read_path(item, path) for path in self.key_paths)
        key: object = values[0] if len(values) == 1 else values
        if not isinstance(key, Hashable):
            raise ToolangError(
                f"collection {self.name!r} item key is not hashable: {key!r}"
            )
        return key

    def identity_components(self, item: _T) -> tuple[str, ...]:
        """Return normalized public identity components for an item."""

        values: list[str] = list(self.identity.bound)
        for path in self.identity.paths:
            value = _read_path(item, tuple(path.split(".")))
            if not isinstance(value, str) or not value:
                raise ToolangError(
                    f"collection {self.name!r} identity field {path!r} "
                    "must contain non-empty text"
                )
            values.append(value)
        return tuple(values)

    def identity_for(self, item: _T) -> str:
        """Return the canonical public identity for an item."""

        components = self.identity_components(item)
        if len(components) == 1:
            return components[0]
        separator = self.identity.separator
        assert separator is not None
        return separator.join(components)

    def exact_selector_for(self, item: _T) -> str:
        """Format a selector that exactly identifies this public identity."""

        return format_identity(self.identity_for(item), exact=True)

    def to_data(self) -> dict[str, object]:
        """Return a deterministic machine-readable query schema."""

        return {
            "collection": self.name,
            "identity": {
                "labels": list(self.identity.labels),
                "separator": self.identity.separator,
                "bound": list(self.identity.bound),
                "matching": {
                    "bare": "case-sensitive glob",
                    "quoted": "exact",
                    "unqualified": "final component",
                },
            },
            "fields": [
                {
                    "name": field.name,
                    "type": field.type_name,
                    "operators": list(field.operators),
                    "choices": [_json_value(value) for value in field.choices],
                    "source": field.source,
                    "description": field.description,
                }
                for field in self.fields.values()
            ],
            "columns": [
                {
                    "label": column.label,
                    "fields": list(column.fields),
                    "formatter": column.formatter,
                }
                for column in self.columns
            ],
        }

    def to_json(self) -> str:
        """Return deterministic compact query-schema JSON."""

        return json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"))

    def help_text(self) -> str:
        """Return concise human-readable identity, field, and column help."""

        labels = (
            self.identity.labels[0]
            if len(self.identity.labels) == 1
            else (self.identity.separator or "").join(self.identity.labels)
        )
        lines = [
            f"Collection: {self.name}",
            f"Identity: {labels} (bare glob or JSON-quoted exact value)",
            "Fields:",
        ]
        for field in self.fields.values():
            choices = ""
            if field.choices:
                choices = " values=" + ",".join(
                    format_literal(value) for value in field.choices
                )
            lines.append(
                f"  {field.name}: {field.type_name}; "
                f"operators={','.join(field.operators)}{choices}"
            )
        if self.columns:
            lines.append("Columns:")
            for column in self.columns:
                lines.append(f"  {column.label}: {', '.join(column.fields)}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CollectionDefinition(Generic[_T]):
    """Reusable collection schema with optional presentation metadata."""

    schema: CollectionSchema[_T]

    def dataset(
        self,
        items: Sequence[_T],
        *,
        overlays: Mapping[Hashable, Mapping[str, object]] | None = None,
        complete: bool = True,
    ) -> QueryDataset[_T]:
        """Bind the definition to one immutable base snapshot."""

        return QueryDataset(self.schema, items, overlays=overlays, complete=complete)


class QueryDataset(Generic[_T]):
    """A validated ordered base snapshot and its query schema."""

    __slots__ = ("schema", "items", "_keys", "_overlays")

    def __init__(
        self,
        schema: CollectionSchema[_T],
        items: Sequence[_T],
        *,
        overlays: Mapping[Hashable, Mapping[str, object]] | None = None,
        complete: bool = True,
    ) -> None:
        if not complete:
            raise ToolangError(
                f"collection {schema.name!r} cannot be queried from a partial snapshot"
            )
        self.schema = schema
        self.items = tuple(items)
        self._overlays = dict(overlays or {})
        keys: list[Hashable] = []
        seen: set[Hashable] = set()
        for item in self.items:
            item_matches_type = (
                isinstance(item, Mapping)
                if is_typeddict(schema.item_type)
                else isinstance(item, schema.item_type)
            )
            if not item_matches_type:
                raise ToolangError(
                    f"collection {schema.name!r} expected {schema.item_type.__name__} "
                    f"items, got {type(item).__name__}"
                )
            key = schema.item_key(item)
            if key in seen:
                raise ToolangError(
                    f"collection {schema.name!r} contains duplicate item key {key!r}"
                )
            seen.add(key)
            keys.append(key)
            schema.identity_components(item)
            for field in schema.fields.values():
                values = self._field_values(item, key, field)
                for value in values:
                    _validate_runtime_value(field, value)
        unknown_overlays = set(self._overlays).difference(seen)
        if unknown_overlays:
            value = sorted(map(repr, unknown_overlays))[0]
            raise ToolangError(
                f"collection {schema.name!r} has overlay for unknown item key {value}"
            )
        self._keys = tuple(keys)

    def query(
        self,
        query: CollectionQuery | str | Sequence[str] | None = None,
    ) -> tuple[_T, ...]:
        """Return a stable base-order subsequence accepted by the query."""

        if query is None:
            return self.items
        parsed = (
            query if isinstance(query, CollectionQuery) else self.schema.parse(query)
        )
        return tuple(
            item
            for key, item in zip(self._keys, self.items, strict=True)
            if any(
                self._selector_matches(item, key, selector)
                for selector in parsed.selectors
            )
        )

    def require_one(
        self,
        query: CollectionQuery | str | Sequence[str],
        *,
        label: str | None = None,
    ) -> _T:
        """Resolve a singular query or report zero/ambiguous matches."""

        matches = self.query(query)
        subject = label or self.schema.name.rstrip("s") or "item"
        if not matches:
            raise ToolangError(f"{subject} query matched no items")
        if len(matches) > 1:
            identities = ", ".join(
                self.schema.identity_for(item) for item in matches[:5]
            )
            suffix = " ..." if len(matches) > 5 else ""
            raise ToolangError(
                f"{subject} query is ambiguous; matched {len(matches)} items: "
                f"{identities}{suffix}"
            )
        return matches[0]

    def require_each(
        self,
        queries: Sequence[str],
        *,
        label: str | None = None,
    ) -> None:
        """Require every repeated query value to match at least one item."""

        missing = [raw for raw in queries if not self.query(raw)]
        if missing:
            subject = label or self.schema.name.rstrip("s") or "item"
            raise ToolangError(
                f"{subject} query matched no items: {', '.join(missing)}"
            )

    def apply(
        self,
        operations: Sequence[tuple[SetOperator, CollectionQuery | str | Sequence[str]]],
        *,
        active: Sequence[_T] | None = None,
    ) -> tuple[_T, ...]:
        """Apply restrict/include/exclude directives against the immutable base."""

        base_keys = set(self._keys)
        if active is None:
            active_keys = set(base_keys)
        else:
            active_keys = {
                self.schema.item_key(item)
                for item in active
                if self.schema.item_key(item) in base_keys
            }
        for operator, query in operations:
            matched_keys = {self.schema.item_key(item) for item in self.query(query)}
            if operator == "=":
                active_keys.intersection_update(matched_keys)
            elif operator == "+=":
                active_keys.update(matched_keys)
            elif operator == "-=":
                active_keys.difference_update(matched_keys)
            else:
                raise ToolangError(f"unknown collection set operator: {operator!r}")
        return tuple(
            item
            for key, item in zip(self._keys, self.items, strict=True)
            if key in active_keys
        )

    def table(
        self,
        items: Sequence[_T] | None = None,
        *,
        truncate_at: int = 120,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        """Format configured columns from the same public query values."""

        if truncate_at < 4:
            raise ToolangError("table truncation width must be at least 4")
        selected = self.items if items is None else tuple(items)
        base_keys = set(self._keys)
        rows: list[tuple[str, ...]] = []
        for item in selected:
            key = self.schema.item_key(item)
            if key not in base_keys:
                raise ToolangError(
                    f"collection {self.schema.name!r} table item is outside its dataset"
                )
            rows.append(
                tuple(
                    self._format_column(
                        item,
                        key,
                        column,
                        truncate_at=truncate_at,
                    )
                    for column in self.schema.columns
                )
            )
        return (
            tuple(column.label for column in self.schema.columns),
            tuple(rows),
        )

    def _format_column(
        self,
        item: _T,
        key: Hashable,
        column: ColumnSpec,
        *,
        truncate_at: int,
    ) -> str:
        fields = tuple(self.schema.fields[name] for name in column.fields)
        values = tuple(self._field_values(item, key, field) for field in fields)
        formatter = column.formatter
        if formatter == "identity":
            return self.schema.identity_for(item)
        if formatter in {"text", "identity-component"}:
            return _format_display_scalar(values[0][0])
        if formatter == "bool":
            value = values[0][0]
            return "-" if value is None else "yes" if value is True else "no"
        if formatter == "integer":
            value = values[0][0]
            return "-" if value is None else f"{value:_}"
        if formatter == "join":
            return ",".join(_format_display_scalar(value) for value in values[0]) or "-"
        if formatter == "bool-labels":
            labels = [
                field.name
                for field, field_values in zip(fields, values, strict=True)
                if field_values[0] is True
            ]
            return ",".join(labels) or "-"
        if formatter == "pair":
            return " / ".join(_format_display_scalar(value[0]) for value in values)
        if formatter == "ratio":
            return f"{_format_display_scalar(values[0][0])}/{_format_display_scalar(values[1][0])}"
        if formatter == "env":
            missing = set(values[1])
            labels = [
                f"{value} (missing)" if value in missing else str(value)
                for value in values[0]
            ]
            return ", ".join(labels) or "-"
        if formatter == "truncate":
            text = _format_display_scalar(values[0][0])
            if len(text) <= truncate_at:
                return text
            return f"{text[: truncate_at - 3].rstrip()}..."
        raise AssertionError(f"unhandled column formatter: {formatter}")

    def _selector_matches(
        self,
        item: _T,
        key: Hashable,
        selector: QuerySelector,
    ) -> bool:
        components = self.schema.identity_components(item)
        if not _identity_matches(components, self.schema.identity, selector):
            return False
        return all(
            _predicate_matches(
                self._field_values(item, key, predicate.field), predicate
            )
            for predicate in selector.predicates
        )

    def _field_values(
        self,
        item: _T,
        key: Hashable,
        field: QueryField,
    ) -> tuple[ScalarValue, ...]:
        if field.source == "overlay":
            overlay = self._overlays.get(key)
            if overlay is None:
                raise ToolangError(
                    f"collection {self.schema.name!r} item {key!r} is missing query overlay"
                )
            value = _read_path(overlay, field.path)
        else:
            value = _read_path(item, field.path)
        if field.multiple:
            if value is None and field.nullable:
                return (None,)
            if isinstance(value, (str, bytes, bytearray)) or not isinstance(
                value, Sequence
            ):
                if not isinstance(value, (set, frozenset)):
                    raise ToolangError(
                        f"query field {field.name!r} expected a scalar sequence"
                    )
            return tuple(_normalize_runtime_value(item) for item in value)
        return (_normalize_runtime_value(value),)


@dataclass(frozen=True, slots=True)
class _FieldType:
    kind: QueryKind
    nullable: bool = False
    multiple: bool = False
    choices: tuple[ScalarValue, ...] = ()


def _validate_column(
    column: ColumnSpec,
    fields: Mapping[str, QueryField],
    *,
    identity: IdentitySpec,
) -> None:
    specs = tuple(fields[name] for name in column.fields)
    formatter = column.formatter
    single = {
        "text",
        "bool",
        "integer",
        "join",
        "identity-component",
        "truncate",
    }
    paired = {"pair", "ratio", "env"}
    if formatter in single and len(specs) != 1:
        raise ToolangError(
            f"column {column.label!r} formatter {formatter!r} requires one field"
        )
    if formatter in paired and len(specs) != 2:
        raise ToolangError(
            f"column {column.label!r} formatter {formatter!r} requires two fields"
        )
    if formatter == "identity" and tuple(column.fields) != identity.paths:
        raise ToolangError(
            f"column {column.label!r} identity fields must match "
            f"{', '.join(identity.paths)}"
        )
    if formatter in {"text", "identity-component", "truncate"} and (
        specs[0].kind not in {"text", "enum"} or specs[0].multiple
    ):
        raise ToolangError(
            f"column {column.label!r} formatter {formatter!r} requires scalar text"
        )
    if formatter == "bool" and (specs[0].kind != "bool" or specs[0].multiple):
        raise ToolangError(f"column {column.label!r} bool formatter requires bool")
    if formatter == "integer" and (specs[0].kind != "integer" or specs[0].multiple):
        raise ToolangError(
            f"column {column.label!r} integer formatter requires integer"
        )
    if formatter == "join" and not specs[0].multiple:
        raise ToolangError(
            f"column {column.label!r} join formatter requires a sequence"
        )
    if formatter == "bool-labels" and any(
        spec.kind != "bool" or spec.multiple for spec in specs
    ):
        raise ToolangError(
            f"column {column.label!r} bool-labels formatter requires bool fields"
        )
    if formatter == "pair" and any(
        spec.kind not in {"integer", "float", "decimal", "date", "datetime"}
        or spec.multiple
        for spec in specs
    ):
        raise ToolangError(
            f"column {column.label!r} pair formatter requires scalar numeric or date fields"
        )
    if formatter == "ratio" and any(
        spec.kind not in {"integer", "float", "decimal"} or spec.multiple
        for spec in specs
    ):
        raise ToolangError(
            f"column {column.label!r} ratio formatter requires scalar numeric fields"
        )
    if formatter == "env" and any(
        spec.kind != "text" or not spec.multiple for spec in specs
    ):
        raise ToolangError(
            f"column {column.label!r} env formatter requires text sequences"
        )


def _compile_public_fields(
    item_type: type[object],
    *,
    finite_paths: Mapping[str, Any],
    excluded: set[str],
) -> dict[str, _FieldType]:
    result: dict[str, _FieldType] = {}

    def visit(annotation: Any, prefix: str, *, parent_nullable: bool = False) -> None:
        if prefix in excluded:
            return
        optional, inner = _unwrap_optional(annotation)
        nullable = parent_nullable or optional
        if is_dataclass(inner) or is_typeddict(inner):
            hints = get_type_hints(inner, include_extras=True)
            for name, child in hints.items():
                visit(
                    child,
                    f"{prefix}.{name}" if prefix else name,
                    parent_nullable=nullable,
                )
            return
        origin = get_origin(inner)
        if origin in {dict, Mapping}:
            configured = {
                path: value
                for path, value in finite_paths.items()
                if path == prefix or path.startswith(f"{prefix}.")
            }
            if not configured:
                raise ToolangError(
                    f"public query field {prefix!r} is a dynamic mapping; "
                    "declare finite paths or exclude it"
                )
            for path, child in configured.items():
                spec = _compile_scalar_annotation(child, field=path)
                result[path] = _with_nullable(spec) if nullable else spec
            return
        scalar = _compile_scalar_annotation(annotation, field=prefix)
        if nullable and not scalar.nullable:
            scalar = _with_nullable(scalar)
        result[prefix] = scalar

    if not (is_dataclass(item_type) or is_typeddict(item_type)):
        raise ToolangError(
            f"public query view {item_type!r} must be a dataclass or TypedDict"
        )
    hints = get_type_hints(item_type, include_extras=True)
    for field_name in hints:
        visit(hints[field_name], field_name)
    return result


def _with_nullable(field: _FieldType) -> _FieldType:
    return _FieldType(
        field.kind,
        nullable=True,
        multiple=field.multiple,
        choices=field.choices,
    )


def _compile_scalar_annotation(annotation: Any, *, field: str) -> _FieldType:
    nullable, inner = _unwrap_optional(annotation)
    origin = get_origin(inner)
    multiple = False
    if origin in {list, tuple, set, frozenset, Sequence}:
        args = get_args(inner)
        if not args:
            raise ToolangError(f"public query field {field!r} has an untyped sequence")
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            inner = args[0]
        elif len(args) == 1:
            inner = args[0]
        else:
            raise ToolangError(
                f"public query field {field!r} has a heterogeneous sequence"
            )
        multiple = True
        if _unwrap_optional(inner)[0]:
            raise ToolangError(
                f"public query field {field!r} cannot contain nullable elements"
            )
    origin = get_origin(inner)
    choices: tuple[ScalarValue, ...] = ()
    if origin is Literal:
        raw_choices = tuple(
            _normalize_runtime_value(value) for value in get_args(inner)
        )
        if not raw_choices:
            raise ToolangError(f"public query field {field!r} has an empty Literal")
        kinds = {_kind_for_value(value, field=field) for value in raw_choices}
        if len(kinds) != 1:
            raise ToolangError(
                f"public query field {field!r} has heterogeneous Literal values"
            )
        kind = "bool" if kinds == {"bool"} else "enum"
        choices = raw_choices
    elif isinstance(inner, type) and issubclass(inner, Enum):
        choices = tuple(_normalize_runtime_value(member.value) for member in inner)
        kinds = {_kind_for_value(value, field=field) for value in choices}
        if len(kinds) != 1:
            raise ToolangError(
                f"public query field {field!r} has heterogeneous enum values"
            )
        kind = "enum"
    elif inner is bool:
        kind = "bool"
    elif inner is str:
        kind = "text"
    elif inner is int:
        kind = "integer"
    elif inner is float:
        kind = "float"
    elif inner is Decimal:
        kind = "decimal"
    elif inner is datetime:
        kind = "datetime"
    elif inner is date:
        kind = "date"
    else:
        raise ToolangError(
            f"public query field {field!r} has unsupported type {annotation!r}"
        )
    return _FieldType(kind, nullable=nullable, multiple=multiple, choices=choices)


def _unwrap_optional(annotation: Any) -> tuple[bool, Any]:
    origin = get_origin(annotation)
    if origin not in {Union, UnionType}:
        return False, annotation
    args = get_args(annotation)
    non_null = tuple(value for value in args if value is not type(None))
    if len(non_null) == len(args):
        raise ToolangError(f"heterogeneous unions are not queryable: {annotation!r}")
    if len(non_null) != 1:
        raise ToolangError(f"heterogeneous unions are not queryable: {annotation!r}")
    return True, non_null[0]


def _parse_query_value(
    raw: str, schema: CollectionSchema[Any]
) -> tuple[QuerySelector, ...]:
    text = raw.strip()
    if not text:
        raise ToolangError("collection query cannot be empty")
    parts = _split_top_level(text, ",", context="query")
    return tuple(_parse_selector(part, schema) for part in parts)


def _parse_selector(raw: str, schema: CollectionSchema[Any]) -> QuerySelector:
    text = raw.strip()
    if not text:
        raise ToolangError("collection query contains an empty selector")
    bracket = _find_unquoted(text, "[")
    predicates_text: str | None = None
    identity_text = text
    if bracket >= 0:
        closing = _matching_closing_bracket(text, bracket)
        if closing != len(text) - 1:
            raise ToolangError(
                f"invalid selector {raw!r}: predicate block must be last"
            )
        identity_text = text[:bracket].strip()
        predicates_text = text[bracket + 1 : closing].strip()
        if not predicates_text:
            raise ToolangError(
                f"invalid selector {raw!r}: predicate block cannot be empty"
            )
    elif _find_unquoted(text, "]") >= 0:
        raise ToolangError(f"invalid selector {raw!r}: unmatched closing bracket")
    pattern, exact = _parse_identity(identity_text)
    predicates: tuple[QueryPredicate, ...] = ()
    if predicates_text is not None:
        predicates = tuple(
            _parse_predicate(value, schema)
            for value in _split_top_level(
                predicates_text, ";", context="predicate list"
            )
        )
    return QuerySelector(pattern, exact, predicates)


def _parse_identity(text: str) -> tuple[str, bool]:
    if not text:
        return "*", False
    if text.startswith('"'):
        value = _parse_json_string(text, context="identity")
        if not value:
            raise ToolangError("quoted identity cannot be empty")
        return value, True
    if not _BARE_IDENTITY_RE.fullmatch(text):
        raise ToolangError(
            f"invalid bare identity pattern {text!r}; use a JSON string for punctuation or whitespace"
        )
    if "]" in text:
        raise ToolangError(f"invalid identity pattern: {text!r}")
    return text, False


def _parse_predicate(raw: str, schema: CollectionSchema[Any]) -> QueryPredicate:
    text = raw.strip()
    if not text:
        raise ToolangError("predicate list contains an empty predicate")
    keyword = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_.]*)\s+(not\s+in|in)\s*\((.*)\)",
        text,
        flags=re.DOTALL,
    )
    if keyword:
        field = _require_field(schema, keyword.group(1))
        operator: QueryOperator = (
            "not in" if keyword.group(2).startswith("not") else "in"
        )
        _validate_operator(field, operator)
        raw_values = _split_top_level(keyword.group(3), ",", context="literal list")
        values = tuple(_parse_literal(value, field) for value in raw_values)
        _validate_predicate(field, operator, values)
        return QueryPredicate(field, operator, values)
    comparator = _find_comparator(text)
    if comparator is not None:
        index, operator = comparator
        field_name = text[:index].strip()
        literal = text[index + len(operator) :].strip()
        if not field_name or not literal:
            raise ToolangError(f"invalid predicate: {raw!r}")
        field = _require_field(schema, field_name)
        typed_operator = operator  # narrows through validation below
        _validate_operator(field, typed_operator)
        values = (_parse_literal(literal, field),)
        _validate_predicate(field, typed_operator, values)
        return QueryPredicate(field, typed_operator, values)  # type: ignore[arg-type]
    negated = text.startswith("!")
    field_name = text[1:].strip() if negated else text
    field = _require_field(schema, field_name)
    if field.kind != "bool":
        raise ToolangError(
            f"query field {field.name!r} is {field.type_name}; flag syntax requires bool"
        )
    return QueryPredicate(field, "=", (not negated,))


def _require_field(schema: CollectionSchema[Any], name: str) -> QueryField:
    if not _FIELD_RE.fullmatch(name):
        raise ToolangError(f"invalid query field name: {name!r}")
    field = schema.fields.get(name)
    if field is None:
        available = ", ".join(schema.fields)
        raise ToolangError(
            f"unknown {schema.name} query field {name!r}; available fields: {available}"
        )
    return field


def _find_comparator(text: str) -> tuple[int, str] | None:
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
            continue
        for operator in _COMPARATORS:
            if text.startswith(operator, index):
                return index, operator
    if quoted:
        raise ToolangError(f"unterminated JSON string in predicate: {text!r}")
    return None


def _validate_predicate(
    field: QueryField,
    operator: str,
    values: tuple[ScalarValue, ...],
) -> None:
    _validate_operator(field, operator)
    if not values:
        raise ToolangError(f"predicate for query field {field.name!r} has no values")
    if any(value is None for value in values):
        if not field.nullable:
            raise ToolangError(f"query field {field.name!r} is not nullable")
        if operator not in {"=", "!=", "in", "not in"}:
            raise ToolangError(
                f"null only supports equality membership on query field {field.name!r}"
            )


def _validate_operator(field: QueryField, operator: str) -> None:
    if operator not in field.operators:
        allowed = ", ".join(field.operators)
        raise ToolangError(
            f"operator {operator!r} is not valid for query field {field.name!r} "
            f"({field.type_name}); expected one of {allowed}"
        )


def _parse_literal(raw: str, field: QueryField) -> ScalarValue:
    text = raw.strip()
    if not text:
        raise ToolangError(f"query field {field.name!r} has an empty literal")
    if text == "null":
        if not field.nullable:
            raise ToolangError(f"query field {field.name!r} is not nullable")
        return None
    if text.startswith('"'):
        value: object = _parse_json_string(text, context="literal")
        quoted = True
    else:
        if not _BARE_LITERAL_RE.fullmatch(text):
            raise ToolangError(
                f"invalid literal {text!r}; use a JSON string for punctuation or whitespace"
            )
        value = text
        quoted = False
    try:
        parsed = _coerce_literal(value, field, quoted=quoted)
    except (ValueError, InvalidOperation) as error:
        raise ToolangError(
            f"invalid {field.type_name} literal {text!r} for query field {field.name!r}"
        ) from error
    if field.choices and parsed not in field.choices:
        expected = ", ".join(format_literal(choice) for choice in field.choices)
        raise ToolangError(
            f"invalid value {text!r} for query field {field.name!r}; expected one of {expected}"
        )
    return parsed


def _coerce_literal(
    value: object,
    field: QueryField,
    *,
    quoted: bool,
) -> ScalarValue:
    if not isinstance(value, str):
        raise ValueError
    if field.kind == "text":
        if not quoted and value in {"true", "false", "null"}:
            raise ValueError
        return value
    if quoted and not (
        field.kind == "enum" and field.choices and isinstance(field.choices[0], str)
    ):
        raise ValueError
    if field.kind == "bool":
        if value == "true":
            return True
        if value == "false":
            return False
        raise ValueError
    if field.kind == "integer":
        if not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", value):
            raise ValueError
        return int(value)
    if field.kind == "float":
        if not re.fullmatch(
            r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
            value,
        ):
            raise ValueError
        return float(value)
    if field.kind == "decimal":
        if not re.fullmatch(
            r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
            value,
        ):
            raise ValueError
        return Decimal(value)
    if field.kind == "date":
        return date.fromisoformat(value)
    if field.kind == "datetime":
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if field.kind == "enum":
        sample = next(choice for choice in field.choices if choice is not None)
        if isinstance(sample, bool):
            if value == "true":
                return True
            if value == "false":
                return False
            raise ValueError
        if isinstance(sample, str):
            if not quoted and value in {"true", "false", "null"}:
                raise ValueError
            return value
        if isinstance(sample, int):
            return int(value)
        if isinstance(sample, float):
            return float(value)
    raise ValueError


def _identity_matches(
    components: tuple[str, ...],
    spec: IdentitySpec,
    selector: QuerySelector,
) -> bool:
    separator = spec.separator
    canonical = (
        components[0] if len(components) == 1 else (separator or "").join(components)
    )
    if selector.identity_exact:
        return canonical == selector.identity_pattern
    pattern = selector.identity_pattern
    if len(components) == 1 or not separator or separator not in pattern:
        return _glob_matches(components[-1], pattern)
    patterns = tuple(pattern.split(separator, maxsplit=len(components) - 1))
    if len(patterns) != len(components):
        return False
    return all(
        _glob_matches(value, expected)
        for value, expected in zip(components, patterns, strict=True)
    )


def _predicate_matches(
    actual_values: tuple[ScalarValue, ...],
    predicate: QueryPredicate,
) -> bool:
    operator = predicate.operator
    if operator in _NEGATIVE_OPERATORS:
        return bool(actual_values) and all(
            not _positive_value_match(actual, predicate.values, operator)
            for actual in actual_values
        )
    return any(
        _positive_value_match(actual, predicate.values, operator)
        for actual in actual_values
    )


def _positive_value_match(
    actual: ScalarValue,
    expected: tuple[ScalarValue, ...],
    operator: QueryOperator,
) -> bool:
    if operator in {"=", "!=", "in", "not in"}:
        return any(actual == value for value in expected)
    if operator in {"~=", "!~="}:
        return isinstance(actual, str) and _glob_matches(actual, str(expected[0]))
    if actual is None or expected[0] is None:
        return False
    try:
        if operator == "<":
            return actual < expected[0]  # type: ignore[operator]
        if operator == "<=":
            return actual <= expected[0]  # type: ignore[operator]
        if operator == ">":
            return actual > expected[0]  # type: ignore[operator]
        if operator == ">=":
            return actual >= expected[0]  # type: ignore[operator]
    except TypeError:
        return False
    return False


def _validate_runtime_value(field: QueryField, value: ScalarValue) -> None:
    if value is None:
        if field.nullable:
            return
        raise ToolangError(f"query field {field.name!r} is not nullable")
    expected: type[object]
    if field.kind == "bool":
        expected = bool
    elif (
        field.kind in {"text", "enum"}
        and field.choices
        and not isinstance(field.choices[0], str)
    ):
        expected = type(field.choices[0])
    elif field.kind in {"text", "enum"}:
        expected = str
    elif field.kind == "integer":
        expected = int
    elif field.kind == "float":
        expected = float
    elif field.kind == "decimal":
        expected = Decimal
    elif field.kind == "datetime":
        expected = datetime
    else:
        expected = date
    if type(value) is not expected:
        raise ToolangError(
            f"query field {field.name!r} expected {field.type_name}, got {type(value).__name__}"
        )
    if field.choices and value not in field.choices:
        raise ToolangError(
            f"query field {field.name!r} contains undeclared value {value!r}"
        )


def _normalize_runtime_value(value: object) -> ScalarValue:
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(
        value, (str, bool, int, float, Decimal, datetime, date)
    ):
        return value
    raise ToolangError(f"unsupported runtime query value: {value!r}")


def _format_display_scalar(value: ScalarValue) -> str:
    if value is None:
        return "-"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return f"{value:_}"
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _glob_matches(value: str, pattern: str) -> bool:
    """Match a case-sensitive glob whose only metacharacters are ``*`` and ``?``."""

    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, value, flags=re.DOTALL) is not None


def _read_path(value: object, path: Sequence[str]) -> object:
    current = value
    for component in path:
        if current is None:
            return None
        if isinstance(current, Mapping):
            mapping = cast(Mapping[object, object], current)
            if component not in mapping:
                raise ToolangError(f"query value path {'.'.join(path)!r} is missing")
            current = mapping[component]
        else:
            try:
                current = getattr(current, component)
            except AttributeError as error:
                raise ToolangError(
                    f"query value path {'.'.join(path)!r} is missing"
                ) from error
    return current


def _split_top_level(text: str, delimiter: str, *, context: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    bracket_depth = 0
    paren_depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "[":
            bracket_depth += 1
            if bracket_depth > 1:
                raise ToolangError(
                    f"invalid {context}: nested predicate blocks are not supported"
                )
        elif char == "]":
            bracket_depth -= 1
            if bracket_depth < 0:
                raise ToolangError(f"invalid {context}: unmatched closing bracket")
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth < 0:
                raise ToolangError(f"invalid {context}: unmatched closing parenthesis")
        elif char == delimiter and bracket_depth == 0 and paren_depth == 0:
            part = text[start:index].strip()
            if not part:
                raise ToolangError(f"invalid {context}: empty value")
            parts.append(part)
            start = index + 1
    if quoted:
        raise ToolangError(f"invalid {context}: unterminated JSON string")
    if bracket_depth:
        raise ToolangError(f"invalid {context}: unmatched predicate bracket")
    if paren_depth:
        raise ToolangError(f"invalid {context}: unmatched parenthesis")
    part = text[start:].strip()
    if not part:
        raise ToolangError(f"invalid {context}: empty value")
    parts.append(part)
    return tuple(parts)


def _find_unquoted(text: str, target: str) -> int:
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == target:
            return index
    if quoted:
        raise ToolangError("unterminated JSON string in selector")
    return -1


def _matching_closing_bracket(text: str, opening: int) -> int:
    quoted = False
    escaped = False
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "[":
            depth += 1
            if depth > 1:
                raise ToolangError("nested predicate blocks are not supported")
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    raise ToolangError("unmatched predicate bracket")


def _parse_json_string(text: str, *, context: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ToolangError(f"invalid JSON-quoted {context}: {text!r}") from error
    if not isinstance(value, str):
        raise ToolangError(f"JSON-quoted {context} must be a string")
    return value


def format_identity(value: str, *, exact: bool = False) -> str:
    """Format an identity pattern or exact identity with canonical quoting."""

    safe = bool(_BARE_IDENTITY_RE.fullmatch(value)) and "]" not in value
    if safe and (not exact or not any(char in value for char in "*?")):
        return value
    return json.dumps(value, ensure_ascii=False)


def format_literal(value: ScalarValue) -> str:
    """Format one typed literal with canonical quoting."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if _BARE_LITERAL_RE.fullmatch(value) and value not in {"true", "false", "null"}:
        return value
    return json.dumps(value, ensure_ascii=False)


def format_query(query: CollectionQuery) -> str:
    """Format a validated query in canonical syntax."""

    selectors: list[str] = []
    for selector in query.selectors:
        identity = format_identity(
            selector.identity_pattern, exact=selector.identity_exact
        )
        predicates: list[str] = []
        for predicate in selector.predicates:
            if (
                predicate.field.kind == "bool"
                and predicate.operator == "="
                and len(predicate.values) == 1
                and isinstance(predicate.values[0], bool)
            ):
                predicates.append(
                    predicate.field.name
                    if predicate.values[0]
                    else f"!{predicate.field.name}"
                )
            elif predicate.operator in {"in", "not in"}:
                values = ",".join(format_literal(value) for value in predicate.values)
                predicates.append(
                    f"{predicate.field.name} {predicate.operator} ({values})"
                )
            else:
                predicates.append(
                    f"{predicate.field.name}{predicate.operator}{format_literal(predicate.values[0])}"
                )
        selectors.append(identity + (f"[{';'.join(predicates)}]" if predicates else ""))
    return ",".join(selectors)


def prefix_query_identities(
    query: CollectionQuery,
    *,
    prefix: str,
    separator: str,
) -> CollectionQuery:
    """Bind one leading identity component without changing predicates."""

    if not prefix or not separator or separator in prefix:
        raise ToolangError("query identity prefix and separator must be unambiguous")
    return CollectionQuery(
        tuple(
            QuerySelector(
                identity_pattern=f"{prefix}{separator}{selector.identity_pattern}",
                identity_exact=selector.identity_exact,
                predicates=selector.predicates,
            )
            for selector in query.selectors
        )
    )


def prefix_query_value(raw: str, *, prefix: str, separator: str) -> str:
    """Bind a leading identity component while preserving uncompiled predicates."""

    if not prefix or not separator or separator in prefix:
        raise ToolangError("query identity prefix and separator must be unambiguous")
    text = raw.strip()
    if not text:
        raise ToolangError("collection query cannot be empty")
    values: list[str] = []
    for part in _split_top_level(text, ",", context="query"):
        bracket = _find_unquoted(part, "[")
        predicate_suffix = ""
        identity_text = part
        if bracket >= 0:
            closing = _matching_closing_bracket(part, bracket)
            if closing != len(part) - 1:
                raise ToolangError(
                    f"invalid selector {part!r}: predicate block must be last"
                )
            predicate_suffix = part[bracket:]
            identity_text = part[:bracket].strip()
        pattern, exact = _parse_identity(identity_text)
        qualified = f"{prefix}{separator}{pattern}"
        values.append(format_identity(qualified, exact=exact) + predicate_suffix)
    return ",".join(values)


def resolve_query_sentinels(
    values: Sequence[str],
    *,
    label: str,
) -> tuple[str, ...] | None:
    """Resolve policy-layer ``all``/``none`` while rejecting mixed queries."""

    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{label} queries must be strings")
        text = value.strip()
        if not text:
            raise ToolangError(f"{label} queries must not be empty")
        if text not in normalized:
            normalized.append(text)
    if not normalized:
        return ()
    if len(normalized) == 1 and normalized[0].lower() == "all":
        return None
    if len(normalized) == 1 and normalized[0].lower() == "none":
        return ()
    if any(_query_has_sentinel_alternative(value) for value in normalized):
        raise ToolangError(f"{label} cannot mix queries with all or none")
    return tuple(normalized)


def _query_has_sentinel_alternative(raw: str) -> bool:
    for part in _split_top_level(raw, ",", context="query"):
        bracket = _find_unquoted(part, "[")
        identity_text = part
        if bracket >= 0:
            closing = _matching_closing_bracket(part, bracket)
            if closing != len(part) - 1:
                raise ToolangError(
                    f"invalid selector {part!r}: predicate block must be last"
                )
            identity_text = part[:bracket].strip()
        pattern, exact = _parse_identity(identity_text)
        if bracket < 0 and not exact and pattern.lower() in {"all", "none"}:
            return True
    return False


def _kind_for_value(value: ScalarValue, *, field: str) -> QueryKind:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "text"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    raise ToolangError(f"public query field {field!r} has unsupported choice {value!r}")


def _json_value(value: ScalarValue) -> object:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


__all__ = [
    "CollectionDefinition",
    "CollectionQuery",
    "CollectionSchema",
    "ColumnFormatter",
    "ColumnSpec",
    "IdentitySpec",
    "QueryDataset",
    "QueryField",
    "QueryOperator",
    "QueryPredicate",
    "QuerySelector",
    "SetOperator",
    "format_identity",
    "format_literal",
    "format_query",
    "prefix_query_identities",
    "prefix_query_value",
    "resolve_query_sentinels",
]
