# mixwise

**Offline mortar and concrete batch planner.** Describe your pours in one plain-text job file and get exact batch quantities, whole-bag purchase counts, mixer-drum recipes and a costed shopping list, with line-numbered errors.

Mixwise is a single Python file with no dependencies beyond the standard library. It makes no network calls, needs no account, and stores nothing except the files you ask it to write.

## Features

- **Plain-text job files** in an INI-like format with `[settings]` and `[mix NAME]` sections, `#` comments and per-line error reporting.
- **ASTM C270 mortar types** (`type = M`, `S`, `N` or `O`) as well as custom cement : lime : sand ratios.
- **Concrete mixes** from a cement : sand : gravel ratio and a water-cement ratio, with water litres per order.
- **Exact arithmetic** using rational numbers. Only purchase quantities are rounded, so nothing drifts.
- **Bulking and waste**: wet volume plus a waste allowance is converted to dry volume with a bulking factor for each mix kind.
- **Whole-bag ordering** for cement and lime (rounded up), and 0.01 m³ ordering steps for sand and gravel.
- **Costs in exact cents** with half-up rounding, from per-bag and per-m³ prices in the job file.
- **Mixer drum planning**: `batch = 100 L` gives the drum count and the quantities for one full drum.
- **Shopping list** totalled across every mix in the job, printed as text, as JSON (`--json`) or as CSV (`--csv`).
- **Mix filtering**: `plan --mix footing` plans only the named mixes.
- **Yield check**: `yield` shows how much wet mix a given number of cement bags will make, before and after waste.
- **Bag recipes**: `bag` gives the loose litres of each material per bag of cement, a quick field check for any ratio.
- **Guideline warnings** for a water-cement ratio above 0.60, cement content below 300 kg/m³, and any unpriced material.
- **CI-friendly linting**: `check` exits 1 on errors, and `check --strict` also fails on warnings.

## Quick start

Requires Python 3.10 or newer.

```bash
python3 mixwise.py plan examples/garden-wall.mix
```

The example job file, `examples/garden-wall.mix`, plans three mixes: a type S wall mortar, a 1:2:3 footing concrete run in 100 L drums, and a cap mortar.

To see the quantities behind one bag of cement without a job file:

```bash
python3 mixwise.py bag S
```

Expected output:

```
Per 25 kg bag of cement (mortar, type S):
  cement        17.4 L loose
  lime           8.7 L loose
  sand          78.1 L loose
  wet yield 80.1 L
```

## Job file format

```ini
[settings]
cement_bag_kg = 25
lime_bag_kg = 25
currency = $

[mix wall-mortar]
kind = mortar
type = S              # ASTM C270 type S, cement : lime : sand = 1 : 1/2 : 4.5
volume = 800 L        # wet volume; plain numbers are m3
waste = 5%
cement_per_bag = 7.20
lime_per_bag = 5.50
sand_per_m3 = 45.00

[mix footing]
kind = concrete
ratio = 1:2:3        # cement : sand : gravel, by volume
water_cement = 0.50
volume = 0.6
waste = 5%
batch = 100 L        # one mixer drum
cement_per_bag = 7.20
sand_per_m3 = 45.00
gravel_per_m3 = 60.00
```

| Key | Applies to | Meaning |
| --- | --- | --- |
| `kind` | mix | `mortar` or `concrete` (required) |
| `type` | mortar | ASTM C270 letter `M`, `S`, `N` or `O` |
| `ratio` | mix | parts as `a:b:c`; fractions such as `1/4` are allowed |
| `volume` | mix | wet volume in m³, or `L` (required) |
| `waste` | mix | allowance as a percentage, default 5% |
| `water_cement` | concrete | water-cement ratio (required for concrete) |
| `batch` | mix | mixer drum wet volume, optional |
| `cement_per_bag`, `lime_per_bag` | mix | prices per bag |
| `sand_per_m3`, `gravel_per_m3` | mix | prices per m³ |
| `cement_bag_kg`, `lime_bag_kg`, `currency` | settings | bag sizes and currency label |

Mortar uses `type` or `ratio`, not both. Concrete needs a `ratio` with sand and gravel parts above zero.

## Commands

```text
mixwise plan FILE [--mix NAME ...] [--json] [--csv PATH]
mixwise check FILE [--strict]
mixwise yield FILE NAME --bags N
mixwise bag SPEC [--kind mortar|concrete] [--water-cement R] [--bag-kg KG]
```

Exit codes: `0` success, `1` the job file has errors (or warnings under `--strict`), `2` the file cannot be read.

## Assumptions

These constants are fixed in `mixwise.py`:

- Loose Portland cement weighs 1440 kg/m³, and loose hydrated lime weighs 500 kg/m³.
- Dry volume is 1.30 × wet volume for mortar and 1.54 × wet volume for concrete.
- Mortar proportions are the midpoints of the ASTM C270 ranges: M 1:¼:3, S 1:½:4.5, N 1:1:6, O 1:2:9.
- Sand and gravel are ordered in 0.01 m³ steps.

Check these against your supplier's data before you order. Mixwise plans quantities but does not design structures.

## Testing

```bash
python3 -m pip install pytest
python3 -m pytest tests
```

The tests cover unit parsing, line-numbered errors, exact mortar and concrete quantities, waste and bulking, drum batches, yield, bag recipes, money rounding and the CLI exit codes.

## Layout

```text
mixwise.py              the planner: parser, calculations, output and CLI
examples/garden-wall.mix  a sample job file
tests/                  pytest suite
```
