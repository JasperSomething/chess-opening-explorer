"""Render the generated course as a document a learner can read.

Everything here is a rendering of ``analysis/course.json``. No statement is authored
here: each line is the generator's own output for an object it selected, and each
block keeps the provenance of the object it came from.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def render(course, out=ROOT / 'analysis'):
    lines = ['# The generated Scandinavian course\n']
    lines.append('Machine-generated from the existing objects: structural families, '
                 'reusable schemas, exact decisions and their exceptions. Nothing in '
                 'this document was written by hand, and every item carries its '
                 'provenance (family, schema, plans, boards, statistics).\n')
    lines.append('Knowledge is ordered as the architecture requires — '
                 '**orientation/structure → reusable schema → critical exact decision → '
                 'exception** — and the generator enforces it: orientation items are '
                 'recognition only and carry no move, decisions are admitted only where '
                 'the selected schemas leave meaningful residual value, and every '
                 'exception hangs off the schema or decision it modifies.\n')
    lines.append('Admission rules, declared before selection and not tuned afterwards: '
                 f"a decision must remove at least "
                 f"{course['constants']['DECISION_MIN_ABSOLUTE']} of a board's remaining "
                 f"ordinary-play loss *and* at least "
                 f"{course['constants']['DECISION_MIN_SHARE_OF_RESIDUAL']:.0%} of the "
                 f"residual the schemas leave; a board where a schema's prescription "
                 f"covers less than "
                 f"{course['constants']['DEVIATION_MAX_STRONG_SHARE']:.0%} of what strong "
                 f"players played is an exception rather than a rule.\n")

    lines.append('## Curriculum by learning budget\n')
    lines.append('| budget | items | orientation | schemas | decisions | exceptions available | '
                 'flow coverage | strong-play behaviour explained | EV recovered | exact '
                 'positions memorised | residual unexplained value |')
    lines.append('|' + '---|' * 11)
    for budget, entry in course['courses'].items():
        report = entry['report']
        lines.append(
            f"| {budget} | {report['items']} | {report['orientation']} | "
            f"{report['schemas']} | {report['decisions']} | {report['exceptions_available']} | "
            f"{report['flow_coverage']:.3f} | {report['behaviour_explained']:.3f} | "
            f"{report['value_recovered']:.5f} ({report['recovery']:.3f} of the "
            f"ordinary-play loss) | {report['positions_memorised']} | "
            f"{report['residual_value']:.5f} |")
    lines.append('')

    for budget, entry in course['courses'].items():
        report = entry['report']
        lines.append(f'\n## Learning budget {budget} — {report["items"]} items, '
                     f'{report["burden"]:.0f} burden units\n')
        by_level = {}
        for item in entry['items']:
            by_level.setdefault(item['kind'], []).append(item)

        for item in by_level.get('orientation', []):
            lines.append(f"### Orientation — `{item['title']}` (recognition only)\n")
            lines.append(f"{item['boards']} boards, flow mass {item['flow_mass']:.4f}, "
                         f"reach {item['reach']:.4f}, ply mean "
                         f"{_round(item['recognition'].get('ply_mean'))}, development "
                         f"{_round(item['recognition'].get('development_level'))}.\n")
            occupancy = item['recognition']['look_for'].get('recurring_occupancy') or []
            if occupancy:
                lines.append('Where each piece already stands in this structure '
                             '(share of that piece\'s reach-weighted squares):\n')
                for entry_row in occupancy[:8]:
                    squares = ', '.join(f"{row['square']} {row['share']:.2f}"
                                        for row in entry_row['squares'][:3])
                    lines.append(f"    {entry_row['piece']}: {squares}")
                lines.append('')
            structure_row = item['recognition']['look_for'].get('pawn_structure')
            if structure_row:
                lines.append(f"This structure is defined by its pawn placement: white "
                             f"{_squares(structure_row['white'])}, black "
                             f"{_squares(structure_row['black'])}.\n")
            lines.append('**This item names no move.** It tells you only what you are '
                         'looking at.\n')
            if item.get('destinations'):
                lines.append('Recurring destinations from strong play (for the board view):\n')
                for row in item['destinations'][:8]:
                    lines.append(f"    {row['piece']} {row['from']}->{row['to']}  "
                                 f"{row['share']:.3f}")
                lines.append('')

        for item in by_level.get('schema', []):
            lines.append(f"### Schema — `{item['title']}`\n")
            lines.append(f"Applies to {item['applicability']['boards']} boards in "
                         f"families {', '.join(f[:8] for f in item['applicability']['families'])}"
                         f", side to move {', '.join(item['applicability']['sides'])}.\n")
            lines.append('Required transformations (what the schema always does):\n')
            for row in item['transformations']['required']:
                lines.append(f"    - {row['text']}")
            if item['transformations']['optional']:
                lines.append('\nCommon but not required:\n')
                for row in item['transformations']['optional']:
                    lines.append(f"    - {row['text']}")
            if item['transformations']['residual']:
                lines.append('\nSeen in some covered plans, not part of the schema:\n')
                for row in item['transformations']['residual']:
                    lines.append(f"    - {row['text']}")
            ordering = item['ordering']
            lines.append(f"\nOrdering: flexibility {_round(ordering['flexibility'])} "
                         f"({'constrained' if ordering['constrained'] else 'largely free'})")
            for constraint in ordering['constraints'][:3]:
                lines.append(f"    - {constraint}")
            states = item['completion_state'] or {}
            if states:
                summary = ', '.join(f"{state} {value['share']:.2f}"
                                    for state, value in sorted(states.items()))
                lines.append(f"\nCompletion state across its boards: {summary}")
            lines.append(f"\nBurden {item['burden']:.0f}; marginal value "
                         f"{_round(item.get('marginal_value'), 6)}; covers "
                         f"{item['positions']} positions; flow mass "
                         f"{item['flow_mass']:.4f}.\n")
            lines.append(f"Provenance: schema `{item['provenance']['schema_id']}`, plans "
                         f"{', '.join(pid[:8] for pid in item['provenance']['plan_ids'][:6])}, "
                         f"tables {', '.join(item['provenance']['sources'])}.\n")

        decisions = by_level.get('decision', [])
        if decisions:
            lines.append('### Critical exact decisions\n')
            lines.append('Admitted only against the residual the schemas leave. Each is one '
                         'move at one board, and each is shown with the reason it qualified.\n')
            lines.append('| board | move | residual before | value gained | share of residual | burden |')
            lines.append('|---|---|---|---|---|---|')
            for item in decisions:
                why = item['why_admitted']
                lines.append(f"| `{item['board']['position_key'][:8]}` | "
                             f"{' '.join(item['prescribes']['moves'])} | "
                             f"{why['residual_before']:.5f} | {why['value_gained']:.5f} | "
                             f"{why['share_of_residual']:.2f} | {item['burden']:.0f} |")
            lines.append('')

        exceptions = entry.get('exceptions') or []
        if exceptions:
            lines.append('### Deviations, attached to what they modify\n')
            lines.append('A deviation is stored under the schema or decision it modifies and '
                         'is never shown as a branch of its own: it is what strong players '
                         'did instead at a board where the rule covers less than half of '
                         'their play.\n')
            lines.append('| modifies | board | strong move instead | strong share | schema coverage |')
            lines.append('|---|---|---|---|---|')
            for item in exceptions[:12]:
                why = item['why_selected']
                lines.append(f"| `{item['modifies'].split(':')[0]}:"
                             f"{item['modifies'].split(':')[-1][:8]}` | "
                             f"`{item['board']['position_key'][:8]}` | "
                             f"{why['observed_move']} | {why['observed_share']:.2f} | "
                             f"{why['schema_share_of_strong_play']:.2f} |")
            if len(exceptions) > 12:
                lines.append(f"\n({len(exceptions) - 12} further deviations are attached in "
                             f"the generated course file.)")
            lines.append('')

        lines.append('### Marginal value of every item in this budget\n')
        lines.append('| step | item | kind | marginal burden | marginal value | value per burden |')
        lines.append('|---|---|---|---|---|---|')
        for step, row in enumerate(entry['marginal'], start=1):
            lines.append(f"| {step} | `{row['item_id'][:28]}` | {row['kind']} | "
                         f"{row['marginal_burden']:.0f} | {row['marginal_value']:.6f} | "
                         f"{row['value_per_burden']:.6f} |")
        lines.append('')

    (out / 'scandinavian-course.md').write_text('\n'.join(lines) + '\n')
    return out / 'scandinavian-course.md'


def _squares(bitmask):
    """Decode the stored placement bitmask into square names (rendering, not advice)."""
    try:
        bits = int(bitmask, 16)
    except (TypeError, ValueError):
        return str(bitmask)
    names = []
    for square in range(64):
        if bits >> square & 1:
            names.append(chr(ord('a') + square % 8) + str(square // 8 + 1))
    return ' '.join(names) or 'none'


def _round(value, digits=3):
    return 'n/a' if value is None else round(value, digits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=Path, default=ROOT / 'analysis' / 'course.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    options = parser.parse_args()
    course = json.loads(options.course.read_text())
    print(render(course, options.out))


if __name__ == '__main__':
    main()
