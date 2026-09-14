"""Phase 3: tree-sitter parsing into symbols and raw edge references.

Deliberately hand-walked rather than driven by tree-sitter's query language. The
query API has changed shape three times across recent tree-sitter releases;
node-type traversal has not. It is also easier to debug when a grammar surprises
you, which it will.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any

from tree_sitter import Language, Node, Parser

# --------------------------------------------------------------------------
# grammar loading
# --------------------------------------------------------------------------


@cache
def _language(name: str) -> Language | None:
    try:
        if name == "python":
            import tree_sitter_python as m

            return Language(m.language())
        if name == "javascript":
            import tree_sitter_javascript as m

            return Language(m.language())
        if name == "typescript":
            import tree_sitter_typescript as m

            return Language(m.language_typescript())
        if name == "tsx":
            import tree_sitter_typescript as m

            return Language(m.language_tsx())
        if name == "go":
            import tree_sitter_go as m

            return Language(m.language())
    except Exception:  # pragma: no cover - missing optional grammar
        return None
    return None


SUPPORTED_LANGUAGES = ("python", "javascript", "typescript", "tsx", "go")


@dataclass(slots=True)
class ParsedSymbol:
    kind: str
    name: str
    qualified_name: str
    signature: str | None
    docstring: str | None
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    parent_path: tuple[str, ...] = ()


@dataclass(slots=True)
class RawEdge:
    src_qualified_name: str
    dst_name: str
    kind: str
    line: int


@dataclass(slots=True)
class ParsedFile:
    symbols: list[ParsedSymbol] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    module_path: str = ""


# --------------------------------------------------------------------------
# per-language node-type configuration
# --------------------------------------------------------------------------

# node type -> symbol kind
DEFINITION_NODES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "generator_function_declaration": "function",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "abstract_class_declaration": "class",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
}
DEFINITION_NODES["tsx"] = DEFINITION_NODES["typescript"]

CONTAINER_KINDS = {"class", "interface"}

CALL_NODES = {"call", "call_expression", "new_expression"}
IMPORT_NODES = {
    "import_statement",
    "import_from_statement",
    "import_declaration",
    "import_spec",
}
INHERIT_FIELDS = {
    "python": ("superclasses",),
    "javascript": ("superclass",),
    "typescript": ("superclass",),
    "tsx": ("superclass",),
    "go": (),
}


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _name_of(node: Node, src: bytes, language: str) -> str | None:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named, src)
    if language == "go":
        if node.type == "method_declaration":
            fid = next((c for c in node.children if c.type == "field_identifier"), None)
            if fid is not None:
                return _text(fid, src)
        if node.type == "type_declaration":
            spec = next((c for c in node.children if c.type == "type_spec"), None)
            if spec is not None:
                ident = spec.child_by_field_name("name")
                if ident is not None:
                    return _text(ident, src)
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return _text(child, src)
    return None


def _signature(node: Node, src: bytes) -> str:
    """The definition line(s) up to the body, collapsed to one line."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    raw = src[node.start_byte : end].decode("utf-8", errors="replace")
    collapsed = " ".join(raw.split())
    return collapsed[:500].rstrip("{:( ")


def _python_docstring(node: Node, src: bytes) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first = body.children[0]
    if first.type != "expression_statement" or not first.children:
        return None
    string_node = first.children[0]
    if string_node.type != "string":
        return None
    raw = _text(string_node, src)
    return raw.strip("'\"bru \n")[:2000] or None


def _go_receiver_type(node: Node, src: bytes) -> str | None:
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return None
    text = _text(recv, src).strip("()")
    parts = text.replace("*", "").split()
    return parts[-1] if parts else None


def _callee_name(node: Node, src: bytes) -> str | None:
    fn = node.child_by_field_name("function") or node.child_by_field_name("constructor")
    if fn is None:
        return None
    if fn.type in ("identifier", "field_identifier"):
        return _text(fn, src)
    if fn.type in ("attribute", "member_expression", "selector_expression"):
        attr = (
            fn.child_by_field_name("attribute")
            or fn.child_by_field_name("property")
            or fn.child_by_field_name("field")
        )
        obj = fn.child_by_field_name("object") or fn.child_by_field_name("operand")
        if attr is not None:
            attr_name = _text(attr, src)
            if obj is not None and obj.type in ("identifier", "type_identifier"):
                return f"{_text(obj, src)}.{attr_name}"
            return attr_name
    return None


def _import_names(node: Node, src: bytes) -> list[str]:
    names: list[str] = []
    for child in node.children:
        if child.type in ("dotted_name", "identifier", "type_identifier"):
            names.append(_text(child, src))
        elif child.type in ("string", "interpreted_string_literal"):
            names.append(_text(child, src).strip("'\"`"))
        elif child.type in ("import_clause", "named_imports", "dotted_name", "import_spec_list"):
            names.extend(_import_names(child, src))
        elif child.type in ("import_specifier", "import_spec", "aliased_import"):
            names.extend(_import_names(child, src))
    if not names:
        mod = node.child_by_field_name("module_name") or node.child_by_field_name("source")
        if mod is not None:
            names.append(_text(mod, src).strip("'\"`"))
    return [n for n in names if n]


def module_path_for(rel_path: str, language: str) -> str:
    stem = rel_path.rsplit(".", 1)[0]
    if language == "python":
        parts = [p for p in stem.split("/") if p]
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return stem.replace("/", ".")


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def parse_file(rel_path: str, source: str, language: str) -> ParsedFile:
    """Extract symbols and unresolved edge references from one file."""
    result = ParsedFile(module_path=module_path_for(rel_path, language))
    lang = _language(language)
    if lang is None:
        return result

    definitions = DEFINITION_NODES.get(language, {})
    src = source.encode("utf-8")
    tree = Parser(lang).parse(src)

    def enclosing_qname(stack: list[ParsedSymbol]) -> str:
        return stack[-1].qualified_name if stack else result.module_path

    def visit(node: Node, stack: list[ParsedSymbol]) -> None:
        kind = definitions.get(node.type)
        new_stack = stack

        if kind is not None:
            name = _name_of(node, src, language)
            if name:
                parent_path = tuple(s.name for s in stack)
                # A function nested inside a class is a method, whatever the grammar calls it.
                if kind == "function" and stack and stack[-1].kind in CONTAINER_KINDS:
                    kind = "method"

                prefix = enclosing_qname(stack)
                if language == "go" and node.type == "method_declaration":
                    receiver = _go_receiver_type(node, src)
                    if receiver:
                        prefix = (
                            f"{result.module_path}.{receiver}" if result.module_path else receiver
                        )

                qualified = f"{prefix}.{name}" if prefix else name
                symbol = ParsedSymbol(
                    kind=kind,
                    name=name,
                    qualified_name=qualified,
                    signature=_signature(node, src),
                    docstring=_python_docstring(node, src) if language == "python" else None,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent_path=parent_path,
                )
                result.symbols.append(symbol)
                new_stack = stack + [symbol]

                bases: list[Node] = []
                for field_name in INHERIT_FIELDS.get(language, ()):
                    base = node.child_by_field_name(field_name)
                    if base is not None:
                        bases.append(base)
                # TS/JS put `extends X implements Y` in a class_heritage node
                # rather than behind a named field.
                bases.extend(c for c in node.children if c.type == "class_heritage")
                for base in bases:
                    for ident in _collect_identifiers(base, src):
                        result.edges.append(
                            RawEdge(qualified, ident, "inherits", node.start_point[0] + 1)
                        )

        if node.type in CALL_NODES:
            callee = _callee_name(node, src)
            if callee:
                result.edges.append(
                    RawEdge(enclosing_qname(new_stack), callee, "calls", node.start_point[0] + 1)
                )
        elif node.type in IMPORT_NODES:
            for imported in _import_names(node, src):
                result.edges.append(
                    RawEdge(result.module_path, imported, "imports", node.start_point[0] + 1)
                )

        for child in node.children:
            visit(child, new_stack)

    visit(tree.root_node, [])
    result.edges = _dedupe_edges(result.edges)
    return result


def _dedupe_edges(edges: list[RawEdge]) -> list[RawEdge]:
    """Nested grammar nodes emit the same reference twice (Go imports, mainly)."""
    seen: dict[tuple[str, str, str], RawEdge] = {}
    for edge in edges:
        key = (edge.src_qualified_name, edge.dst_name, edge.kind)
        current = seen.get(key)
        if current is None or edge.line < current.line:
            seen[key] = edge
    return list(seen.values())


def _collect_identifiers(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    if node.type in ("identifier", "type_identifier", "dotted_name"):
        out.append(_text(node, src))
    for child in node.children:
        out.extend(_collect_identifiers(child, src))
    return out


def language_stats(files: list[Any]) -> dict[str, int]:
    """LOC per language, for the repo overview card."""
    stats: dict[str, int] = {}
    for f in files:
        key = getattr(f, "language", None) or getattr(f, "kind", "other")
        stats[key] = stats.get(key, 0) + getattr(f, "loc", 0)
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]))
