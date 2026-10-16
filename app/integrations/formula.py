"""Small loss-aware LaTeX AST used by Word OMML and vector PDF rendering.

Unsupported extensions retain the exact source; arbitrary TeX is never executed.
"""
from __future__ import annotations

import re

from app.ai.quality import normalize_legacy_math


class FormulaUnsupported(ValueError):
    pass


SYMBOLS = dict(zip(
    "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi rho sigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega".split(),
    "α β γ δ ε ε ζ η θ ϑ ι κ λ μ ν ξ π ρ σ τ υ φ ϕ χ ψ ω Γ Δ Θ Λ Ξ Π Σ Φ Ψ Ω".split(), strict=True))
SYMBOLS.update({"times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤",
                "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "infty": "∞",
                "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
                "emptyset": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨",
                "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
                "Leftarrow": "⇐", "Leftrightarrow": "⇔", "rightleftharpoons": "⇌", "uparrow": "↑", "downarrow": "↓",
                "angle": "∠", "degree": "°", "perp": "⊥", "parallel": "∥", "triangle": "△", "partial": "∂",
                "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "cdots": "⋯", "dots": "…",
                "ldots": "…", "therefore": "∴", "because": "∵", "vert": "|", "Vert": "‖", "langle": "⟨",
                "rangle": "⟩", "log": "log", "ln": "ln", "sin": "sin", "cos": "cos", "tan": "tan", "lim": "lim",
                "max": "max", "min": "min", "exp": "exp", "det": "det", "mod": "mod", "bmod": "mod"})


def parse_formula(source: str):
    source = normalize_legacy_math(source).strip()
    tokens = re.findall(r"\\[A-Za-z]+|\\.|\s+|[^\s]", source)
    position = 0

    def argument():
        nonlocal position
        while position < len(tokens) and tokens[position].isspace():
            position += 1
        if position >= len(tokens):
            raise FormulaUnsupported("missing formula argument")
        if tokens[position] == "{":
            position += 1
            return row(True)
        return atom()

    def atom():
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "{":
            return row(True)
        if token.startswith("\\"):
            command = token[1:]
            if command in {"frac", "dfrac", "tfrac"}:
                return ("frac", argument(), argument())
            if command == "sqrt":
                return ("sqrt", argument())
            if command in {"text", "textrm", "operatorname"}:
                while position < len(tokens) and tokens[position].isspace():
                    position += 1
                if position >= len(tokens) or tokens[position] != "{":
                    raise FormulaUnsupported("text command requires braces")
                position += 1
                pieces = []
                while position < len(tokens) and tokens[position] != "}":
                    if tokens[position] == "{":
                        raise FormulaUnsupported("nested text groups")
                    pieces.append(tokens[position])
                    position += 1
                if position >= len(tokens):
                    raise FormulaUnsupported("unbalanced text group")
                position += 1
                return ("text", "".join(pieces))
            if command in {"mathrm", "mathbf", "mathit", "boldsymbol"}:
                return argument()
            if command in {"left", "right", "displaystyle", "textstyle", "limits", "nolimits"}:
                return ("text", "")
            if command in {",", ";", ":", " ", "quad", "qquad"}:
                return ("text", " ")
            if command in {"xrightarrow", "xleftarrow"}:
                return ("over", ("text", "→" if command == "xrightarrow" else "←"), argument())
            if command in SYMBOLS:
                return ("text", SYMBOLS[command])
            if command in {"{", "}", "%", "_", "#", "$", "&", "|"}:
                return ("text", command)
            raise FormulaUnsupported(f"unsupported command: {command}")
        if token in {"}", "^", "_"}:
            raise FormulaUnsupported("unexpected formula token")
        return ("text", token)

    def row(nested=False):
        nonlocal position
        nodes = []
        while position < len(tokens):
            if tokens[position].isspace():
                position += 1
                continue
            if tokens[position] == "}":
                if not nested:
                    raise FormulaUnsupported("unbalanced braces")
                position += 1
                return ("row", nodes)
            node = atom()
            sub, sup = None, None
            while position < len(tokens) and tokens[position] in {"^", "_"}:
                marker = tokens[position]
                position += 1
                if marker == "^":
                    sup = argument()
                else:
                    sub = argument()
            nodes.append(("scripts", node, sub, sup) if sub is not None or sup is not None else node)
        if nested:
            raise FormulaUnsupported("unbalanced braces")
        return ("row", nodes)

    return row()


def omml(node):
    from docx.oxml import OxmlElement

    def append_node(parent, value):
        if value[0] == "row":
            for child in value[1]:
                append_node(parent, child)
        else:
            parent.append(omml(value))

    def wrapper(tag, value):
        result = OxmlElement("m:" + tag)
        append_node(result, value)
        return result

    kind = node[0]
    if kind == "text":
        run, text = OxmlElement("m:r"), OxmlElement("m:t")
        text.text = node[1]
        run.append(text)
        return run
    if kind == "row":
        result = OxmlElement("m:e")
        for child in node[1]:
            append_node(result, child)
        return result
    if kind == "frac":
        result = OxmlElement("m:f")
        result.extend([wrapper("num", node[1]), wrapper("den", node[2])])
        return result
    if kind == "sqrt":
        result = OxmlElement("m:rad")
        props = OxmlElement("m:radPr")
        hidden = OxmlElement("m:degHide")
        from docx.oxml.ns import qn
        hidden.set(qn("m:val"), "1")
        props.append(hidden)
        result.extend([props, wrapper("e", node[1])])
        return result
    if kind == "scripts":
        base, sub, sup = node[1:]
        result = OxmlElement("m:sSubSup" if sub is not None and sup is not None else "m:sSub" if sub is not None else "m:sSup")
        result.append(wrapper("e", base))
        if sub is not None:
            result.append(wrapper("sub", sub))
        if sup is not None:
            result.append(wrapper("sup", sup))
        return result
    if kind == "over":
        result = OxmlElement("m:limUpp")
        result.extend([wrapper("e", node[1]), wrapper("lim", node[2])])
        return result
    raise FormulaUnsupported("unsupported AST")


def formula_segments(text: str):
    """Yield (is_math, content), preserving unmatched delimiters as source text."""
    pattern = re.compile(r"\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]|(?<!\\)\$\$([\s\S]*?)\$\$|(?<!\\)\$([^\n$]+)\$")
    offset = 0
    for match in pattern.finditer(text):
        if match.start() > offset:
            yield False, text[offset:match.start()]
        yield True, next(group for group in match.groups() if group is not None)
        offset = match.end()
    if offset < len(text):
        yield False, text[offset:]


def math_box(node, font, size, *, cjk_font=None):
    """Return width, ascent, descent and a canvas drawing closure."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    kind = node[0]
    if kind == "text":
        text = node[1]
        pieces = [(character, cjk_font if cjk_font and re.match(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]", character) else font) for character in text]
        width = sum(stringWidth(character, selected_font, size) for character, selected_font in pieces)

        def draw(canvas, x, y):
            for character, selected_font in pieces:
                canvas.setFont(selected_font, size)
                canvas.drawString(x, y, character)
                x += stringWidth(character, selected_font, size)
        return width, size * 0.9, size * 0.25, draw
    if kind == "row":
        boxes = [math_box(child, font, size, cjk_font=cjk_font) for child in node[1]]

        def draw(canvas, x, y):
            for box in boxes:
                box[3](canvas, x, y)
                x += box[0]
        return sum(b[0] for b in boxes), max((b[1] for b in boxes), default=size), max((b[2] for b in boxes), default=0), draw
    if kind == "frac":
        numerator, denominator = math_box(node[1], font, size * .85, cjk_font=cjk_font), math_box(node[2], font, size * .85, cjk_font=cjk_font)
        width = max(numerator[0], denominator[0]) + 8
        up = numerator[1] + numerator[2] + 4
        down = denominator[1] + denominator[2] + 4

        def draw(canvas, x, y):
            numerator[3](canvas, x + (width - numerator[0]) / 2, y + numerator[2] + 3)
            denominator[3](canvas, x + (width - denominator[0]) / 2, y - denominator[1] - 3)
            canvas.setLineWidth(.5)
            canvas.line(x + 1, y, x + width - 1, y)
        return width, up, down, draw
    if kind == "sqrt":
        child = math_box(node[1], font, size, cjk_font=cjk_font)
        width = child[0] + size

        def draw(canvas, x, y):
            canvas.setFont(font, size)
            canvas.drawString(x, y, "√")
            child[3](canvas, x + size * .8, y)
            canvas.setLineWidth(.5)
            canvas.line(x + size * .65, y + child[1], x + width, y + child[1])
        return width, child[1] + 2, child[2], draw
    if kind == "scripts":
        base = math_box(node[1], font, size, cjk_font=cjk_font)
        sub = math_box(node[2], font, size * .65, cjk_font=cjk_font) if node[2] is not None else None
        sup = math_box(node[3], font, size * .65, cjk_font=cjk_font) if node[3] is not None else None

        def draw(canvas, x, y):
            base[3](canvas, x, y)
            if sub:
                sub[3](canvas, x + base[0], y - size * .4)
            if sup:
                sup[3](canvas, x + base[0], y + size * .6)
        return base[0] + max(sub[0] if sub else 0, sup[0] if sup else 0), max(base[1], size * 1.2 if sup else 0), max(base[2], size * .6 if sub else 0), draw
    if kind == "over":
        base = math_box(node[1], font, size, cjk_font=cjk_font)
        label = math_box(node[2], font, size * .65, cjk_font=cjk_font)
        width = max(base[0], label[0]) + 4

        def draw(canvas, x, y):
            base[3](canvas, x + (width - base[0]) / 2, y)
            label[3](canvas, x + (width - label[0]) / 2, y + size * .8)
        return width, size * 1.6, base[2], draw
    raise FormulaUnsupported("unsupported AST")
