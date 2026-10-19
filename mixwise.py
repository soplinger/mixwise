#!/usr/bin/env python3
"""mixwise: offline mortar and concrete batch planner driven by one plain-text job file.

Quantities use exact fractions. Only the final purchase quantities are rounded,
and money is rounded half-up to whole cents.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

__version__ = "1.0.0"

NUMBER = r"\d+(?:\.\d+)?"
FRACTION = r"\d+(?:\.\d+)?(?:/\d+)?"

CEMENT_DENSITY = Fraction(1440)  # kg per m3, loose Portland cement
LIME_DENSITY = Fraction(500)  # kg per m3, loose hydrated lime
DENSITY = {"cement": CEMENT_DENSITY, "lime": LIME_DENSITY}
# Dry volume produced per unit of wet volume (bulking allowance for each mix kind).
DRY_FACTOR = {"mortar": Fraction(13, 10), "concrete": Fraction(77, 50)}
MATERIALS = {
    "mortar": ("cement", "lime", "sand"),
    "concrete": ("cement", "sand", "gravel"),
}
PRICE_KEY = {
    "cement": "cement_per_bag",
    "lime": "lime_per_bag",
    "sand": "sand_per_m3",
    "gravel": "gravel_per_m3",
}
# ASTM C270 cement-lime-sand proportions, using the midpoint of each range.
MORTAR_TYPES = {"M": "1:1/4:3", "S": "1:1/2:4.5", "N": "1:1:6", "O": "1:2:9"}
ORDER_STEP = Fraction(1, 100)  # sand and gravel are sold in 0.01 m3 steps
MAX_WATER_CEMENT = Fraction(3, 5)
MIN_CONCRETE_CEMENT = Fraction(300)  # kg per m3
DEFAULT_WASTE = Fraction(5, 100)
DEFAULT_SETTINGS = {"cement_bag_kg": Fraction(25), "lime_bag_kg": Fraction(25), "currency": "$"}

SECTION_RE = re.compile(r"\[\s*(settings|mix)(?:\s+([A-Za-z0-9_-]+))?\s*\]")
ENTRY_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*(.*)")


# --- value parsing -----------------------------------------------------------

def parse_number(text: str) -> Fraction:
    if not re.fullmatch(NUMBER, text.strip()):
        raise ValueError(f"expected a plain number, got {text!r}")
    return Fraction(text.strip())


def parse_positive(text: str) -> Fraction:
    value = parse_number(text)
    if value == 0:
        raise ValueError("must be greater than zero")
    return value


def parse_fraction(text: str) -> Fraction:
    if not re.fullmatch(FRACTION, text.strip()):
        raise ValueError(f"expected a number or fraction like 1/4, got {text!r}")
    return Fraction(text.strip())


def parse_ratio(text: str) -> tuple:
    return tuple(parse_fraction(piece) for piece in text.split(":"))


def parse_volume(text: str) -> Fraction:
    """Accepts 0.8, '0.8 m3' or '800 L' and returns cubic metres."""
    match = re.fullmatch(rf"({NUMBER})\s*(m3|l)?", text.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError(f"expected a volume like 0.8, 0.8 m3 or 800 L, got {text!r}")
    amount = Fraction(match.group(1))
    if (match.group(2) or "m3").lower() == "l":
        return amount / 1000
    return amount


def parse_percent(text: str) -> Fraction:
    match = re.fullmatch(rf"({NUMBER})\s*%?", text.strip())
    if match is None:
        raise ValueError(f"expected a percentage like 5 or 5%, got {text!r}")
    return Fraction(match.group(1)) / 100


def parse_currency(text: str) -> str:
    if not text:
        raise ValueError("currency cannot be empty")
    return text


SETTING_CONVERTERS = {
    "cement_bag_kg": parse_positive,
    "lime_bag_kg": parse_positive,
    "currency": parse_currency,
}
MIX_CONVERTERS = {
    "volume": parse_volume,
    "batch": parse_volume,
    "waste": parse_percent,
    "water_cement": parse_number,
    "sand_per_m3": parse_number,
    "gravel_per_m3": parse_number,
    "cement_per_bag": parse_number,
    "lime_per_bag": parse_number,
}
MIX_KEYS = set(MIX_CONVERTERS) | {"kind", "ratio", "type"}


# --- job file model and parsing ----------------------------------------------

@dataclass(frozen=True)
class Problem:
    line: int
    level: str
    message: str


@dataclass
class Mix:
    name: str
    line: int
    kind: str
    label: str
    parts: tuple
    volume: Fraction
    waste: Fraction
    water_cement: Fraction | None
    batch: Fraction | None
    prices: dict


@dataclass
class Job:
    settings: dict
    mixes: list


@dataclass
class Result:
    mix: Mix
    waste_volume: Fraction
    dry_volume: Fraction
    cement_kg_per_m3: Fraction
    needs: dict  # material -> (amount, unit) before purchase rounding
    order: dict  # material -> (quantity, unit) as bought
    costs: dict  # material -> whole cents
    drums: int | None
    drum: dict | None  # material -> (amount, unit) for one full mixer drum
    warnings: list

    @property
    def total_cents(self) -> int:
        return sum(self.costs.values())


def parse_job(text: str) -> tuple:
    """Parse a job file. Returns (Job, problems); problems carry line numbers."""
    problems: list = []
    sections: list = []
    current = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        if body.startswith("["):
            match = SECTION_RE.fullmatch(body)
            if match is None:
                problems.append(Problem(lineno, "error", "bad section header; use [settings] or [mix NAME]"))
                current = {"kind": None, "skip": True}
                continue
            current = {
                "kind": match.group(1),
                "name": match.group(2) or "",
                "line": lineno,
                "entries": {},
                "skip": False,
            }
            if current["kind"] == "mix" and not current["name"]:
                problems.append(Problem(lineno, "error", "mix section needs a name, e.g. [mix wall-mortar]"))
                current["skip"] = True
            sections.append(current)
            continue
        match = ENTRY_RE.fullmatch(body)
        if match is None:
            problems.append(Problem(lineno, "error", f"expected 'key = value', got {body!r}"))
            continue
        if current is None:
            problems.append(Problem(lineno, "error", "key outside any [settings] or [mix] section"))
            continue
        if current["skip"]:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key in current["entries"]:
            first = current["entries"][key][1]
            problems.append(Problem(lineno, "error", f"duplicate key '{key}' (first set on line {first})"))
            continue
        current["entries"][key] = (value, lineno)

    settings = dict(DEFAULT_SETTINGS)
    mixes: list = []
    seen: dict = {}
    settings_line = None
    for section in sections:
        if section["skip"]:
            continue
        if section["kind"] == "settings":
            if settings_line is not None:
                problems.append(Problem(section["line"], "error", f"only one [settings] section is allowed (first on line {settings_line})"))
            settings_line = section["line"]
            _read_settings(section, settings, problems)
            continue
        mix = _read_mix(section, problems)
        if mix is None:
            continue
        if mix.name in seen:
            problems.append(Problem(mix.line, "error", f"duplicate mix '{mix.name}' (first defined on line {seen[mix.name]})"))
            continue
        seen[mix.name] = mix.line
        mixes.append(mix)
    return Job(settings, mixes), problems


def _read_settings(section: dict, settings: dict, problems: list) -> None:
    if section["name"]:
        problems.append(Problem(section["line"], "error", "[settings] takes no name"))
    for key, (value, line) in section["entries"].items():
        if key not in SETTING_CONVERTERS:
            problems.append(Problem(line, "error", f"unknown setting '{key}'"))
            continue
        try:
            settings[key] = SETTING_CONVERTERS[key](value)
        except (ValueError, ZeroDivisionError) as exc:
            problems.append(Problem(line, "error", f"{key}: {exc}"))


def _read_mix(section: dict, problems: list):
    name, head = section["name"], section["line"]
    start = len(problems)
    values: dict = {}
    for key, (value, line) in section["entries"].items():
        if key not in MIX_KEYS:
            problems.append(Problem(line, "error", f"unknown key '{key}' in mix '{name}'"))
        elif key in MIX_CONVERTERS:
            try:
                values[key] = (MIX_CONVERTERS[key](value), line)
            except (ValueError, ZeroDivisionError) as exc:
                problems.append(Problem(line, "error", f"{key}: {exc}"))
        else:
            values[key] = (value, line)

    def line_of(key: str) -> int:
        return values[key][1] if key in values else head

    kind = str(values["kind"][0]).lower() if "kind" in values else ""
    if kind not in MATERIALS:
        problems.append(Problem(line_of("kind"), "error", f"mix '{name}' needs kind = mortar or concrete"))
        return None
    materials = MATERIALS[kind]

    label, parts = "", None
    if "ratio" in values and "type" in values:
        problems.append(Problem(line_of("type"), "error", f"mix '{name}' takes ratio or type, not both"))
    elif "type" in values:
        letter = str(values["type"][0]).upper()
        if letter not in MORTAR_TYPES:
            problems.append(Problem(line_of("type"), "error", f"unknown mortar type {letter!r}; use M, S, N or O"))
        elif kind != "mortar":
            problems.append(Problem(line_of("type"), "error", "type applies only to mortar; use ratio for concrete"))
        else:
            parts = parse_ratio(MORTAR_TYPES[letter])
            label = f"type {letter} ({MORTAR_TYPES[letter]})"
    elif "ratio" in values:
        text = str(values["ratio"][0])
        try:
            parts = parse_ratio(text)
            label = f"ratio {text}"
        except (ValueError, ZeroDivisionError) as exc:
            problems.append(Problem(line_of("ratio"), "error", f"ratio: {exc}"))
    else:
        problems.append(Problem(head, "error", f"mix '{name}' needs ratio or type"))

    if parts is not None:
        if len(parts) != 3:
            problems.append(Problem(line_of("ratio"), "error", f"ratio needs three parts: {':'.join(materials)}"))
        elif parts[0] == 0:
            problems.append(Problem(line_of("ratio"), "error", f"the {materials[0]} part must be above zero"))
        elif kind == "concrete" and 0 in parts:
            problems.append(Problem(line_of("ratio"), "error", "concrete needs sand and gravel parts above zero"))

    if "volume" not in values:
        problems.append(Problem(head, "error", f"mix '{name}' needs volume = ..."))
    elif values["volume"][0] == 0:
        problems.append(Problem(line_of("volume"), "error", "volume must be above zero"))
    waste = values["waste"][0] if "waste" in values else DEFAULT_WASTE
    if not 0 <= waste < 1:
        problems.append(Problem(line_of("waste"), "error", "waste must be at least 0% and below 100%"))

    water_cement = values["water_cement"][0] if "water_cement" in values else None
    if kind == "concrete" and water_cement is None:
        problems.append(Problem(head, "error", f"concrete mix '{name}' needs water_cement = ..."))
    elif kind == "mortar" and water_cement is not None:
        problems.append(Problem(line_of("water_cement"), "error", "water_cement applies only to concrete"))
    elif water_cement == 0:
        problems.append(Problem(line_of("water_cement"), "error", "water_cement must be above zero"))

    batch = values["batch"][0] if "batch" in values else None
    if batch == 0:
        problems.append(Problem(line_of("batch"), "error", "batch must be above zero"))

    allowed = {PRICE_KEY[m] for m in materials}
    for key in PRICE_KEY.values():
        if key in values and key not in allowed:
            problems.append(Problem(line_of(key), "error", f"{key} does not apply to {kind} mixes"))

    if len(problems) > start:
        return None
    prices = {key: values[key][0] for key in PRICE_KEY.values() if key in values}
    return Mix(name, head, kind, label, parts, values["volume"][0], waste, water_cement, batch, prices)


# --- calculations ------------------------------------------------------------

def round_up(value: Fraction, step: Fraction) -> Fraction:
    return math.ceil(value / step) * step


def to_cents(amount: Fraction) -> int:
    return math.floor(amount * 100 + Fraction(1, 2))


def compute_mix(mix: Mix, settings: dict) -> Result:
    names = MATERIALS[mix.kind]
    total = sum(mix.parts)
    waste_volume = mix.volume * (1 + mix.waste)
    dry = waste_volume * DRY_FACTOR[mix.kind]
    solid = {name: dry * part / total for name, part in zip(names, mix.parts)}
    cement_kg = solid["cement"] * CEMENT_DENSITY

    needs: dict = {}
    order: dict = {}
    for name in names:
        if solid[name] == 0:
            continue
        if name in DENSITY:
            kg = solid[name] * DENSITY[name]
            bag_kg = settings["cement_bag_kg"] if name == "cement" else settings["lime_bag_kg"]
            needs[name] = (kg, "kg")
            order[name] = (math.ceil(kg / bag_kg), "bag")
        else:
            needs[name] = (solid[name], "m3")
            order[name] = (round_up(solid[name], ORDER_STEP), "m3")
    if mix.kind == "concrete":
        needs["water"] = (mix.water_cement * cement_kg, "L")

    warnings = []
    costs = {}
    for name, (quantity, _unit) in order.items():
        price = mix.prices.get(PRICE_KEY[name])
        if price is None:
            warnings.append(f"no {PRICE_KEY[name]} given; {name} is costed at 0")
            price = 0
        costs[name] = to_cents(quantity * price)

    cement_kg_per_m3 = cement_kg / waste_volume
    if mix.kind == "concrete":
        if mix.water_cement > MAX_WATER_CEMENT:
            warnings.append(f"water_cement {float(mix.water_cement):.2f} is above 0.60; durability may suffer")
        if cement_kg_per_m3 < MIN_CONCRETE_CEMENT:
            warnings.append(f"cement content {float(cement_kg_per_m3):.0f} kg/m3 is below 300 kg/m3")

    drums, drum = None, None
    if mix.batch is not None:
        drums = math.ceil(waste_volume / mix.batch)
        scale = mix.batch / waste_volume
        drum = {name: (amount * scale, unit) for name, (amount, unit) in needs.items()}

    return Result(
        mix=mix,
        waste_volume=waste_volume,
        dry_volume=dry,
        cement_kg_per_m3=cement_kg_per_m3,
        needs=needs,
        order=order,
        costs=costs,
        drums=drums,
        drum=drum,
        warnings=warnings,
    )


def yield_for_bags(mix: Mix, settings: dict, bags: int) -> tuple:
    """Return (wet volume the cement can make, usable pour after waste) in m3."""
    cement_m3 = bags * settings["cement_bag_kg"] / CEMENT_DENSITY
    dry = cement_m3 * sum(mix.parts) / mix.parts[0]
    wet = dry / DRY_FACTOR[mix.kind]
    return wet, wet / (1 + mix.waste)


def bag_recipe(kind: str, parts: tuple, bag_kg: Fraction, water_cement=None) -> dict:
    """Loose litres of each material per bag of cement, plus the wet yield in litres."""
    names = MATERIALS[kind]
    total = sum(parts)
    cement_l = bag_kg * 1000 / CEMENT_DENSITY
    dry_l = cement_l * total / parts[0]
    litres = {name: dry_l * part / total for name, part in zip(names, parts) if part > 0}
    recipe = {"litres": litres, "wet_litres": dry_l / DRY_FACTOR[kind]}
    if kind == "concrete":
        recipe["litres"]["water"] = water_cement * bag_kg
    return recipe


def job_warnings(results: list) -> list:
    return [Problem(r.mix.line, "warning", f"mix '{r.mix.name}': {w}") for r in results for w in r.warnings]


def aggregate(results: list) -> dict:
    totals: dict = {}
    for result in results:
        for name, (quantity, unit) in result.order.items():
            entry = totals.setdefault(name, {"quantity": 0, "unit": unit, "cost_cents": 0})
            entry["quantity"] += quantity
            entry["cost_cents"] += result.costs[name]
    return totals


# --- formatting --------------------------------------------------------------

def fmt(amount, unit: str) -> str:
    if unit == "m3":
        return f"{float(amount):.3f} m3"
    return f"{float(amount):,.1f} {unit}"


def money(cents: int, symbol: str) -> str:
    return f"{symbol}{cents // 100:,}.{cents % 100:02d}"


def num(value) -> float:
    return round(float(value), 3)


def describe(result: Result, symbol: str) -> str:
    mix = result.mix
    lines = [
        f"{mix.name}  ({mix.kind}, {mix.label})",
        f"  wet volume  {fmt(mix.volume, 'm3')} + {float(mix.waste * 100):g}% waste = {fmt(result.waste_volume, 'm3')} ordered",
        f"  dry volume  {fmt(result.dry_volume, 'm3')} (bulking x{float(DRY_FACTOR[mix.kind]):.2f})",
    ]
    for name, (amount, unit) in result.needs.items():
        line = f"  {name:<8}{fmt(amount, unit):>16}"
        if name in result.order:
            quantity, bought = result.order[name]
            buy = f"{quantity} bags" if bought == "bag" else fmt(quantity, bought)
            line += f"  buy {buy:<14}{money(result.costs[name], symbol):>10}"
        lines.append(line)
    if result.drums is not None:
        lines.append(f"  drums       {result.drums} x {fmt(mix.batch, 'm3')}; per full drum:")
        for name, (amount, unit) in result.drum.items():
            lines.append(f"    {name:<8}{fmt(amount, unit):>16}")
    return "\n".join(lines)


def result_dict(result: Result) -> dict:
    data = {
        "name": result.mix.name,
        "kind": result.mix.kind,
        "label": result.mix.label,
        "wet_volume_m3": num(result.mix.volume),
        "ordered_wet_volume_m3": num(result.waste_volume),
        "dry_volume_m3": num(result.dry_volume),
        "cement_kg_per_m3": num(result.cement_kg_per_m3),
        "needs": {name: {"amount": num(a), "unit": u} for name, (a, u) in result.needs.items()},
        "order": {name: {"quantity": num(q), "unit": u} for name, (q, u) in result.order.items()},
        "costs_cents": dict(result.costs),
        "total_cents": result.total_cents,
        "warnings": list(result.warnings),
    }
    if result.drums is not None:
        data["drums"] = result.drums
        data["per_drum"] = {name: {"amount": num(a), "unit": u} for name, (a, u) in result.drum.items()}
    return data


def write_csv(path: str, results: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["mix", "material", "quantity", "unit", "cost"])
        for result in results:
            for name, (quantity, unit) in result.order.items():
                writer.writerow([result.mix.name, name, num(quantity), unit, f"{result.costs[name] / 100:.2f}"])


# --- command line ------------------------------------------------------------

def _load(path: str):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"mixwise: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return None, None
    return parse_job(text)


def _report(path: str, problems: list, stream) -> None:
    for problem in problems:
        print(f"{path}:{problem.line}: {problem.level}: {problem.message}", file=stream)


def _has_errors(problems: list) -> bool:
    return any(p.level == "error" for p in problems)


def cmd_plan(args) -> int:
    job, problems = _load(args.file)
    if job is None:
        return 2
    _report(args.file, problems, sys.stderr)
    if _has_errors(problems):
        return 1
    if not job.mixes:
        print(f"mixwise: {args.file} has no [mix NAME] sections", file=sys.stderr)
        return 1
    mixes = job.mixes
    if args.mix:
        known = {m.name for m in job.mixes}
        unknown = [name for name in args.mix if name not in known]
        if unknown:
            print(f"mixwise: no mix named {', '.join(unknown)}", file=sys.stderr)
            return 1
        mixes = [m for m in job.mixes if m.name in args.mix]
    results = [compute_mix(m, job.settings) for m in mixes]
    _report(args.file, job_warnings(results), sys.stderr)

    symbol = job.settings["currency"]
    totals = aggregate(results)
    grand = sum(r.total_cents for r in results)
    if args.csv:
        write_csv(args.csv, results)
    if args.json:
        payload = {
            "currency": symbol,
            "mixes": [result_dict(r) for r in results],
            "totals": {
                name: {"quantity": num(t["quantity"]), "unit": t["unit"], "cost_cents": t["cost_cents"]}
                for name, t in totals.items()
            },
            "total_cents": grand,
        }
        print(json.dumps(payload, indent=2))
        return 0

    for result in results:
        print(describe(result, symbol))
        print()
    print("Shopping list")
    for name, total in totals.items():
        quantity = total["quantity"]
        shown = f"{quantity} bags" if total["unit"] == "bag" else fmt(quantity, total["unit"])
        print(f"  {name:<8}{shown:>16}  {money(total['cost_cents'], symbol):>12}")
    print(f"  {'total':<8}{'':>16}  {money(grand, symbol):>12}")
    return 0


def cmd_check(args) -> int:
    job, problems = _load(args.file)
    if job is None:
        return 2
    results = [compute_mix(m, job.settings) for m in job.mixes] if job.mixes else []
    problems = problems + job_warnings(results)
    _report(args.file, problems, sys.stdout)
    errors = sum(p.level == "error" for p in problems)
    warnings = len(problems) - errors
    if errors == 0 and not (args.strict and warnings):
        print(f"ok: {len(job.mixes)} mix(es), {warnings} warning(s)")
        return 0
    print(f"{errors} error(s), {warnings} warning(s)")
    return 1


def cmd_yield(args) -> int:
    job, problems = _load(args.file)
    if job is None:
        return 2
    _report(args.file, problems, sys.stderr)
    if _has_errors(problems):
        return 1
    match = [m for m in job.mixes if m.name == args.name]
    if not match:
        print(f"mixwise: no mix named {args.name!r}", file=sys.stderr)
        return 1
    mix = match[0]
    if args.bags < 1:
        print("mixwise: --bags must be at least 1", file=sys.stderr)
        return 1
    wet, usable = yield_for_bags(mix, job.settings, args.bags)
    print(f"{mix.name}: {args.bags} cement bag(s) of {float(job.settings['cement_bag_kg']):g} kg")
    print(f"  makes up to {fmt(wet, 'm3')} of mix")
    print(f"  usable pour after {float(mix.waste * 100):g}% waste: {fmt(usable, 'm3')}")
    return 0


def cmd_bag(args) -> int:
    spec = args.spec.strip()
    if spec.upper() in MORTAR_TYPES:
        kind, parts, label = "mortar", parse_ratio(MORTAR_TYPES[spec.upper()]), f"type {spec.upper()}"
    else:
        try:
            parts = parse_ratio(spec)
        except (ValueError, ZeroDivisionError) as exc:
            print(f"mixwise: bad spec {spec!r}: {exc}", file=sys.stderr)
            return 1
        kind, label = args.kind, f"ratio {spec}"
    if len(parts) != 3 or parts[0] == 0:
        print("mixwise: spec needs three parts with a cement part above zero", file=sys.stderr)
        return 1
    if kind == "concrete" and args.water_cement is None:
        print("mixwise: concrete needs --water-cement", file=sys.stderr)
        return 1
    water = parse_number(str(args.water_cement)) if args.water_cement is not None else None
    bag_kg = parse_positive(str(args.bag_kg))
    recipe = bag_recipe(kind, parts, bag_kg, water)
    print(f"Per {float(bag_kg):g} kg bag of cement ({kind}, {label}):")
    for name, litres in recipe["litres"].items():
        print(f"  {name:<8}{float(litres):>10,.1f} L loose")
    print(f"  wet yield {float(recipe['wet_litres']):,.1f} L")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mixwise", description="Offline mortar and concrete batch planner.")
    parser.add_argument("--version", action="version", version=f"mixwise {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="plan the batches and shopping list from a job file")
    plan.add_argument("file")
    plan.add_argument("--mix", action="append", metavar="NAME", help="only plan this mix (repeatable)")
    plan.add_argument("--json", action="store_true", help="print the plan as JSON")
    plan.add_argument("--csv", metavar="PATH", help="write the shopping list to a CSV file")
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="lint a job file; exit 1 on errors")
    check.add_argument("file")
    check.add_argument("--strict", action="store_true", help="also exit 1 on warnings")
    check.set_defaults(func=cmd_check)

    yld = sub.add_parser("yield", help="how much mix a number of cement bags makes")
    yld.add_argument("file")
    yld.add_argument("name", help="mix name")
    yld.add_argument("--bags", type=int, required=True, help="cement bags on hand")
    yld.set_defaults(func=cmd_yield)

    bag = sub.add_parser("bag", help="loose litres of each material per bag of cement")
    bag.add_argument("spec", help="mortar type letter (M, S, N, O) or a ratio like 1:2:3")
    bag.add_argument("--kind", choices=["mortar", "concrete"], default="mortar")
    bag.add_argument("--water-cement", metavar="RATIO", help="water-cement ratio, concrete only")
    bag.add_argument("--bag-kg", default="25", help="cement bag size in kg (default 25)")
    bag.set_defaults(func=cmd_bag)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
