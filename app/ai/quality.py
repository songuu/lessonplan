"""Deterministic, bounded checks. Rendering warnings do not rewrite content."""
from __future__ import annotations

import re
from uuid import uuid4

from .assets import AssetBundle


def finding(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "severity": severity, "message": message}


def _strip_blocks(content: str, targets: set[str]) -> str:
    """Walk tag boundaries once; unmatched control blocks remain suppressed through EOF."""
    output, stack, cursor = [], [], 0
    for token in re.finditer(r"</?([A-Za-z][\w:-]*)(?:\s[^<>]*|/?)>", content):
        name = token.group(1).lower()
        if name not in targets:
            continue
        closing = token.group(0).startswith("</")
        if not closing:
            if not stack:
                output.append(content[cursor:token.start()])
            if not token.group(0).endswith("/>"):
                stack.append(name)
            elif not stack:
                cursor = token.end()
        elif stack:
            if name in stack:
                while stack.pop() != name:
                    pass
                if not stack:
                    cursor = token.end()
        else:
            output.append(content[cursor:token.start()])
            cursor = token.end()
    if not stack:
        output.append(content[cursor:])
    return "".join(output)


def _strip_control_comments(content: str) -> str:
    output, cursor = [], 0
    while (start := content.find("<!--", cursor)) >= 0:
        output.append(content[cursor:start])
        end = content.find("-->", start + 4)
        body = content[start + 4:end if end >= 0 else len(content)].strip()
        controlled = bool(re.match(r"(?:MODULE:|END_MODULE\b|/?(?:internal|analysis)\b)", body, re.I))
        if end < 0:
            if not controlled:
                output.append(content[start:])
            return "".join(output)
        if not controlled:
            output.append(content[start:end + 3])
        cursor = end + 3
    output.append(content[cursor:])
    return "".join(output)


def _mask_math(content: str):
    output, protected, cursor = [], [], 0
    nonce = uuid4().hex
    starts = re.compile(r"\\[\[(]")
    while (match := starts.search(content, cursor)) is not None:
        start = match.start()
        end = content.find(r"\)" if content[start + 1] == "(" else r"\]", start + 2)
        if end < 0:
            break
        output.extend([content[cursor:start], f"\ue000{nonce}:{len(protected)}\ue001"])
        protected.append(content[start:end + 2])
        cursor = end + 2
    output.append(content[cursor:])
    return "".join(output), protected, nonce


_DANGEROUS = re.compile(
    r"\\(?:input|include|includegraphics|write\d*|openout|read|catcode|csname|def|gdef|edef|xdef|let|futurelet|"
    r"newcommand|renewcommand|providecommand|usepackage|documentclass|html(?:Class|Id|Style|Data)|url|href|"
    r"require|special|pdfobj|pdfliteral|immediate|loop|repeat|verbatiminput)\b", re.I)
_KNOWN_MATH = set("frac dfrac tfrac sqrt text textrm textbf textit mathrm mathbf mathit mathsf mathtt mathcal mathbb mathnormal "
                  "left right middle big Big bigg Bigg begin end overline underline vec hat bar dot ddot dots cdots ldots vdots ddots "
                  "sum prod int iint iiint oint lim log ln sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh "
                  "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi varpi rho "
                  "sigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega "
                  "infty partial nabla pm mp times div cdot ast circ bullet le leq ge geq ne neq approx equiv sim simeq "
                  "propto in notin subset subseteq supset supseteq cup cap emptyset varnothing forall exists neg land lor "
                  "to rightarrow leftarrow leftrightarrow rightleftharpoons Rightarrow Leftarrow Leftrightarrow mapsto xrightarrow xleftarrow "
                  "uparrow downarrow degree angle triangle perp parallel bot top therefore because quad qquad hspace "
                  "underbrace overbrace overset underset binom dbinom tbinom operatorname color phantom vphantom hphantom "
                  "boldsymbol acute grave breve check tilde widehat widetilde det max min gcd lcm exp mod bmod pmod "
                  "Re Im aleph ell hbar prime vert Vert lvert rvert lVert rVert langle rangle lt gt not limits nolimits "
                  "displaystyle textstyle scriptstyle scriptscriptstyle substack smallmatrix cases aligned matrix pmatrix bmatrix "
                  "Bmatrix vmatrix Vmatrix array gather split mathop mathrel mathord mathbin mathpunct mathopen mathclose".split())


def normalize_legacy_math(content: str) -> str:
    def chemical(match):
        formula = match.group(1)
        # Convert only a flat chemical expression; preserve complex historical syntax for diagnosis.
        if not re.fullmatch(r"[A-Za-z0-9+\-=> .()]+", formula):
            return match.group(0)
        formula = formula.replace("<=>", r"\rightleftharpoons ").replace("->", r"\rightarrow ")
        pieces = re.split(r"(\\[A-Za-z]+\s*|\s*[+=]\s*)", formula)
        output = []
        for piece in pieces:
            if not piece:
                continue
            if "\\" in piece or re.fullmatch(r"\s*[+=]\s*", piece):
                output.append(piece)
            else:
                piece = re.sub(r"(?<=[A-Za-z)])(\d+)", r"_{\1}", piece.strip())
                output.append(r"\mathrm{" + piece + "}")
        return "".join(output)
    content = re.sub(r"\\ce\{([^{}]+)\}", chemical, content)
    content = re.sub(r"\\SI\{([^{}]+)\}\{([^{}]+)\}", lambda m: m.group(1) + r"\,\mathrm{" + m.group(2) + "}", content)
    return content


class SafePatchProcessor:
    def __init__(self, assets: AssetBundle | None = None):
        self.assets = assets or AssetBundle()
        self.policy = self.assets.json("config/safe_patch_policy.json")

    def apply(self, content: str, inputs: dict, *, module: dict | None = None,
              outline: list[dict] | None = None, scope: str = "module") -> tuple[str, list[dict]]:
        patches = []
        allowed = self.policy["allowed"]
        original = content

        def change(code, transformed):
            nonlocal content
            if transformed != content:
                patches.append({"code": code, "kind": "deterministic", "scope": scope})
                content = transformed

        if "internal_markers" in allowed:
            change("internal_markers", _strip_blocks(content, {"internal_style", "style_intent", "analysis", "thinking", "reasoning", "system_prompt"}))
            change("internal_markers", _strip_control_comments(content))
            change("internal_markers", re.sub(r"</?(?:module|module_content|teacher_view_content|canonical_content|lesson_plan)[^<>]*>", "", content, flags=re.I))
        if "unsafe_html" in allowed:
            change("unsafe_html", _strip_blocks(content, {"script", "style", "iframe", "object"}))
            # Compare symbols inside math are content, not XML/HTML markup.
            masked, protected, nonce = _mask_math(content)
            masked = re.sub(r"</?[A-Za-z][^<>]*>", "", masked)
            masked = re.sub("\ue000" + nonce + r":(\d+)\ue001", lambda m: protected[int(m.group(1))], masked)
            change("unsafe_html", masked)
            change("unsafe_html", re.sub(r"\]\(\s*(?:javascript|data|vbscript):[^()[\]\r\n]*\)", "]", content, flags=re.I))
        if "unsupported_page_claims" in allowed:
            material = str(inputs.get("source_material", "") or inputs.get("textbook_content", ""))
            def page(match):
                return match.group(0) if match.group(0) in material else ""
            change("unsupported_page_claims", re.sub(r"(?:教材)?第\s*\d+\s*页(?:[-—至]\s*第?\s*\d+\s*页)?[：:]?", page, content))
        if "legacy_formula" in allowed:
            change("legacy_formula", normalize_legacy_math(content))
        if outline and "process_heading" in allowed and scope != "selection":
            first = outline[0]
            wanted = f"### {first['title']}（{first['duration_min']:g}分钟）" if isinstance(first["duration_min"], (int, float)) else f"### {first['title']}（{first['duration_min']}分钟）"
            lines = content.strip().splitlines()
            while lines and re.match(r"^#{1,3}\s", lines[0]) and first["title"] not in lines[0]:
                lines.pop(0)
            if not lines or not re.match(r"^###\s+", lines[0]) or first["title"] not in lines[0]:
                lines.insert(0, wanted)
            else:
                lines[0] = wanted
            change("process_heading", "\n".join(lines))
        content = content.strip()
        # Do not turn a source consisting solely of removable markers into a fake success.
        if not content and original.strip():
            patches.append({"code": "empty_after_safe_patch", "kind": "diagnostic", "scope": scope})
        return content, patches


class TopicConsistencyGate:
    def check(self, content: str, inputs: dict) -> list[dict]:
        from app.core.catalog import LESSON_TYPES, STAGES, SUBJECTS
        findings = []
        topic = str(inputs.get("topic", "")).strip("《》 ")
        declared = re.findall(r"(?:本课课题|课题|教学主题)\s*[:：]\s*《([^《》]+)》", content)
        if topic and declared and all(topic not in title and title not in topic for title in declared):
            findings.append(finding("topic_mismatch", "正文声明的课题与当前课题不一致。", "error"))
        target_stage = next((s["label"] for s in STAGES if inputs.get("school_stage") in {s["value"], s["label"]}), str(inputs.get("school_stage", "")))
        stages = re.findall(r"(?:本课面向|适用学段|教学对象)\s*[:：]?\s*(小学|初中|高中)", content)
        if target_stage and stages and all(stage not in target_stage for stage in stages):
            findings.append(finding("school_stage_mismatch", "正文声明的学段与当前学段不一致。", "error"))
        for field, label, choices, pattern in [
            ("subject", "学科", SUBJECTS, r"(?:适用学科|本课学科|学科)\s*[:：]\s*([^\n。；;]{1,80})"),
            ("lesson_type", "课型", LESSON_TYPES, r"(?:适用课型|本课课型|课型)\s*[:：]\s*([^\n。；;]{1,80})"),
        ]:
            target = next((c["value"] for c in choices if inputs.get(field) in {c["value"], c["label"]}), None)
            declarations = re.findall(pattern, content)
            for declaration in declarations:
                declared_values = {c["value"] for c in choices if any(part in declaration for part in c["label"].split("/"))}
                if target and declared_values and target not in declared_values:
                    findings.append(finding(f"{field}_mismatch", f"正文明确声明的{label}与当前输入不一致。", "error"))
        return findings


class ModuleQualityGate:
    def check(self, content: str, inputs: dict, module: dict, *, scope: str = "module") -> dict:
        findings = TopicConsistencyGate().check(content, inputs)
        if not content.strip():
            findings.append(finding("empty_module", "模块正文为空。", "error"))
        if _DANGEROUS.search(content) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]", content):
            findings.append(finding("unsafe_formula", "正文含危险公式命令或不可显示控制字符。", "error"))
        if re.search(r"<(?:script|iframe|object)\b|javascript\s*:", content, re.I):
            findings.append(finding("unsafe_html", "正文含不安全标记。", "error"))
        if re.search(r"(?:直接|用嘴|亲自)?(?:品尝|尝一口|舔舐)(?:[^。\n]{0,12})(?:试剂|化学品|溶液|盐酸|氢氧化钠)", content):
            findings.append(finding("unsafe_experiment", "实验描述含危险的试剂品尝操作。", "error"))
        if re.search(r"(?:忽略|绕过)(?:所有|以上|之前).{0,8}(?:指令|规则)|SYSTEM_PROMPT|api[_ -]?key\s*[:=]", content, re.I):
            findings.append(finding("internal_control_leak", "正文包含内部控制信息。", "error"))
        render_invalid = False
        commands = set(re.findall(r"\\([A-Za-z]+)", content))
        unknown = commands - _KNOWN_MATH
        if unknown - {"ce", "SI"}:
            render_invalid = True
        for start, end in ((r"\(", r"\)"), (r"\[", r"\]")):
            if content.count(start) != content.count(end):
                render_invalid = True
        if content.count("{") != content.count("}"):
            render_invalid = True
        environments = re.findall(r"\\begin\{([^{}]+)\}", content)
        endings = re.findall(r"\\end\{([^{}]+)\}", content)
        if sorted(environments) != sorted(endings):
            render_invalid = True
        known_environments = {"aligned", "gathered", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "array", "cases", "smallmatrix", "split"}
        if set(environments) - known_environments:
            render_invalid = True
        if render_invalid:
            findings.append(finding("math_render_warning", "公式存在渲染兼容问题；保留源码供教师编辑。"))
        if module.get("module_type") == "textbook_analysis" and not inputs.get("source_material") and not inputs.get("textbook_content"):
            findings.append(finding("textbook_source_unverified", "未提供教材原文，具体编排需教师核对。", "info"))
        passed = not any(f["severity"] == "error" for f in findings)
        return {"passed": passed, "hard_gate_status": "passed" if passed else "failed",
                "math_render_state": "invalid" if render_invalid else "valid", "findings": findings}
