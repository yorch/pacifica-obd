#!/usr/bin/env python3
"""extract_seedkey.py -- turn your own AlfaOBD install into a seed->key provider.

AlfaOBD ships one C# method per (ECU, supplier, variant, algorithm) security
access algorithm, plus a dispatcher (``CalculateKey()``) that picks one from the
module's identification data.  This tool:

  * decodes AlfaOBD's ``b.h(literal, key)`` string obfuscation,
  * enumerates every ``CalculateKey*`` function with its ECU / supplier /
    variant / algorithm-ID metadata (``--list``),
  * recovers the selector out of the decompiled ``CalculateKey()`` dispatcher
    (``--selector``),
  * transpiles a chosen algorithm, and everything it calls, from the decompiled
    C# into a standalone Python provider (``--emit``) that
    ``pacifica-obd security --provider`` can load,
  * can also write a dotnet harness that runs the *original* C# so you can diff
    it against the generated provider (``--emit-harness``).

Nothing in this file is vendor material: every constant that ends up in a
generated provider is read out of the files *you* point it at.

Typical use
-----------
    python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml --list
    python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \\
        --selector dec/af.cs --ecu IPC_PN
    python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \\
        --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 \\
        -o ~/.local/share/pacifica-obd/providers/ipc_pn.py

Getting the inputs (Windows build of AlfaOBD, 2.x):

    dec/ad.cs   every CalculateKey* method -- decompiled class ``ad``, whose
                Dotfuscator original is ``AlfaOBD_PC.AlfaOBDKeys``.  Produce it
                with:  ilspycmd -p -o dec --nested-directories AlfaOBD.exe
    Map.xml     ships next to AlfaOBD.exe (Dotfuscator rename map).  Without it
                the tool still works, but functions are reported under their
                obfuscated two-letter names.
    dec/af.cs   the ``CalculateKey()`` dispatcher -- decompiled class ``af``
                (original ``AlfaOBD_PC.AlfaOBD``).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys

# ==========================================================================
# 1. The b.h(...) string decoder
# ==========================================================================
#
# From the decompiled helper (b.cs):
#
#     int num = 1843161013 + A_1;
#     b2 = (ch & 0xFF) ^ num++;
#     b3 = ((ch >> 8) & 0xFF) ^ num++;
#     out = (b2 << 8) | b3;

_H_SEED = 1843161013

_ESC_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{1,4}|[0-7]{1,3}|.)")
_ESC_MAP = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'",
            "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def unescape_csharp(lit: str) -> str:
    """Turn the text between the quotes of a C# string literal into characters."""

    def rep(m: "re.Match[str]") -> str:
        e = m.group(1)
        if e[0] in "ux":
            return chr(int(e[1:], 16))
        if e.isdigit():
            return chr(int(e, 8))
        return _ESC_MAP.get(e, e)

    return _ESC_RE.sub(rep, lit)


def h_decode(s: str, key: int) -> str:
    """Decode one ``b.h(s, key)`` literal."""
    n = _H_SEED + key
    out = []
    for ch in s:
        e = ord(ch)
        low = (e & 0xFF) ^ (n & 0xFF)
        n += 1
        high = ((e >> 8) & 0xFF) ^ (n & 0xFF)
        n += 1
        out.append(chr((low << 8) | high))
    return "".join(out)


# ==========================================================================
# 2. Dotfuscator map
# ==========================================================================

def load_map(path: str) -> dict:
    """Map.xml -> {obf_type: {'orig', 'methods': [(orig, obf, sig)], 'fields'}}."""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        data = fh.read()
    types: dict = {}
    for block in re.findall(r"<type>(.*?)</type>", data, re.S):
        tn = re.search(r"<name>(.*?)</name>", block, re.S)
        tw = re.search(r"<newname>(.*?)</newname>", block, re.S)
        if not (tn and tw):
            continue
        methods = []
        for m in re.findall(r"<method>(.*?)</method>", block, re.S):
            on = re.search(r"<name>(.*?)</name>", m, re.S)
            nn = re.search(r"<newname>(.*?)</newname>", m, re.S)
            sg = re.search(r"<signature>(.*?)</signature>", m, re.S)
            if on and nn:
                methods.append((html.unescape(on.group(1)),
                                html.unescape(nn.group(1)),
                                html.unescape(sg.group(1)) if sg else ""))
        fields = []
        for f in re.findall(r"<field>(.*?)</field>", block, re.S):
            on = re.search(r"<name>(.*?)</name>", f, re.S)
            nn = re.search(r"<newname>(.*?)</newname>", f, re.S)
            if on and nn:
                fields.append((html.unescape(on.group(1)), html.unescape(nn.group(1))))
        types[html.unescape(tw.group(1))] = {
            "orig": html.unescape(tn.group(1)), "methods": methods, "fields": fields}
    return types


_IL_TO_CS = {
    "unsigned int8": "byte", "int8": "sbyte", "int16": "short",
    "unsigned int16": "ushort", "int32": "int", "unsigned int32": "uint",
    "int64": "long", "unsigned int64": "ulong", "float32": "float",
    "float64": "double", "string": "string", "char": "char", "bool": "bool",
    "object": "object", "void": "void",
}


def _il_params(sig: str) -> list | None:
    m = re.match(r"^.*?\((.*)\)$", sig, re.S)
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return []
    out = []
    for part in inner.split(","):
        p = part.strip()
        rank = 0
        while p.endswith("[]"):
            p = p[:-2]
            rank += 1
        out.append(TypeRef(_IL_TO_CS.get(p, p), rank))
    return out


# ==========================================================================
# 3. A small C# front end (only the subset AlfaOBD's key methods use)
# ==========================================================================

_TOKEN_RE = re.compile(r"""
      (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<str>@"(?:[^"]|"")*"|"(?:\\.|[^"\\])*")
    | (?P<chr>'(?:\\.|[^'\\])')
    | (?P<num>0[xX][0-9a-fA-F]+[uUlL]*|\d[\d.]*[eE]?[+-]?\d*[fFdDmM]?[uUlL]*)
    | (?P<ident>@?[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op><<=|>>=|\+\+|--|>>|<<|&&|\|\||==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|::|[{}()\[\];,:?.!<>=+\-*/%&|^~])
    | (?P<ws>\s+)
    | (?P<other>.)
""", re.X | re.S)

_PRIMITIVES = ("int", "long", "byte", "bool", "string", "void", "uint", "ushort",
               "short", "sbyte", "object", "char", "double", "float", "decimal",
               "ulong", "nint", "nuint", "Int64", "Int32", "Byte", "UInt32",
               "Double", "IntPtr", "UIntPtr")


class CsError(RuntimeError):
    pass


def tokenize(src: str):
    out = []
    for m in _TOKEN_RE.finditer(src):
        if m.lastgroup in ("ws", "comment", "other"):
            continue
        out.append((m.lastgroup, m.group(0)))
    return out


class TypeRef:
    __slots__ = ("name", "rank")

    def __init__(self, name: str, rank: int = 0):
        self.name = name
        self.rank = rank

    def __repr__(self) -> str:
        return self.name + "[]" * self.rank

    def __eq__(self, other) -> bool:
        return (isinstance(other, TypeRef) and self.name == other.name
                and self.rank == other.rank)

    def __hash__(self) -> int:
        return hash((self.name, self.rank))


INT = TypeRef("int")
LONG = TypeRef("long")
BYTE = TypeRef("byte")
BOOL = TypeRef("bool")
STRING = TypeRef("string")
VOID = TypeRef("void")
DOUBLE = TypeRef("double")
OBJECT = TypeRef("object")

# ---------------------------------------------------------------- AST -----

class Node:
    pass


class Expr(Node):
    type = None


class Lit(Expr):
    def __init__(self, tok, kind):
        self.tok, self.kind = tok, kind


class Ident(Expr):
    def __init__(self, name):
        self.name = name


class FieldRef(Expr):
    def __init__(self, owner, name):
        self.owner, self.name = owner, name


class Member(Expr):
    def __init__(self, obj, name):
        self.obj, self.name = obj, name


class Call(Expr):
    def __init__(self, target, args, named=None):
        self.target, self.args = target, args
        self.named = named or [None] * len(args)
        self.method = None          # resolved python identifier
        self.ordered = None         # args reordered to parameter order


class NewArray(Expr):
    def __init__(self, ty, dims, init):
        self.ty, self.dims, self.init = ty, dims, init


class Index(Expr):
    def __init__(self, obj, idx):
        self.obj, self.idx = obj, idx


class Cast(Expr):
    def __init__(self, ty, expr):
        self.ty, self.expr = ty, expr


class Unary(Expr):
    def __init__(self, op, expr):
        self.op, self.expr = op, expr


class Binary(Expr):
    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right


class Ternary(Expr):
    def __init__(self, cond, a, b):
        self.cond, self.a, self.b = cond, a, b


class Assign(Expr):
    def __init__(self, op, target, value):
        self.op, self.target, self.value = op, target, value


class Checked(Expr):
    def __init__(self, inner):
        self.inner = inner


class Block(Node):
    def __init__(self, body):
        self.body = body


class Decl(Node):
    def __init__(self, ty, name, init):
        self.ty, self.name, self.init = ty, name, init


class If(Node):
    def __init__(self, cond, then, els):
        self.cond, self.then, self.els = cond, then, els


class While(Node):
    def __init__(self, cond, body, do_first=False):
        self.cond, self.body, self.do_first = cond, body, do_first


class For(Node):
    def __init__(self, init, cond, step, body):
        self.init, self.cond, self.step, self.body = init, cond, step, body


class Switch(Node):
    def __init__(self, disc, cases, default):
        self.disc, self.cases, self.default = disc, cases, default


class Return(Node):
    def __init__(self, value):
        self.value = value


class Control(Node):
    def __init__(self, kind):
        self.kind = kind


class ExprStmt(Node):
    def __init__(self, expr):
        self.expr = expr


class Throw(Node):
    def __init__(self, expr):
        self.expr = expr


class Try(Node):
    def __init__(self, body, handlers, final):
        self.body, self.handlers, self.final = body, handlers, final


class MethodDecl:
    def __init__(self, ret, name, params, body, line=0):
        self.ret, self.name, self.params, self.body = ret, name, params, body
        self.line = line
        self.orig = None
        self.py_name = None


class ParsedClass:
    def __init__(self, name):
        self.name = name
        self.fields: dict = {}
        self.methods: list = []
        self.static_ctor = None


class Parser:
    _ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=")
    _LEVELS = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="),
               ("<", ">", "<=", ">="), ("<<", ">>"), ("+", "-"), ("*", "/", "%")]
    _MODIFIERS = ("static", "public", "private", "internal", "protected",
                  "readonly", "const", "unsafe", "new", "volatile")

    def __init__(self, tokens, class_name="ad"):
        self.t = tokens
        self.i = 0
        self.class_name = class_name

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, "")

    def at(self, val):
        return self.peek()[1] == val

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def accept(self, val):
        if self.at(val):
            self.i += 1
            return True
        return False

    def expect(self, val):
        if not self.accept(val):
            raise CsError(f"expected {val!r} but found {self.peek()[1]!r}"
                          f"{self.context()}")
        return True

    def context(self, width=40):
        lo = max(0, self.i - width)
        before = "".join(t[1] for t in self.t[lo:self.i])
        after = "".join(t[1] for t in self.t[self.i:self.i + width // 2])
        return f" near '...{before}[[{after}...'"

    # -- types
    def try_type(self):
        save = self.i
        kind, val = self.peek()
        if kind != "ident":
            return None
        self.next()
        name = val.lstrip("@")
        while self.at("::") or self.at("."):
            self.next()
            k2, v2 = self.peek()
            if k2 != "ident":
                self.i = save
                return None
            self.next()
            name = v2.lstrip("@")
        rank = 0
        while self.at("["):
            j = 1
            commas = 0
            while self.peek(j)[1] == ",":
                commas += 1
                j += 1
            if self.peek(j)[1] != "]":
                break
            for _ in range(j + 1):
                self.next()
            rank += commas + 1
        if rank == 0 and name not in _PRIMITIVES:
            if not (name[:1].isupper() or name in (self.class_name,)):
                self.i = save
                return None
        return TypeRef(name, rank)

    # -- expressions
    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        left = self.parse_ternary()
        if self.peek()[1] in self._ASSIGN_OPS:
            op = self.next()[1]
            return Assign(op, left, self.parse_assign())
        return left

    def parse_ternary(self):
        cond = self.parse_binary(0)
        if self.accept("?"):
            a = self.parse_assign()
            self.expect(":")
            return Ternary(cond, a, self.parse_ternary())
        return cond

    def parse_binary(self, level):
        if level >= len(self._LEVELS):
            return self.parse_unary()
        ops = self._LEVELS[level]
        left = self.parse_binary(level + 1)
        while self.peek()[1] in ops:
            op = self.next()[1]
            left = Binary(op, left, self.parse_binary(level + 1))
        return left

    def parse_unary(self):
        val = self.peek()[1]
        if val in ("!", "-", "+", "~", "^"):
            self.next()
            return Unary(val, self.parse_unary())
        if val in ("++", "--"):
            self.next()
            return Unary(val + "pre", self.parse_unary())
        if val == "(":
            save = self.i
            self.next()
            ty = self.try_type()
            if ty is not None and self.at(")"):
                self.next()
                if self.peek()[1] not in (";", ",", ")", "]", "}", ":", ""):
                    return Cast(ty, self.parse_unary())
            self.i = save
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            val = self.peek()[1]
            if val == "(":
                pairs = self.parse_args()
                e = Call(e, [p[1] for p in pairs], [p[0] for p in pairs])
            elif val == "[":
                self.next()
                idx = [self.parse_expr()]
                while self.accept(","):
                    idx.append(self.parse_expr())
                self.expect("]")
                e = Index(e, idx)
            elif val == ".":
                self.next()
                if self.peek()[0] != "ident":
                    raise CsError("bad member access")
                e = Member(e, self.next()[1].lstrip("@"))
            elif val == "::":
                self.next()
                if self.peek()[0] != "ident":
                    raise CsError("bad :: access")
                nm = self.next()[1].lstrip("@")
                if self.at("."):
                    self.next()
                    if self.peek()[0] != "ident":
                        raise CsError("bad qualified access")
                    e = FieldRef(nm, self.next()[1].lstrip("@"))
                else:
                    e = Ident(nm)
            elif val in ("++", "--"):
                self.next()
                e = Unary(val + "post", e)
            else:
                break
        return e

    def parse_args(self):
        """Returns [(name_or_None, expr), ...]; `name` is an ILSpy named argument."""
        self.expect("(")
        args = []
        if not self.at(")"):
            args.append(self.parse_arg())
            while self.accept(","):
                args.append(self.parse_arg())
        self.expect(")")
        return args

    def parse_arg(self):
        if self.peek()[0] == "ident" and self.peek(1)[1] == ":":
            name = self.next()[1].lstrip("@")
            self.next()
            return (name, self.parse_expr())
        return (None, self.parse_expr())

    def parse_primary(self):
        kind, val = self.peek()
        if val == "(":
            self.next()
            e = self.parse_expr()
            self.expect(")")
            return e
        if val in ("checked", "unchecked"):
            self.next()
            self.expect("(")
            inner = self.parse_expr()
            self.expect(")")
            return Checked(inner)
        if val == "new":
            return self.parse_new()
        if kind == "num":
            self.next()
            return Lit(val, "num")
        if kind == "str":
            self.next()
            return Lit(val, "str")
        if kind == "chr":
            self.next()
            return Lit(val, "chr")
        if kind == "ident":
            self.next()
            return Ident(val.lstrip("@"))
        raise CsError(f"unexpected token {val!r}{self.context()}")

    def parse_new(self):
        self.expect("new")
        ty = self.try_type()
        if ty is None:
            raise CsError("'new' needs a type")
        dims = []
        if self.at("[") and self.peek(1)[1] != "]":
            self.next()
            dims.append(self.parse_expr())
            while self.accept(","):
                dims.append(self.parse_expr())
            self.expect("]")
        extra = 0
        while self.at("[") and self.peek(1)[1] == "]":
            self.next()
            self.next()
            extra += 1
        total = max(len(dims) + extra, 1)
        init = self.parse_array_init() if self.at("{") else None
        return NewArray(TypeRef(ty.name, total), dims, init)

    def parse_array_init(self):
        self.expect("{")
        out = []
        while not self.at("}"):
            out.append(self.parse_array_init() if self.at("{") else self.parse_expr())
            if not self.accept(","):
                break
        self.expect("}")
        return out

    # -- statements
    def parse_block(self):
        self.expect("{")
        body = []
        while not self.at("}"):
            body.append(self.parse_stmt())
        self.expect("}")
        return Block(body)

    def parse_stmt(self):
        val = self.peek()[1]
        if val == "{":
            return self.parse_block()
        if val == ";":
            self.next()
            return Block([])
        if val == "if":
            self.next()
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then = self.parse_stmt()
            els = self.parse_stmt() if self.accept("else") else None
            return If(cond, then, els)
        if val == "while":
            self.next()
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            return While(cond, self.parse_stmt())
        if val == "do":
            self.next()
            body = self.parse_stmt()
            self.expect("while")
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            self.expect(";")
            return While(cond, body, do_first=True)
        if val == "for":
            self.next()
            self.expect("(")
            init = None if self.at(";") else self.parse_for_init()
            self.expect(";")
            cond = None if self.at(";") else self.parse_expr()
            self.expect(";")
            step = None if self.at(")") else self.parse_expr()
            self.expect(")")
            return For(init, cond, step, self.parse_stmt())
        if val == "switch":
            return self.parse_switch()
        if val == "return":
            self.next()
            e = None if self.at(";") else self.parse_expr()
            self.expect(";")
            return Return(e)
        if val in ("break", "continue"):
            self.next()
            self.expect(";")
            return Control(val)
        if val == "throw":
            self.next()
            e = self.parse_expr()
            self.expect(";")
            return Throw(e)
        if val == "try":
            self.next()
            body = self.parse_block()
            handlers = []
            while self.accept("catch"):
                if self.at("("):
                    depth = 0
                    while True:
                        v = self.next()[1]
                        if v == "(":
                            depth += 1
                        elif v == ")":
                            depth -= 1
                            if depth == 0:
                                break
                handlers.append(self.parse_block())
            final = self.parse_block() if self.accept("finally") else None
            return Try(body, handlers, final)
        if val in ("goto", "label"):
            raise CsError("goto is not supported")
        save = self.i
        ty = self.try_type()
        if ty is not None and self.peek()[0] == "ident":
            name = self.next()[1].lstrip("@")
            init = self.parse_expr() if self.accept("=") else None
            self.expect(";")
            return Decl(ty, name, init)
        self.i = save
        e = self.parse_expr()
        self.expect(";")
        return ExprStmt(e)

    def parse_for_init(self):
        save = self.i
        ty = self.try_type()
        if ty is not None and self.peek()[0] == "ident":
            name = self.next()[1].lstrip("@")
            init = self.parse_expr() if self.accept("=") else None
            return Decl(ty, name, init)
        self.i = save
        return ExprStmt(self.parse_expr())

    def parse_switch(self):
        self.expect("switch")
        self.expect("(")
        disc = self.parse_expr()
        self.expect(")")
        self.expect("{")
        cases = []
        default = None
        cur = None
        while not self.at("}"):
            if self.accept("case"):
                val = self.parse_expr()
                self.expect(":")
                cur = [val, []]
                cases.append(cur)
            elif self.accept("default"):
                self.expect(":")
                cur = []
                default = cur
            else:
                if cur is None:
                    raise CsError("statement before first case")
                cur[1].append(self.parse_stmt())
        self.expect("}")
        return Switch(disc, cases, default)


def parse_class(src: str, class_name: str = "ad") -> ParsedClass:
    """Parse the top-level ``class class_name`` out of a decompiled file."""
    toks = tokenize(src)
    idx = None
    for i, (kind, val) in enumerate(toks):
        if val in ("class", "struct") and i + 1 < len(toks) \
                and toks[i + 1][1].lstrip("@") == class_name:
            idx = i
            break
    if idx is None:
        raise CsError(f"class {class_name!r} not found")
    j = idx
    while toks[j][1] != "{":
        j += 1
    depth, k = 0, j
    while k < len(toks):
        if toks[k][1] == "{":
            depth += 1
        elif toks[k][1] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1

    cls = ParsedClass(class_name)
    p = Parser(toks[j + 1:k], class_name)
    while p.i < len(p.t):
        progress = p.i
        if p.at("["):
            while not p.at("]"):
                p.next()
            p.next()
            continue
        if p.at(";"):
            p.next()
            continue
        while p.peek()[1] in Parser._MODIFIERS:
            p.next()
        if p.at("class") or p.at("struct") or p.at("enum"):
            # nested type: skip its body
            while not p.at("{"):
                p.next()
            depth = 0
            while True:
                v = p.next()[1]
                if v == "{":
                    depth += 1
                elif v == "}":
                    depth -= 1
                    if depth == 0:
                        break
            continue
        if p.peek()[0] == "ident" and p.peek()[1].lstrip("@") == class_name \
                and p.peek(1)[1] == "(":
            name, ret, is_ctor = class_name, VOID, True
        else:
            ret = p.try_type()
            if ret is None or p.peek()[0] != "ident":
                p.i = progress + 1
                continue
            name, is_ctor = p.next()[1].lstrip("@"), False
        if p.at("("):
            p.next()
            params = []
            while not p.at(")"):
                pty = p.try_type()
                pn = p.next()[1].lstrip("@")
                params.append((pty, pn))
                if not p.accept(","):
                    break
            p.expect(")")
            while p.peek()[1] != "{":
                p.next()
            body = p.parse_block()
            md = MethodDecl(ret, name, params, body)
            if is_ctor and not params:
                cls.static_ctor = md
            else:
                cls.methods.append(md)
        else:
            while not p.at(";"):
                p.next()
            p.next()
            cls.fields[name] = ret

    decl_re = re.compile(
        r"^\s*(?:public|private|internal|protected)?\s*static\s+[\w\[\],<>\.\s]+?\s"
        r"(?P<name>@?[A-Za-z_]\w*)\s*\(")
    positions: dict = {}
    for ln, text in enumerate(src.split("\n"), 1):
        m = decl_re.match(text)
        if m:
            positions.setdefault(m.group("name").lstrip("@"), []).append(ln)
    for md in cls.methods + ([cls.static_ctor] if cls.static_ctor else []):
        got = positions.get(md.name)
        if got:
            md.line = got.pop(0)
    return cls


def attach_orig_names(cls: ParsedClass, info: dict):
    """Label methods with their Dotfuscator original names (by full signature)."""
    for md in cls.methods + ([cls.static_ctor] if cls.static_ctor else []):
        cs_types = [t for t, _ in md.params]
        for orig, obf, sig in info["methods"]:
            if obf.lstrip("@") != md.name:
                continue
            il = _il_params(sig)
            if il is not None and il == cs_types:
                md.orig = orig
                break


# ==========================================================================
# 4. Type inference + overload resolution
# ==========================================================================

def lit_type(tok: str) -> TypeRef:
    if tok[:2].lower() == "0x":
        body = tok
        while body and body[-1] in "uUlL":
            body = body[:-1]
        return INT if int(body, 16) <= 0x7FFFFFFF else TypeRef("uint")
    u = tok.upper()
    if u.endswith("UL") or u.endswith("LU"):
        return TypeRef("ulong")
    if u.endswith("L"):
        return LONG
    if u.endswith("F"):
        return TypeRef("float")
    if u.endswith("D"):
        return DOUBLE
    if "." in tok:
        return DOUBLE
    if u.endswith("U"):
        return INT if int(tok[:-1], 10) <= 0x7FFFFFFF else TypeRef("uint")
    return INT


_RANK = {"byte": 1, "sbyte": 1, "short": 1, "ushort": 1, "char": 1, "int": 2,
         "uint": 3, "long": 4, "ulong": 5, "float": 6, "double": 7, "decimal": 8}


def promote(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a.rank or b.rank:
        return a if a.rank else b
    ra, rb = _RANK.get(a.name, 2), _RANK.get(b.name, 2)
    if ra < 2 and rb < 2:
        return INT
    return a if ra >= rb else b


class Typer:
    def __init__(self, cls: ParsedClass):
        self.cls = cls
        self.by_name: dict = {}
        for md in cls.methods:
            self.by_name.setdefault(md.name, []).append(md)
        self.errors: list = []

    def infer(self, e, env):
        t = self._infer(e, env)
        if isinstance(e, Expr) and e.type is None:
            e.type = t
        return t

    def _infer(self, e, env):
        if isinstance(e, Lit):
            if e.kind == "num":
                return lit_type(e.tok)
            if e.kind == "str":
                return STRING
            return TypeRef("char")
        if isinstance(e, Ident):
            if e.name in env:
                return env[e.name]
            for key in (e.name, "m_" + e.name):
                if key in self.cls.fields:
                    return self.cls.fields[key]
            return None
        if isinstance(e, FieldRef):
            for key in (e.name, "m_" + e.name):
                if key in self.cls.fields:
                    return self.cls.fields[key]
            return None
        if isinstance(e, Member):
            if e.name == "Length":
                return INT
            self.infer(e.obj, env)
            return None
        if isinstance(e, Index):
            ot = self.infer(e.obj, env)
            for i in e.idx:
                self.infer(i, env)
            if ot is None:
                return None
            return TypeRef(ot.name, max(0, ot.rank - 1))
        if isinstance(e, Cast):
            self.infer(e.expr, env)
            return e.ty
        if isinstance(e, Unary):
            inner = self.infer(e.expr, env)
            return BOOL if e.op == "!" else (inner if e.op in ("-", "~") else inner)
        if isinstance(e, Checked):
            return self.infer(e.inner, env)
        if isinstance(e, NewArray):
            for d in e.dims:
                self.infer(d, env)
            if e.init:
                _walk_init(e.init, self, env)
            return e.ty
        if isinstance(e, Ternary):
            self.infer(e.cond, env)
            return promote(self.infer(e.a, env), self.infer(e.b, env))
        if isinstance(e, Assign):
            self.infer(e.value, env)
            return self.infer(e.target, env)
        if isinstance(e, Binary):
            a = self.infer(e.left, env)
            b = self.infer(e.right, env)
            if e.op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||"):
                return BOOL
            return promote(a, b)
        if isinstance(e, Call):
            return self._infer_call(e, env)
        return None

    def _infer_call(self, e, env):
        tgt = e.target
        if isinstance(tgt, FieldRef) and tgt.name == "h":
            for a in e.args:
                self.infer(a, env)
            return STRING
        name = None
        if isinstance(tgt, Ident):
            name = tgt.name
        elif isinstance(tgt, FieldRef):
            name = tgt.name
        elif isinstance(tgt, Member):
            ot = self.infer(tgt.obj, env)
            for a in e.args:
                self.infer(a, env)
            if tgt.name in ("Floor", "Ceiling", "Round"):
                return DOUBLE
            if tgt.name in ("Substring", "ToString", "Intern", "Format"):
                return STRING
            if tgt.name in ("IndexOf",):
                return INT
            if tgt.name in ("StartsWith", "EndsWith", "Contains"):
                return BOOL
            if tgt.name in ("GetLength",):
                return INT
            if tgt.name in ("Parse",):
                return ot
            return None
        if name in ("Math", "Convert", "Encoding", "Array", "Enum", "BitConverter"):
            for a in e.args:
                self.infer(a, env)
            return OBJECT
        if name in _PRIMITIVES:
            for a in e.args:
                self.infer(a, env)
            return TypeRef(name)
        argtypes = [self.infer(a, env) for a in e.args]
        md = self.resolve(name, argtypes, e)
        if md is None:
            return None
        e.method = method_id(md)
        return md.ret

    def resolve(self, name, argtypes, call=None):
        cands = self.by_name.get(name)
        if not cands:
            return None
        cands = [c for c in cands if len(c.params) == len(argtypes)]
        if not cands:
            return None
        scored = []
        for c in cands:
            order = _order_args(call, c) if call is not None else None
            if call is not None and any(call.named) and order is None:
                continue
            types = argtypes if order is None else \
                [self._type_of(a) for a in order]
            s = _score(c.params, types)
            if s is None:
                continue
            scored.append((s, c, order))
        if not scored:
            return cands[0] if len(cands) == 1 else None
        scored.sort(key=lambda x: -x[0])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            self.errors.append(
                f"ambiguous call {name}({', '.join(str(t) for t in argtypes)})")
        if call is not None and scored[0][2] is not None:
            call.ordered = scored[0][2]
        return scored[0][1]

    @staticmethod
    def _type_of(e):
        return e.type if isinstance(e, Expr) else None


def _order_args(call, cand):
    """Reorder a call's arguments into parameter order (ILSpy named arguments)."""
    if call is None or not any(call.named):
        return None
    pnames = [pn for _, pn in cand.params]
    slots = [None] * len(pnames)
    pos = 0
    for nm, a in zip(call.named, call.args):
        if nm is None:
            while pos < len(slots) and slots[pos] is not None:
                pos += 1
            if pos >= len(slots):
                return None
            slots[pos] = a
            pos += 1
        else:
            if nm not in pnames or slots[pnames.index(nm)] is not None:
                return None
            slots[pnames.index(nm)] = a
    return None if any(s is None for s in slots) else slots


def _score(params, argtypes):
    score = 0
    for (pty, _pn), at in zip(params, argtypes):
        if at is None:
            score += 1
            continue
        if pty == at:
            score += 8
            continue
        if pty.name == "long" and at.name in ("int", "uint", "byte", "short", "char", "ushort"):
            score += 4
            continue
        if pty.name == "int" and at.name in ("byte", "short", "char", "ushort"):
            score += 6
            continue
        if pty.name == "uint" and at.name in ("int", "byte", "short"):
            score += 3
            continue
        if pty.name == "double" and at.name in ("int", "long", "float", "byte"):
            score += 2
            continue
        if pty.name == "Object":
            score += 1
            continue
        return None
    return score


def _walk_init(items, typer, env):
    for it in items:
        if isinstance(it, list):
            _walk_init(it, typer, env)
        elif isinstance(it, Node):
            typer.infer(it, env)


C_TYPE_CODE = {"int": "i", "long": "l", "byte": "b", "bool": "z", "string": "s",
               "double": "d", "char": "c", "void": "v", "object": "o"}


def method_id(md: MethodDecl) -> str:
    if md.py_name is None:
        base = re.sub(r"\W", "_", md.orig or md.name)
        sig = "".join(C_TYPE_CODE.get(t.name, "x") + "a" * t.rank
                      for t, _ in md.params)
        md.py_name = f"{base}__{sig}" if sig else base
    return md.py_name


# ==========================================================================
# 5. Python emitter
# ==========================================================================

PY_RUNTIME = '''\
# --- C# integer semantics helpers (generated by tools/extract_seedkey.py) ---
_M64 = (1 << 64) - 1
_M32 = (1 << 32) - 1


def i64(v):
    v &= _M64
    return v - (_M64 + 1) if v >> 63 else v


def i32(v):
    v &= _M32
    return v - (_M32 + 1) if v >> 31 else v


def divtz(a, b):
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def modtz(a, b):
    return a - divtz(a, b) * b


def _zeros(n):
    return [0] * n


def __floor(x):
    return int(x // 1)


def __ceil(x):
    import math
    return int(math.ceil(x))
'''


def _iter_nodes(node):
    """Yield the node itself plus every nested Node, lists included."""
    if isinstance(node, Node):
        yield node
        for attr in vars(node).values():
            yield from _iter_nodes(attr)
    elif isinstance(node, (list, tuple)):
        for x in node:
            yield from _iter_nodes(x)


def collect_h_keys(body):
    """Find the per-method ``int a_ = N;`` obfuscation keys of a body."""
    keys = {}
    for node in _iter_nodes(body):
        if isinstance(node, Decl) and node.ty is not None \
                and node.ty.name == "int" and isinstance(node.init, Lit) \
                and node.init.kind == "num":
            try:
                keys[node.name] = int(node.init.tok.rstrip("Ll"))
            except ValueError:
                pass
    return keys


def _has_incdec(node):
    for n in _iter_nodes(node):
        if isinstance(n, Unary) and n.op in ("++post", "--post", "++pre", "--pre"):
            return True
    return False


def assigned_fields(body):
    """Field names assigned anywhere in a method body."""
    out = set()
    for n in _iter_nodes(body):
        if isinstance(n, Assign):
            tgt = n.target
            if isinstance(tgt, FieldRef):
                out.add(tgt.name)
                out.add("m_" + tgt.name)
            elif isinstance(tgt, Ident):
                out.add(tgt.name)
                out.add("m_" + tgt.name)
    return out


def method_id_of(name, orig, params):
    base = re.sub(r"\W", "_", orig or name)
    sig = "".join(C_TYPE_CODE.get(t.name, "x") + "a" * t.rank
                  for t, _ in params)
    return f"{base}__{sig}" if sig else base


class Emitter:
    def __init__(self, cls: ParsedClass, typer: Typer):
        self.cls = cls
        self.typer = typer
        self.needed: list = []
        self.seen: set = set()
        self.needs_static_ctor = False
        self.h_keys: dict = {}
        self.counter = 0

    # -- closure
    def find(self, name):
        for md in self.cls.methods:
            if md.orig == name or md.name == name:
                return md
        raise CsError(f"function {name!r} not found in the source")

    def collect(self, name):
        target = self.find(name)
        self.walk(target)
        return target

    def walk(self, md):
        if id(md) in self.seen:
            return
        self.seen.add(id(md))
        env = {pn: pt for pt, pn in md.params}
        self.h_keys = collect_h_keys(md.body)
        self.type_stmt(md.body, env)
        method_id(md)
        self.needed.append(md)
        for call in _iter_nodes(md.body):
            if isinstance(call, Call) and call.method:
                sub = self.by_py_name(call.method)
                if sub is not None:
                    self.walk(sub)

    def by_py_name(self, py_name):
        for md in self.cls.methods:
            if method_id(md) == py_name:
                return md
        return None

    def type_stmt(self, node, env):
        tp = self.typer
        if isinstance(node, Block):
            for s in node.body:
                self.type_stmt(s, env)
        elif isinstance(node, Decl):
            if node.init is not None:
                tp.infer(node.init, env)
            env[node.name] = node.ty
        elif isinstance(node, ExprStmt):
            tp.infer(node.expr, env)
        elif isinstance(node, If):
            tp.infer(node.cond, env)
            self.type_stmt(node.then, env)
            if node.els is not None:
                self.type_stmt(node.els, env)
        elif isinstance(node, While):
            tp.infer(node.cond, env)
            self.type_stmt(node.body, env)
        elif isinstance(node, For):
            if node.init is not None:
                self.type_stmt(node.init, env)
            if node.cond is not None:
                tp.infer(node.cond, env)
            if node.step is not None:
                tp.infer(node.step, env)
            self.type_stmt(node.body, env)
        elif isinstance(node, Switch):
            tp.infer(node.disc, env)
            for case in node.cases:
                tp.infer(case[0], env)
                for s in case[1]:
                    self.type_stmt(s, env)
            for s in node.default or []:
                self.type_stmt(s, env)
        elif isinstance(node, Return):
            if node.value is not None:
                tp.infer(node.value, env)
        elif isinstance(node, Try):
            self.type_stmt(node.body, env)
            for h in node.handlers:
                self.type_stmt(h, env)
            if node.final:
                self.type_stmt(node.final, env)
        elif isinstance(node, Throw):
            tp.infer(node.expr, env)

    # -- ++/-- hoisting ------------------------------------------------
    #
    # Python has no pre/post increment, so `a[i++]` and `while (n-- > 0)` are
    # rewritten into an explicit temporary plus an update, placed exactly where
    # C# evaluates them (before the condition test, or before the statement).

    def hoist(self, holder, env, attrs):
        """Replace ++/-- inside the given attributes; return prologue lines."""
        lines = []

        def walk(e):
            if e is None or not isinstance(e, Node):
                return e
            if isinstance(e, Unary) and e.op in ("++post", "--post", "++pre", "--pre"):
                e.expr = walk(e.expr)
                tgt = self.expr(e.expr, env)
                self.counter += 1
                tmp = f"_t{self.counter}"
                op = "+" if e.op.startswith("++") else "-"
                if e.op.endswith("post"):
                    lines.append(f"{tmp} = {tgt}")
                    lines.append(f"{tgt} = {tgt} {op} 1")
                else:
                    lines.append(f"{tgt} = {tgt} {op} 1")
                    lines.append(f"{tmp} = {tgt}")
                env[tmp] = e.type or INT
                return Ident(tmp)
            if isinstance(e, Ternary):
                if _has_incdec(e):
                    raise CsError("++/-- inside a ?: expression is not supported")
                e.cond = walk(e.cond)
                e.a = walk(e.a)
                e.b = walk(e.b)
                return e
            for attr, val in list(vars(e).items()):
                if attr in ("type", "method", "ordered", "named"):
                    continue
                if isinstance(val, Node):
                    setattr(e, attr, walk(val))
                elif isinstance(val, list):
                    for i, item in enumerate(val):
                        if isinstance(item, Node):
                            val[i] = walk(item)
            return e

        for attr in attrs:
            val = getattr(holder, attr, None)
            if isinstance(val, Node):
                setattr(holder, attr, walk(val))
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, Node):
                        val[i] = walk(item)
        return lines

    # -- emission
    def emit(self, target) -> str:
        chunks = [PY_RUNTIME]
        bodies = []
        for md in self.needed:
            self.h_keys = collect_h_keys(md.body)
            bodies.append(self.emit_method(md))
        head = ["", "# --- static fields of the decompiled class ---"]
        for fname, fty in self.cls.fields.items():
            head.append(f"{fname} = {default_value(fty)}")
        static_call = []
        if self.needs_static_ctor and self.cls.static_ctor is not None:
            sc = self.cls.static_ctor
            self.h_keys = collect_h_keys(sc.body)
            head.append("")
            head.append(self.render("_static_init", "static constructor", None,
                                    [], sc.body, sc.line, {}))
            static_call = ["", "", "_static_init()"]
        chunks.extend(head)
        for text in bodies:
            chunks.append("")
            chunks.append(text)
        chunks.extend(static_call)
        chunks.append("")
        chunks.append(self.facade(target))
        return "\n".join(chunks) + "\n"

    def emit_method(self, md) -> str:
        env = {pn: pt for pt, pn in md.params}
        return self.render(md.name, md.orig, md.ret, md.params, md.body,
                           md.line, env)

    def render(self, name, orig, ret, params, body, line, env) -> str:
        params_txt = ", ".join(py_local(pn) for _, pn in params)
        head = f"# {orig or name}   (decompiled source line {line})"
        lines = [f"def {method_id_of(name, orig, params)}({params_txt}):"]
        g = sorted({f for f in assigned_fields(body) if f in self.cls.fields})
        if g:
            lines.append("    global " + ", ".join(g))
        lines.extend(self.emit_block(body, 1, env) or ["    pass"])
        if ret is None or ret.name == "void" or ret.rank:
            lines.append("    return None")
        elif ret.name == "bool":
            lines.append("    return False")
        elif ret.name == "string":
            lines.append('    return ""')
        else:
            lines.append("    return 0")
        return "\n".join([head] + lines)

    def facade(self, target) -> str:
        return (
            "# --- public interface -------------------------------------------------\n"
            "def _seed_bytes(seed):\n"
            "    if isinstance(seed, (bytes, bytearray)):\n"
            "        return bytearray(seed)\n"
            "    if isinstance(seed, int):\n"
            "        return bytearray((seed >> s) & 0xFF for s in (24, 16, 8, 0))\n"
            "    return bytearray(seed)\n"
            "\n"
            "\n"
            "def key_bytes(seed, **ctx):\n"
            '    """Security-access key for this ECU as bytes."""\n'
            f"    out = {method_id(target)}(_seed_bytes(seed))\n"
            '    return bytes(out or b"")\n'
            "\n"
            "\n"
            "def key_for(seed, **ctx):\n"
            '    """Security-access key as an int (as sent in service 0x27)."""\n'
            '    return int.from_bytes(key_bytes(seed), "big")\n'
        )

    def emit_block(self, node, indent, env):
        out = []
        for s in node.body:
            out.extend(self.emit_stmt(s, indent, env))
        return out

    def emit_stmt(self, node, indent, env):
        pad = "    " * indent
        if isinstance(node, Block):
            return self.emit_block(node, indent, env)
        if isinstance(node, Decl):
            env[node.name] = node.ty
            pre = self.hoist(node, env, ["init"])
            val = default_value(node.ty) if node.init is None \
                else self.expr(node.init, env)
            return [pad + x for x in pre] + [f"{pad}{py_local(node.name)} = {val}"]
        if isinstance(node, ExprStmt):
            pre = self.hoist(node, env, ["expr"])
            return [pad + x for x in pre] + [f"{pad}{self.expr(node.expr, env)}"]
        if isinstance(node, If):
            pre = self.hoist(node, env, ["cond"])
            out = [pad + x for x in pre]
            out.append(f"{pad}if {self.expr(node.cond, env)}:")
            out.extend(self.emit_stmt(node.then, indent + 1, dict(env))
                       or [pad + "    pass"])
            if node.els is not None:
                out.append(f"{pad}else:")
                out.extend(self.emit_stmt(node.els, indent + 1, dict(env))
                           or [pad + "    pass"])
            return out
        if isinstance(node, While):
            pre = self.hoist(node, env, ["cond"])
            step = self.emit_stmt(node.body, indent + 1, dict(env))
            if node.do_first:
                out = [f"{pad}while True:"]
                out.extend(step or [pad + "    pass"])
                out.extend(pad + "    " + x for x in pre)
                out.append(f"{pad}    if not ({self.expr(node.cond, env)}):")
                out.append(f"{pad}        break")
                return out
            if pre:
                out = [f"{pad}while True:"]
                out.extend(pad + "    " + x for x in pre)
                out.append(f"{pad}    if not ({self.expr(node.cond, env)}):")
                out.append(f"{pad}        break")
                out.extend(step or [pad + "    pass"])
                return out
            out = [f"{pad}while {self.expr(node.cond, env)}:"]
            out.extend(step or [pad + "    pass"])
            return out
        if isinstance(node, For):
            pre = []
            if isinstance(node.init, Decl):
                env[node.init.name] = node.init.ty
                pre += [pad + x for x in self.hoist(node.init, env, ["init"])]
                if node.init.init is not None:
                    pre.append(f"{pad}{py_local(node.init.name)} = "
                               f"{self.expr(node.init.init, env)}")
            elif isinstance(node.init, ExprStmt):
                pre += [pad + x for x in self.hoist(node.init, env, ["expr"])]
                pre.append(f"{pad}{self.expr(node.init.expr, env)}")
            cpre = self.hoist(node, env, ["cond"]) if node.cond is not None else []
            inner = self.emit_stmt(node.body, indent + 1, dict(env))
            if node.step is not None:
                inner.extend(pad + "    " + x for x in self.hoist(node.step, env, ["step"]))
                inner.append(f"{pad}    {self.expr(node.step, env)}")
            if cpre:
                out = pre + [f"{pad}while True:"]
                out.extend(pad + "    " + x for x in cpre)
                out.append(f"{pad}    if not ({self.expr(node.cond, env)}):")
                out.append(f"{pad}        break")
                out.extend(inner or [pad + "    pass"])
                return out
            cond = self.expr(node.cond, env) if node.cond is not None else "True"
            out = pre + [f"{pad}while {cond}:"]
            out.extend(inner or [pad + "    pass"])
            return out
        if isinstance(node, Switch):
            self.counter += 1
            v = f"_sw{self.counter}"
            out = [f"{pad}{v} = {self.expr(node.disc, env)}",
                   f"{pad}_matched = False", f"{pad}_jumped = False"]
            tail = []
            for case in node.cases:
                out.append(f"{pad}if not _matched and {v} == "
                           f"{self.expr(case[0], env)}:")
                out.append(f"{pad}    _matched = True")
                out.extend(self.emit_stmt(Block(case[1]), indent + 1, dict(env))
                           or [pad + "    pass"])
            # C# switch falls through unless a case ends in break/return;
            # ILSpy emits explicit `break`, so a straight if-chain is faithful.
            if node.default is not None:
                out.append(f"{pad}if not _matched:")
                out.extend(self.emit_stmt(Block(node.default), indent + 1, dict(env))
                           or [pad + "    pass"])
            out.extend(tail)
            return out
        if isinstance(node, Return):
            return [f"{pad}return " + ("None" if node.value is None
                                       else self.expr(node.value, env))]
        if isinstance(node, Control):
            if node.kind == "break":
                return [f"{pad}break"]
            return [f"{pad}continue"]
        if isinstance(node, Throw):
            return [f"{pad}raise RuntimeError({self.expr(node.expr, env)})"]
        if isinstance(node, Try):
            out = [f"{pad}try:"]
            out.extend(self.emit_block(node.body, indent + 1, dict(env))
                       or [pad + "    pass"])
            for h in node.handlers:
                out.append(f"{pad}except Exception:")
                out.extend(self.emit_block(h, indent + 1, dict(env))
                           or [pad + "    pass"])
            if node.final:
                out.append(f"{pad}finally:")
                out.extend(self.emit_block(node.final, indent + 1, dict(env))
                           or [pad + "    pass"])
            return out
        raise CsError(f"cannot emit {node.__class__.__name__}")

    # -- expressions
    def expr(self, e, env):
        if isinstance(e, Lit):
            if e.kind == "num":
                return emit_number(e.tok)
            if e.kind == "str":
                return json.dumps(self.decode(e.tok))
            return str(ord(unescape_csharp(e.tok[1:-1])))
        if isinstance(e, Ident):
            if e.name == "this":
                return "_self"
            if e.name in env:
                return py_local(e.name)
            for key in (e.name, "m_" + e.name):
                if key in self.cls.fields:
                    self.needs_static_ctor = True
                    return key
            return py_local(e.name)
        if isinstance(e, FieldRef):
            for key in (e.name, "m_" + e.name):
                if key in self.cls.fields:
                    self.needs_static_ctor = True
                    return key
            return py_local(e.name)
        if isinstance(e, Cast):
            return self.cast(e, env)
        if isinstance(e, Unary):
            inner = self.expr(e.expr, env)
            t = self.typer.infer(e.expr, env)
            if e.op == "!":
                return f"(not {inner})"
            if e.op == "-":
                return wrap(t, f"-({inner})")
            if e.op == "~":
                return wrap(t, f"~({inner})")
            return inner
        if isinstance(e, Checked):
            return self.expr(e.inner, env)
        if isinstance(e, Binary):
            return self.binary(e, env)
        if isinstance(e, Ternary):
            return (f"({self.expr(e.a, env)} if {self.expr(e.cond, env)} "
                    f"else {self.expr(e.b, env)})")
        if isinstance(e, Assign):
            return self.assign(e, env)
        if isinstance(e, NewArray):
            return self.new_array(e, env)
        if isinstance(e, Index):
            obj = self.expr(e.obj, env)
            parts = []
            for i in e.idx:
                if isinstance(i, Unary) and i.op == "^":
                    parts.append(f"len({obj}) - ({self.expr(i.expr, env)})")
                else:
                    parts.append(self.expr(i, env))
            return f"{obj}[" + ", ".join(parts) + "]"
        if isinstance(e, Member):
            if e.name == "Length":
                return f"len({self.expr(e.obj, env)})"
            if isinstance(e.obj, Ident) and e.obj.name in _PRIMITIVES:
                if e.name == "MaxValue":
                    return str(_PRIM_MAX[e.obj.name])
                if e.name == "MinValue":
                    return str(_PRIM_MIN[e.obj.name])
            return f"{self.expr(e.obj, env)}.{e.name}"
        if isinstance(e, Call):
            return self.call(e, env)
        raise CsError(f"cannot emit expression {e.__class__.__name__}")

    def decode(self, tok):
        if tok.startswith('@"'):
            return tok[2:-1].replace('""', '"')
        return h_decode(unescape_csharp(tok[1:-1]), self.cur_key)

    def decode_h(self, call):
        """Decode one ``b.h("literal", <keyVar>)`` call."""
        tok = None
        if call.args and isinstance(call.args[0], Lit) and call.args[0].kind == "str":
            tok = call.args[0].tok
        if tok is None:
            raise CsError("b.h() with a non-literal argument")
        key = 0
        if len(call.args) > 1:
            a1 = call.args[1]
            if isinstance(a1, Ident):
                key = int(self.h_keys.get(a1.name, 0))
            elif isinstance(a1, Lit) and a1.kind == "num":
                key = int(a1.tok.rstrip("Ll"))
        return h_decode(unescape_csharp(tok[1:-1]), key)

    cur_key = 0

    def cast(self, e, env):
        inner = self.expr(e.expr, env)
        n = e.ty.name
        if e.ty.rank:
            return inner
        if n == "byte":
            return f"({inner} & 0xFF)"
        if n == "int":
            return f"i32({inner})"
        if n == "long":
            return f"i64({inner})"
        if n == "uint":
            return f"({inner} & 0xFFFFFFFF)"
        if n == "short" or n == "ushort":
            return f"i32(({inner}) & 0xFFFF)"
        if n in ("double", "float"):
            return f"float({inner})"
        return inner

    def new_array(self, e, env):
        base = e.ty.name
        rank = e.ty.rank
        dims = [self.expr(d, env) for d in e.dims]
        if e.init is not None:
            if not any(isinstance(x, list) for x in e.init):
                return "[" + ", ".join(self.array_item(x, env) for x in e.init) + "]"
            return "[" + ", ".join("[" + ", ".join(
                self.array_item(x, env) for x in row) + "]" for row in e.init) + "]"
        if rank == 1 or not dims:
            n = dims[0] if dims else "0"
            if base in ("long", "int"):
                return f"_zeros({n})"
            if base == "byte":
                return f"bytearray({n})"
            return f"[None] * ({n})"
        rows = dims[0]
        cols = dims[1] if len(dims) > 1 else "0"
        fill = "0" if base in ("long", "int") else "None"
        return f"[[{fill}] * ({cols}) for _ in range({rows})]"

    def array_item(self, x, env):
        if isinstance(x, list):
            return "[" + ", ".join(self.array_item(y, env) for y in x) + "]"
        return self.expr(x, env)

    def binary(self, e, env):
        a = self.expr(e.left, env)
        b = self.expr(e.right, env)
        op = e.op
        lt = self.typer.infer(e.left, env)
        rt = self.typer.infer(e.right, env)
        if op in ("&&", "||"):
            return f"({a} {'and' if op == '&&' else 'or'} {b})"
        if op in ("==", "!=", "<", ">", "<=", ">="):
            return f"({a} {op} {b})"
        kind = e.type.name if e.type is not None else None
        if kind == "bool":
            kind = lt.name if lt is not None else None
        if op == "+":
            return wrap(kind, f"({a}) + ({b})")
        if op == "-":
            return wrap(kind, f"({a}) - ({b})")
        if op == "*":
            return wrap(kind, f"({a}) * ({b})")
        if op == "/":
            return wrap(kind, f"divtz(({a}), ({b}))")
        if op == "%":
            return wrap(kind, f"modtz(({a}), ({b}))")
        if op == "&":
            return wrap(kind, f"({a}) & ({b})")
        if op == "|":
            return wrap(kind, f"({a}) | ({b})")
        if op == "^":
            return wrap(kind, f"({a}) ^ ({b})")
        is32 = (lt is not None and lt.name in ("int", "uint", "byte", "short",
                                               "ushort", "char")) or \
               (lt is None and rt is not None and rt.name == "int")
        if op == "<<":
            return (f"i32(({a}) << (({b}) & 31))" if is32
                    else wrap(kind, f"({a}) << ({b})"))
        if op == ">>":
            return (f"i32(({a}) >> (({b}) & 31))" if is32
                    else wrap(kind, f"({a}) >> ({b})"))
        raise CsError(f"unsupported operator {op}")

    def assign(self, e, env):
        target = self.expr(e.target, env)
        if e.op == "=":
            return f"{target} = {self.expr(e.value, env)}"
        inner = Binary(e.op[:-1], e.target, e.value)
        inner.type = self.typer.infer(e.target, env)
        return f"{target} = {self.expr(inner, env)}"

    def call(self, e, env):
        tgt = e.target
        args = [self.expr(a, env) for a in (e.ordered or e.args)]
        if isinstance(tgt, FieldRef) and tgt.name == "h":
            return json.dumps(self.decode_h(e))
        if isinstance(tgt, Member):
            obj = self.expr(tgt.obj, env)
            if isinstance(tgt.obj, Ident) and tgt.obj.name == "Math":
                if tgt.name == "Floor":
                    return f"__floor({obj})".replace("Math.", "")
                if tgt.name == "Ceiling":
                    return "__ceil(" + ", ".join(args) + ")"
                if tgt.name == "Abs":
                    return "abs(" + ", ".join(args) + ")"
            nm = tgt.name
            if nm == "Substring":
                return f"{obj}[{', '.join(args)}]"
            if nm == "IndexOf":
                return f"{obj}.find({', '.join(args)})"
            if nm == "StartsWith":
                return f"{obj}.startswith({args[0]})"
            if nm == "EndsWith":
                return f"{obj}.endswith({args[0]})"
            if nm == "Contains":
                return f"({args[0]} in {obj})"
            if nm == "ToString":
                return ("str(" + obj + ")" if not args
                        else f"str({obj})")
            if nm == "Parse":
                return f"int({args[0]}, 10)"
            if nm == "GetLength":
                return f"len({self.expr(tgt.obj, env)})"
            raise CsError(f"unsupported member call .{nm}()")
        if isinstance(tgt, Ident) and tgt.name in _PRIMITIVES:
            if tgt.name in ("long", "int"):
                return f"int({args[0]}, 10)"
            return f"int({', '.join(args)})"
        if isinstance(tgt, FieldRef):
            nm = tgt.name
            if nm == "Floor":
                return f"__floor({args[0]})"
            if nm == "Ceiling":
                return f"__ceil({args[0]})"
            if nm == "Intern":
                return args[0]
            if nm == "Parse":
                return f"int({args[0]}, 10)"
            if nm in ("ToByte", "ToInt32", "ToInt16", "ToUInt32", "ToInt64"):
                return f"int({', '.join(args)}, 0)" if len(args) > 1 \
                    else f"int({args[0]})"
            if nm == "ToDouble":
                return f"float({args[0]})"
            if nm == "GetString":
                return f"bytes({args[0]}).decode('latin-1')"
            if nm == "GetBytes":
                return f"bytearray({args[0]}.encode('latin-1'))"
        if e.method:
            return f"{e.method}({', '.join(args)})"
        name = None
        if isinstance(tgt, Ident):
            name = tgt.name
        elif isinstance(tgt, FieldRef):
            name = tgt.name
        if name:
            argtypes = [self.typer.infer(a, env) for a in e.args]
            md = self.typer.resolve(name, argtypes)
            if md is not None:
                e.method = method_id(md)
                return f"{e.method}({', '.join(args)})"
        raise CsError(f"cannot resolve call to {name!r}")


_PRIM_MAX = {"int": 2147483647, "long": 2 ** 63 - 1, "byte": 255, "short": 32767,
             "ushort": 65535, "uint": 4294967295, "char": 65535}
_PRIM_MIN = {"int": -2147483648, "long": -(2 ** 63), "byte": 0, "short": -32768,
             "ushort": 0, "uint": 0, "char": 0}


def wrap(kind, s):
    if kind == "int":
        return f"i32({s})"
    if kind == "long":
        return f"i64({s})"
    return s


def emit_number(tok):
    if tok[:2].lower() == "0x":
        body = tok
        while body and body[-1] in "uUlL":
            body = body[:-1]
        return str(int(body, 16))
    u = tok.upper()
    for suf in ("UL", "LU", "L", "U", "F", "D", "M"):
        if u.endswith(suf):
            body = tok[: -len(suf)]
            break
    else:
        body = tok
    if "." in body:
        return repr(float(body))
    return str(int(body, 10))


_PY_KEYWORDS = set("""False None True and as assert async await break class continue
def del elif else except finally for from global if import in is lambda nonlocal not
or pass raise return try while with yield""".split())


def py_local(name):
    n = re.sub(r"\W", "_", name)
    if n in _PY_KEYWORDS or (n and n[0].isdigit()):
        n = "v_" + n
    return n


def default_value(t):
    if t is None:
        return "None"
    if t.rank:
        return "[]"
    if t.name == "bool":
        return "False"
    if t.name == "string":
        return '""'
    if t.name in ("long", "int", "byte", "short", "uint", "ushort"):
        return "0"
    if t.name in ("double", "float"):
        return "0.0"
    return "None"


# ==========================================================================
# 6. Algorithm-name parsing
# ==========================================================================

def parse_key_name(name: str) -> dict:
    info = {"func": name, "level": None, "ecu": None, "supplier": None,
            "variant": None, "algid": None, "rev": None, "tagged_pn": False,
            "recognised": False}
    m = re.match(r"^Calculate(?:Key)?(?P<rest>.*)$", name)
    if not m or not m.group("rest"):
        return info
    tokens = m.group("rest").split("_")
    if tokens and re.fullmatch(r"S\d+", tokens[0]):
        info["level"] = int(tokens[0][1:])
        tokens = tokens[1:]
    if not tokens:
        return info
    cut = None
    for i, tk in enumerate(tokens):
        if tk.startswith("Sup") and len(tk) > 3:
            cut = i
            break
        if tk.lower() == "rev":
            cut = i
            break
    if cut is None:
        info["ecu"] = "_".join(tokens)
        info["recognised"] = bool(tokens)
        return info
    info["ecu"] = "_".join(tokens[:cut])
    tail = tokens[cut:]
    i = 0
    while i < len(tail):
        tk = tail[i]
        if tk.startswith("Sup") and len(tk) > 3:
            info["supplier"] = tk[3:].upper()
        elif tk.startswith("Var"):
            info["variant"] = tk[3:]
        elif re.match(r"(?i)^alg(id)?", tk):
            v = re.sub(r"(?i)^alg(id)?", "", tk)
            if not v and i + 1 < len(tail):
                i += 1
                v = tail[i]
            info["algid"] = v
        elif tk.lower() == "rev" and i + 1 < len(tail):
            i += 1
            info["rev"] = tail[i]
        elif re.fullmatch(r"[vV]\d+", tk):
            info["rev"] = tk[1:]
        i += 1
    info["tagged_pn"] = "_PN" in (info["ecu"] or "")
    info["recognised"] = True
    return info


def iter_key_functions(cls: ParsedClass):
    for md in cls.methods:
        name = md.orig or md.name
        if not name.startswith("Calculate"):
            continue
        info = parse_key_name(name)
        info["obf"] = md.name
        info["line"] = md.line
        info["arity"] = len(md.params)
        yield md, info


# ==========================================================================
# 7. Selector recovery
# ==========================================================================

_ECUTYPE_RE = re.compile(r"ECUTYPE_([A-Za-z0-9_]+)")
_SELECTOR_FIELDS = {"c2", "cx", "cy", "cv", "m_c0", "m_ge", "m_aj", "m_ak"}


class SelectorWalk:
    def __init__(self, key_map):
        self.key_map = key_map
        self.rows = []
        self.h_keys = {}

    def run(self, body):
        self.walk(body, [])
        return self.rows

    def walk(self, node, conds):
        if isinstance(node, Block):
            for s in node.body:
                self.walk(s, conds)
            return
        if isinstance(node, If):
            guard = self.guard(node.cond)
            inner = conds + ([guard] if guard else [])
            body = node.then.body if isinstance(node.then, Block) else [node.then]
            fn = self.single_call(body)
            if fn:
                self.rows.append(self.row(conds, guard, fn))
            self.walk(node.then, inner)
            if node.els is not None:
                self.walk(node.els, conds)
            return
        if isinstance(node, Switch):
            if self.member(node.disc) == "m_aj":
                for case in node.cases:
                    lv = self.const_int(case[0])
                    for s in case[1]:
                        self.walk_switch(s, conds, lv)
            else:
                for case in node.cases:
                    for s in case[1]:
                        self.walk(s, conds)
                for s in node.default or []:
                    self.walk(s, conds)
            return
        if isinstance(node, Block):
            return
        if isinstance(node, (For, While, Try)):
            return

    def walk_switch(self, node, conds, level):
        if isinstance(node, Block):
            for s in node.body:
                self.walk_switch(s, conds, level)
            return
        if isinstance(node, If):
            guard = self.guard(node.cond)
            body = node.then.body if isinstance(node.then, Block) else [node.then]
            fn = self.single_call(body)
            if fn:
                self.rows.append(self.row(conds, guard, fn, level))
            self.walk_switch(node.then, conds, level)
            if node.els is not None:
                self.walk_switch(node.els, conds, level)
            return
        if isinstance(node, Switch):
            for case in node.cases:
                for s in case[1]:
                    self.walk_switch(s, conds, level)
            return

    def row(self, conds, guard, fn, level=None):
        ecus, ges = [], []
        for c in conds:
            ecus.extend(c.get("ecus", []))
            if c.get("ge") is not None:
                ges.append(c["ge"])
        vals = list(guard.get("values", [])) if guard else []
        return {"ecu_types": sorted(set(ecus)), "m_ge": sorted(set(ges)),
                "level": level, "selectors": vals or None,
                "obf": fn, "func": self.key_map.get(fn, fn)}

    def single_call(self, body):
        if len(body) != 1:
            return None
        stmt = body[0]
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.expr, Assign):
            return None
        val = stmt.expr.value
        if isinstance(val, Cast):
            val = val.expr
        if isinstance(val, Call):
            if isinstance(val.target, FieldRef):
                return val.target.name
            if isinstance(val.target, Ident):
                return val.target.name
        return None

    @staticmethod
    def member(e):
        if isinstance(e, (Member, FieldRef)):
            return e.name
        return None

    @staticmethod
    def const_int(e):
        if isinstance(e, Lit) and e.kind == "num":
            try:
                return int(e.tok.rstrip("Ll"))
            except ValueError:
                return None
        return None

    def guard(self, cond):
        out = {"ecus": [], "ge": None, "values": []}

        def rec(e):
            if isinstance(e, Binary) and e.op in ("&&", "||"):
                rec(e.left)
                rec(e.right)
                return
            if isinstance(e, Unary) and e.op == "!":
                rec(e.expr)
                return
            for m in _ECUTYPE_RE.finditer(self.text(e)):
                if m.group(1) not in out["ecus"]:
                    out["ecus"].append(m.group(1))
            if isinstance(e, Binary) and e.op == "==":
                for a, b in ((e.left, e.right), (e.right, e.left)):
                    fld = self.member(a)
                    if fld in _SELECTOR_FIELDS:
                        val = self.text(b)
                        if val:
                            out["values"].append({"field": fld, "value": val})
                mm = re.search(r"m_ge\s*==\s*(\d+)", self.text(e))
                if mm:
                    out["ge"] = int(mm.group(1))

        rec(cond)
        if not out["ecus"] and out["ge"] is None and not out["values"]:
            return None
        return out

    def text(self, e, depth=0):
        if depth > 8:
            return ""
        if isinstance(e, Lit):
            return self.decode(e.tok) if e.kind == "str" else (
                e.tok if e.kind == "num" else "")
        if isinstance(e, Ident):
            if e.name in self.h_keys:
                return str(self.h_keys[e.name])
            return ""
        if isinstance(e, Call) and isinstance(e.target, FieldRef) \
                and e.target.name == "h":
            lit = None
            if e.args and isinstance(e.args[0], Lit) and e.args[0].kind == "str":
                lit = e.args[0].tok
            key = 0
            if len(e.args) > 1 and isinstance(e.args[1], Ident):
                key = int(self.h_keys.get(e.args[1].name, 0))
            return h_decode(unescape_csharp(lit[1:-1]), key) if lit else ""
        if isinstance(e, Binary):
            if e.op == "+":
                return self.text(e.left, depth + 1) + self.text(e.right, depth + 1)
            return self.text(e.left, depth + 1)
        if isinstance(e, Cast):
            return self.text(e.expr, depth + 1)
        if isinstance(e, Unary):
            return self.text(e.expr, depth + 1)
        if isinstance(e, Ternary):
            return self.text(e.a, depth + 1)
        return ""

    def decode(self, tok):
        if tok.startswith('@"'):
            return tok[2:-1].replace('""', '"')
        return tok


def recover_selector(cls: ParsedClass, key_map: dict) -> dict:
    for md in cls.methods:
        if md.orig == "CalculateKey" or md.name == "CalculateKey":
            w = SelectorWalk(key_map)
            w.h_keys = collect_h_keys(md.body)
            rows = w.run(md.body)
            return {"dispatcher": md.orig or md.name, "line": md.line,
                    "selectors": sorted(_SELECTOR_FIELDS), "rows": rows}
    raise CsError("CalculateKey() dispatcher not found")


# ==========================================================================
# 8. CLI
# ==========================================================================

def load_sources(args):
    src = open(args.source, encoding="utf-8", errors="replace").read()
    class_name = args.class_name
    info = None
    if args.map:
        m = load_map(args.map)
        for obf, meta in m.items():
            if meta["orig"].endswith(".AlfaOBDKeys"):
                class_name = obf
                info = meta
                break
    cls = parse_class(src, class_name)
    if info:
        attach_orig_names(cls, info)
    return cls, src


def load_dispatcher(args):
    text = open(args.selector, encoding="utf-8", errors="replace").read()
    class_name = "af"
    info = None
    if args.map:
        m = load_map(args.map)
        for obf, meta in m.items():
            if meta["orig"].endswith(".AlfaOBD"):
                class_name = obf
                info = meta
                break
    cls = parse_class(text, class_name)
    if info:
        attach_orig_names(cls, info)
    return cls


def cmd_list(args):
    cls, _ = load_sources(args)
    rows = []
    for md, info in iter_key_functions(cls):
        if args.filter and args.filter.lower() not in info["func"].lower():
            continue
        if args.pn and not info["tagged_pn"]:
            continue
        rows.append(info)
    rows.sort(key=lambda r: r["func"])
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{len(rows)} CalculateKey* function(s)")
    print(f"{'function':56s} {'lvl':>3s} {'ecu':20s} {'sup':>6s} {'var':>5s} "
          f"{'algID':>7s} rev")
    for r in rows:
        print(f"{r['func']:56s} {str(r['level'] or '-'):>3s} "
              f"{str(r['ecu'] or '-'):20s} {str(r['supplier'] or '-'):>6s} "
              f"{str(r['variant'] or '-'):>5s} {str(r['algid'] or '-'):>7s} "
              f"{str(r['rev'] or '-'):>3s}")
    return 0


def cmd_selector(args):
    cls, _ = load_sources(args)
    key_map = {md.name: (md.orig or md.name) for md, _ in iter_key_functions(cls)}
    af_cls = load_dispatcher(args)
    sel = recover_selector(af_cls, key_map)
    if args.ecu:
        want = args.ecu.upper()
        sel["rows"] = [r for r in sel["rows"]
                       if any(want in e.upper() for e in r["ecu_types"])]
    if args.json:
        print(json.dumps(sel, indent=2))
        return 0
    print(f"# CalculateKey() dispatcher: {sel['dispatcher']} "
          f"(source line {sel['line']})")
    print(f"# {len(sel['rows'])} dispatch row(s)")
    for r in sel["rows"]:
        st = ", ".join(f"{v['field']}={v['value']}" for v in (r["selectors"] or []))
        print(f"{'|'.join(r['ecu_types']) or '-':30s} "
              f"ge={','.join(str(g) for g in r['m_ge']) or '-':6s} "
              f"S{str(r['level'] or '-'):<3s} {st:46s} -> {r['func']}")
    return 0


def cmd_emit(args):
    cls, _ = load_sources(args)
    typer = Typer(cls)
    em = Emitter(cls, typer)
    target = em.collect(args.emit)
    code = em.emit(target)
    header = (
        "# Generated from the user's own copy of AlfaOBD by\n"
        "# tools/extract_seedkey.py -- no third-party code, no vendor constants:\n"
        "# the Python below is a mechanical translation of this one algorithm.\n"
        f"#    algorithm : {target.orig or target.name}\n"
        f"#    source    : {args.source}\n"
        "#\n"
        "# Derived from proprietary vendor material.  Keep it local.\n"
        "\n"
    )
    text = header + code
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.output} ({len(text)} bytes, "
              f"{len(em.needed)} translated method(s))")
    else:
        sys.stdout.write(text)
    return 0


def cmd_emit_harness(args):
    root = args.emit_harness
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    with open(os.path.join(root, "gt.csproj"), "w", encoding="utf-8") as fh:
        fh.write(_HARNESS_CSPROJ)
    src_dir = os.path.join(root, "src")
    with open(os.path.join(src_dir, "hub.cs"), "w", encoding="utf-8") as fh:
        fh.write(_HARNESS_HUB)
    with open(os.path.join(src_dir, "stub_af.cs"), "w", encoding="utf-8") as fh:
        fh.write(_HARNESS_STUB_AF)
    text = open(args.source, encoding="utf-8", errors="replace").read()
    # a method may not share its name with its class; nudge it aside
    text = re.sub(rf"\b({re.escape(args.class_name)})\(byte\[\] A_0\)",
                  r"m_\1_ctor(byte[] A_0)", text)
    with open(os.path.join(src_dir, "algo.cs"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"harness written to {root}\n"
          f"  cd {root} && dotnet build -c Release\n"
          f"  dotnet bin/Release/net10.0/gt.dll <ObfuscatedMethodName> 12 34 56 78")
    return 0


_HARNESS_CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
    <AssemblyName>gt</AssemblyName>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <NoWarn>CS0168;CS0219;CS0414;CS0649;CS0162;CS0169;CS0108;CS0675;CS8981</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="src/*.cs" />
  </ItemGroup>
</Project>
"""

_HARNESS_HUB = '''\
using System;
using System.IO;
using System.Reflection;
using System.Text;

public static class b
{
    // AlfaOBD's b.h(literal, key) decoder, reproduced from the decompiled b.cs.
    public static string h(string A_0, int A_1)
    {
        char[] array = A_0.ToCharArray();
        int num = 1843161013 + A_1;
        for (int i = 0; i < array.Length; i++)
        {
            int c0 = array[i];
            byte lo = (byte)((c0 & 0xFF) ^ num++);
            byte hi = (byte)((c0 >> 8) ^ num++);
            array[i] = (char)((lo << 8) | hi);
        }
        return string.Intern(new string(array));
    }
}

public class Program
{
    static string Hex(byte[] a)
    {
        var s = new StringBuilder();
        foreach (var x in a) s.Append(x.ToString("X2"));
        return s.ToString();
    }

    static byte[] Run(string name, byte[] seed)
    {
        var mi = typeof(ad).GetMethod(name,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
            null, new Type[] { typeof(byte[]) }, null);
        if (mi == null) throw new Exception("method not found: " + name);
        return (byte[])mi.Invoke(null, new object[] { seed });
    }

    public static void Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--batch")
        {
            // stdin: "<Method> <b0> <b1> <b2> <b3> ..." per line.
            string line;
            while ((line = Console.ReadLine()) != null)
            {
                var f = line.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                if (f.Length < 2) continue;
                var seed = new byte[f.Length - 1];
                for (int i = 1; i < f.Length; i++) seed[i - 1] = Convert.ToByte(f[i], 16);
                string res;
                try { res = Hex(Run(f[0], seed)); }
                catch (Exception ex) { res = "ERR:" + ex.GetType().Name; }
                Console.WriteLine(f[0] + " " + res);
            }
            return;
        }
        string name = args[0];
        var s2 = new byte[args.Length - 1];
        for (int i = 1; i < args.Length; i++) s2[i - 1] = Convert.ToByte(args[i], 16);
        Console.WriteLine(Hex(Run(name, s2)));
    }
}
'''

_HARNESS_STUB_AF = '''\
using System;

// Stand-in for AlfaOBD.AlfaOBD.l(string): ASCII hex -> byte[].  The
// big-integer helpers use it when turning a number into a byte array.
public static class af
{
    public static byte[] l(string A_0)
    {
        int n = A_0.Length / 2;
        byte[] r = new byte[n];
        for (int i = 0; i < n; i++) r[i] = Convert.ToByte(A_0.Substring(i * 2, 2), 16);
        return r;
    }
}
'''


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="extract_seedkey.py",
        description="Recover AlfaOBD's FCA/Stellantis seed->key selector and "
                    "generate a Python key provider from your own copy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Typical use")[-1])
    ap.add_argument("--source", required=True,
                    help="decompiled class holding the CalculateKey* methods "
                         "(obfuscated 'ad' = original 'AlfaOBDKeys')")
    ap.add_argument("--map", help="Dotfuscator Map.xml shipped with AlfaOBD.exe")
    ap.add_argument("--class-name", default="ad",
                    help="obfuscated name of the key class (default: ad)")
    ap.add_argument("--list", action="store_true",
                    help="enumerate every CalculateKey* function")
    ap.add_argument("--filter", help="with --list: substring filter")
    ap.add_argument("--pn", action="store_true",
                    help="with --list: only _PN (PowerNet) algorithms")
    ap.add_argument("--selector", metavar="AF_CS",
                    help="decompiled CalculateKey() dispatcher source (class af)")
    ap.add_argument("--ecu", help="with --selector: filter on ECU type, e.g. IPC_PN")
    ap.add_argument("--emit", metavar="FUNCTION",
                    help="transpile FUNCTION into a Python provider")
    ap.add_argument("--emit-harness", metavar="DIR",
                    help="write a dotnet project that runs the ORIGINAL C# so you "
                         "can diff it against the generated provider")
    ap.add_argument("-o", "--output", default="-", help="output file for --emit")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.emit_harness:
            return cmd_emit_harness(args)
        if args.list:
            return cmd_list(args)
        if args.selector:
            return cmd_selector(args)
        if args.emit:
            return cmd_emit(args)
    except CsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
