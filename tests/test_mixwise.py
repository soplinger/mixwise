import json
from fractions import Fraction
from pathlib import Path

import pytest

import mixwise
from mixwise import (
    bag_recipe,
    compute_mix,
    main,
    parse_job,
    parse_percent,
    parse_ratio,
    parse_volume,
    round_up,
    to_cents,
    yield_for_bags,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "garden-wall.mix"


def _mix(text):
    job, problems = parse_job(text)
    assert problems == []
    assert len(job.mixes) == 1
    return job.mixes[0], job.settings


def test_example_job_parses_cleanly():
    job, problems = parse_job(EXAMPLE.read_text(encoding="utf-8"))
    assert problems == []
    assert [m.name for m in job.mixes] == ["wall-mortar", "footing", "cap-mortar"]
    assert job.settings["cement_bag_kg"] == 25


def test_volume_units_are_parsed_to_cubic_metres():
    assert parse_volume("0.25") == Fraction(1, 4)
    assert parse_volume("0.25 m3") == Fraction(1, 4)
    assert parse_volume("800 L") == Fraction(4, 5)
    with pytest.raises(ValueError):
        parse_volume("two cubes")


def test_percent_and_ratio_parsing():
    assert parse_percent("5%") == Fraction(1, 20)
    assert parse_ratio("1:1/4:3") == (Fraction(1), Fraction(1, 4), Fraction(3))
    with pytest.raises(ZeroDivisionError):
        parse_ratio("1:1/0:3")


def test_type_letter_and_matching_ratio_give_the_same_parts():
    by_type, _ = _mix("[mix a]\nkind = mortar\ntype = S\nvolume = 1\n")
    by_ratio, _ = _mix("[mix a]\nkind = mortar\nratio = 1:1/2:4.5\nvolume = 1\n")
    assert by_type.parts == by_ratio.parts


def test_problems_carry_the_line_number_of_the_bad_key():
    text = (
        "[settings]\n"
        "currency = $\n"
        "\n"
        "[mix a]\n"
        "kind = mortar\n"
        "type = S\n"
        "volume = 1\n"
        "flavour = mild\n"
    )
    job, problems = parse_job(text)
    assert job.mixes == []
    assert any(p.line == 8 and "unknown key 'flavour'" in p.message for p in problems)


def test_missing_volume_and_key_outside_section_are_reported():
    _, problems = parse_job("[mix a]\nkind = mortar\ntype = N\n")
    assert any(p.line == 1 and "needs volume" in p.message for p in problems)
    _, problems = parse_job("volume = 1\n")
    assert any(p.line == 1 and "outside any" in p.message for p in problems)


def test_duplicate_key_and_duplicate_mix_name_are_errors():
    _, problems = parse_job("[mix a]\nkind = mortar\nkind = concrete\n")
    assert any(p.line == 3 and "duplicate key 'kind'" in p.message for p in problems)
    text = "[mix a]\nkind = mortar\ntype = S\nvolume = 1\n\n[mix a]\nkind = mortar\ntype = N\nvolume = 2\n"
    job, problems = parse_job(text)
    assert [m.name for m in job.mixes] == ["a"]
    assert any(p.line == 6 and "duplicate mix 'a'" in p.message for p in problems)


def test_concrete_requires_water_cement_and_mortar_rejects_it():
    _, problems = parse_job("[mix a]\nkind = concrete\nratio = 1:2:3\nvolume = 1\n")
    assert any("needs water_cement" in p.message for p in problems)
    _, problems = parse_job("[mix a]\nkind = mortar\ntype = S\nvolume = 1\nwater_cement = 0.5\n")
    assert any("applies only to concrete" in p.message for p in problems)


def test_mortar_order_is_exact_and_rounds_up_to_whole_bags():
    mix, settings = _mix("[mix a]\nkind = mortar\nratio = 1:0:3\nvolume = 1\nwaste = 0\n")
    result = compute_mix(mix, settings)
    # dry = 1.3 m3, cement = 0.325 m3 = 468 kg = 18.72 bags -> 19
    assert result.needs["cement"] == (Fraction(468), "kg")
    assert result.order["cement"] == (19, "bag")
    # sand = 0.975 m3, ordered in 0.01 m3 steps -> 0.98
    assert result.order["sand"] == (Fraction(98, 100), "m3")
    assert "lime" not in result.order


def test_concrete_water_and_cement_content_are_exact():
    mix, settings = _mix(
        "[mix a]\nkind = concrete\nratio = 1:2:3\nwater_cement = 0.5\nvolume = 1\nwaste = 0\n"
    )
    result = compute_mix(mix, settings)
    assert result.cement_kg_per_m3 == Fraction(1848, 5)  # 369.6 kg/m3
    assert result.needs["water"] == (Fraction(924, 5), "L")  # 184.8 L


def test_waste_allowance_is_applied_before_bulking():
    mix, settings = _mix("[mix a]\nkind = mortar\nratio = 1:0:3\nvolume = 1\nwaste = 10%\n")
    result = compute_mix(mix, settings)
    assert result.waste_volume == Fraction(11, 10)
    assert result.dry_volume == Fraction(11, 10) * Fraction(13, 10)


def test_missing_prices_are_warned_and_costed_at_zero():
    mix, settings = _mix("[mix a]\nkind = mortar\ntype = S\nvolume = 1\nsand_per_m3 = 40\n")
    result = compute_mix(mix, settings)
    assert any("cement_per_bag" in w for w in result.warnings)
    assert result.costs["cement"] == 0
    assert result.costs["sand"] == to_cents(result.order["sand"][0] * 40)


def test_high_water_cement_and_low_cement_content_warn():
    mix, settings = _mix("[mix a]\nkind = concrete\nratio = 1:2:3\nwater_cement = 0.7\nvolume = 1\n")
    assert any("above 0.60" in w for w in compute_mix(mix, settings).warnings)
    mix, settings = _mix("[mix a]\nkind = concrete\nratio = 1:3:6\nwater_cement = 0.5\nvolume = 1\n")
    assert any("below 300" in w for w in compute_mix(mix, settings).warnings)


def test_mixer_drums_are_counted_with_ceiling():
    mix, settings = _mix("[mix a]\nkind = mortar\nratio = 1:0:3\nvolume = 1\nwaste = 0\nbatch = 0.3\n")
    result = compute_mix(mix, settings)
    assert result.drums == 4  # 1 / 0.3 rounds up to 4 drums
    amount, unit = result.drum["cement"]
    assert unit == "kg"
    assert amount == Fraction(468) * Fraction(3, 10)


def test_yield_for_bags_matches_the_dry_volume_maths():
    mix, settings = _mix("[mix a]\nkind = mortar\nratio = 1:0:3\nvolume = 1\nwaste = 0\n")
    wet, usable = yield_for_bags(mix, settings, 19)
    expected = Fraction(19 * 25, 1440) * 4 / Fraction(13, 10)
    assert wet == expected
    assert usable == expected


def test_bag_recipe_gives_sand_in_proportion_to_cement():
    recipe = bag_recipe("concrete", (Fraction(1), Fraction(2), Fraction(3)), Fraction(25), Fraction(1, 2))
    litres = recipe["litres"]
    assert litres["sand"] == 2 * litres["cement"]
    assert litres["water"] == Fraction(25, 2)


def test_money_rounds_half_up_to_cents():
    assert to_cents(Fraction(1005, 1000)) == 101
    assert to_cents(Fraction(1004, 1000)) == 100
    assert round_up(Fraction(975, 1000), Fraction(1, 100)) == Fraction(98, 100)


def test_cli_plan_writes_csv_and_json(tmp_path, capsys):
    csv_path = tmp_path / "shopping.csv"
    assert main(["plan", str(EXAMPLE), "--csv", str(csv_path)]) == 0
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "mix,material,quantity,unit,cost"
    assert len(lines) > 4

    capsys.readouterr()
    assert main(["plan", str(EXAMPLE), "--json", "--mix", "footing"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["mixes"]] == ["footing"]
    assert payload["total_cents"] > 0


def test_cli_plan_rejects_unknown_mix_name(tmp_path):
    assert main(["plan", str(EXAMPLE), "--mix", "nope"]) == 1


def test_cli_check_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.mix"
    good.write_text("[mix a]\nkind = mortar\ntype = S\nvolume = 1\n", encoding="utf-8")
    assert main(["check", str(good)]) == 0

    unpriced_strict = tmp_path / "warn.mix"
    unpriced_strict.write_text("[mix a]\nkind = mortar\ntype = S\nvolume = 1\n", encoding="utf-8")
    assert main(["check", str(unpriced_strict), "--strict"]) == 1

    broken = tmp_path / "broken.mix"
    broken.write_text("[mix a]\nkind = mortar\nvolume = 1\n", encoding="utf-8")
    assert main(["check", str(broken)]) == 1
    assert "needs ratio or type" in capsys.readouterr().out

    assert main(["check", str(tmp_path / "missing.mix")]) == 2


def test_cli_yield_and_bag_commands(tmp_path, capsys):
    assert main(["yield", str(EXAMPLE), "footing", "--bags", "4"]) == 0
    assert "makes up to" in capsys.readouterr().out
    assert main(["yield", str(EXAMPLE), "footing", "--bags", "0"]) == 1

    assert main(["bag", "S"]) == 0
    assert "sand" in capsys.readouterr().out
    assert main(["bag", "1:2:3", "--kind", "concrete"]) == 1  # water needed
    assert main(["bag", "1:2:3", "--kind", "concrete", "--water-cement", "0.5"]) == 0


def test_module_exposes_version():
    assert mixwise.__version__ == "1.0.0"
