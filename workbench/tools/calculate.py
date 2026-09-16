"""``calculate``: sympy expression with pint quantities, units shown at every step."""

from __future__ import annotations

import math
import re
from typing import Any

import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations

from workbench.core.errors import ToolError
from workbench.core.normalise import canonical_unit, ureg
from workbench.tools.registry import ToolContext, ToolResult, ToolSpec, obj

SCHEMA = obj({
    "expression": {"type": "string", "minLength": 1, "maxLength": 300},
    "variables": {"type": "object", "minProperties": 1, "additionalProperties": {
        "type": "object", "additionalProperties": True, "required": ["value", "unit"],
        "properties": {"value": {"type": "number"}, "unit": {"type": "string"},
                       "record": {"type": "string"}, "description": {"type": "string"}}}},
    "result_name": {"type": "string"},
    "result_unit": {"type": "string"},
    "title": {"type": "string"},
}, ["expression", "variables", "result_unit"])

SAFE_RE = re.compile(r"^[A-Za-z0-9_+\-*/(). ^]+$")
FUNCTIONS = {"sqrt": sympy.sqrt, "exp": sympy.exp, "log": sympy.log, "sin": sympy.sin, "cos": sympy.cos,
             "tan": sympy.tan, "pi": sympy.pi}
EXCEL_FUNCS = {"sqrt": "SQRT", "exp": "EXP", "log": "LN", "sin": "SIN", "cos": "COS", "tan": "TAN"}


def _unit(u: str) -> str:
    u = u.strip()
    if u in {"", "-", "1", "dimensionless"}:
        return "dimensionless"
    try:
        return canonical_unit(u) or u
    except ValueError:
        return u


def split_expression(expression: str, default_name: str) -> tuple[str, str]:
    name, sep, rhs = expression.partition("=")
    if sep:
        return name.strip(), rhs.strip()
    return default_name, expression.strip()


def parse_safe(rhs: str, names: list[str]) -> sympy.Expr:
    if not SAFE_RE.match(rhs):
        raise ToolError("expression contains characters outside the arithmetic whitelist")
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rhs):
        if ident not in names and ident not in FUNCTIONS:
            raise ToolError(f"unknown symbol {ident!r} in expression")
    local = {n: sympy.Symbol(n) for n in names} | FUNCTIONS
    expr = parse_expr(rhs.replace("^", "**"), local_dict=local, global_dict={"__builtins__": {}, **FUNCTIONS,
                      "Integer": sympy.Integer, "Float": sympy.Float, "Symbol": sympy.Symbol,
                      "Rational": sympy.Rational, "Add": sympy.Add, "Mul": sympy.Mul, "Pow": sympy.Pow},
                      transformations=standard_transformations, evaluate=False)
    return expr


def fmt_q(q: Any) -> str:
    mag = float(q.magnitude)
    unit = f"{q.units:~P}".strip()
    return f"{mag:.4g} {unit}".strip()


def evaluate(expr: sympy.Expr, quantities: dict[str, Any]) -> Any:
    fn = sympy.lambdify([sympy.Symbol(n) for n in quantities], expr, modules=[{"sqrt": lambda x: x ** 0.5}, "math"])
    return fn(*quantities.values())


def to_excel(rhs: str, refs: dict[str, str]) -> str:
    out = rhs.replace("**", "^")
    for name in sorted(refs, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(name)}\b", refs[name], out)
    for fn, xl in EXCEL_FUNCS.items():
        out = re.sub(rf"\b{fn}\(", f"{xl}(", out)
    return out.replace(" ", "")


def calculate(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    reg = ureg()
    variables: dict[str, dict[str, Any]] = dict(args["variables"])
    name, rhs = split_expression(str(args["expression"]), str(args.get("result_name") or "result"))
    expr = parse_safe(rhs, list(variables))
    quantities = {k: reg.Quantity(float(v["value"]), _unit(str(v["unit"]))) for k, v in variables.items()}
    result_unit = _unit(str(args["result_unit"]))
    try:
        result = evaluate(expr, quantities).to(result_unit)
    except Exception as exc:
        raise ToolError(f"unit error: {exc}") from exc
    steps = [{"step": 1, "label": "Formula", "formula": f"{name} = {rhs}", "substitution": "", "result": ""}]
    subst = rhs
    for k in sorted(variables, key=len, reverse=True):
        subst = re.sub(rf"\b{re.escape(k)}\b", f"({fmt_q(quantities[k])})", subst)
    steps.append({"step": 2, "label": "Substitution", "formula": "", "substitution": f"{name} = {subst}",
                  "result": ""})
    terms = expr.args if isinstance(expr, sympy.Add) else ()
    for i, term in enumerate(terms):
        used = {str(s) for s in term.free_symbols}
        sub_q = {k: quantities[k] for k in variables if k in used}
        try:
            val = evaluate(term, sub_q) if sub_q else reg.Quantity(float(term), "dimensionless")
            val = val.to(result_unit) if val.dimensionality == result.dimensionality else val.to_base_units()
        except Exception:
            continue
        steps.append({"step": len(steps) + 1, "label": f"Term {i + 1}", "formula": sympy.sstr(term),
                      "substitution": "", "result": fmt_q(val)})
    steps.append({"step": len(steps) + 1, "label": "Result", "formula": "", "substitution": "",
                  "result": f"{name} = {fmt_q(result)}"})
    raw = evaluate(expr, {k: float(v["value"]) for k, v in variables.items()})
    consistent = math.isclose(float(raw), float(result.magnitude), rel_tol=1e-9)
    records = [str(v["record"]) for v in variables.values() if v.get("record")]
    body = {
        "title": args.get("title") or f"Calculation of {name}",
        "expression": f"{name} = {rhs}", "result_name": name, "rhs": rhs,
        "variables": {k: {**v, "unit": str(v["unit"])} for k, v in variables.items()},
        "steps": steps,
        "result": {"value": round(float(result.magnitude), 6), "unit": str(args["result_unit"]),
                   "display": fmt_q(result)},
        "excel": {"raw_units_consistent": consistent,
                  "note": "inputs are used as given" if consistent else "inputs converted to SI base units"},
        "method": "sympy + pint",
    }
    rec = ctx.rt.ledger.add(ctx.task_id, "calc_result", summary=f"{body['title']}: {name} = {fmt_q(result)}",
                            body=body, label=ctx.label(), produced_by=ctx.call_id, inputs=records,
                            confidence="high")
    return ToolResult(ok=True, summary=f"{name} = {fmt_q(result)} ({len(steps)} steps)", records=[rec.id],
                      body={"records": [rec.id], **body})


SPECS = [ToolSpec(name="calculate", description="Evaluate an engineering formula with units at every step.",
                  input_schema=SCHEMA, handler=calculate, output_type="calc_result", budget_key="calculate")]
