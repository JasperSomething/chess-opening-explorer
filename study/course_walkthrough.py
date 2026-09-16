"""A short walkthrough of one budget of the generated course: what it teaches, in order.

Written from the course file only. It is deliberately short: the point is to let a
player read the whole curriculum in a couple of minutes and judge whether it looks
like something worth learning.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def render(course, budget='25', out=ROOT / 'analysis'):
    entry = course['courses'][budget]
    report = entry['report']
    lines = [f'# The {budget}-unit Scandinavian course\n']
    lines.append(f'{report["items"]} items in {report["burden"]:.0f} marginal burden units. '
                 f'Flow coverage {report["flow_coverage"]:.0%}; strong-play behaviour '
                 f'explained {report["behaviour_explained"]:.0%}; measured EV recovered '
                 f'{report["recovery"]:.0%} of the ordinary-play loss. Exact positions to '
                 f'memorise: {report["positions_memorised"]}.\n')
    lines.append('Every line below is generator output. Statistics are available through '
                 'each item\'s provenance and are not shown here.\n')

    orientation = [item for item in entry['items'] if item['kind'] == 'orientation']
    schemas = [item for item in entry['items'] if item['kind'] == 'schema']
    decisions = [item for item in entry['items'] if item['kind'] == 'decision']
    exceptions = [item for item in entry.get('exceptions') or []
                  if item.get('kind') == 'exception']
    notes = [item for item in entry.get('exceptions') or [] if item.get('kind') == 'note']

    lines.append(f'\n## 1. Recognise where you are ({len(orientation)} items)\n')
    lines.append('No moves here. Learn only to tell these structures apart.\n')
    for item in orientation:
        lines.append(f'**{item["title"]}** — {item["boards"]} boards, flow mass '
                     f'{item["flow_mass"]:.2f}.')
        for row in (item.get('distinctive') or [])[:3]:
            lines.append(f"    {row['fact']} ({row['present_in_family']:.0%} here, "
                         f"{row['present_elsewhere']:.0%} elsewhere)")
        lines.append('')

    lines.append(f'\n## 2. The plans to understand ({len(schemas)} schemas)\n')
    for item in schemas:
        required = ' + '.join(row['text'] for row in item['transformations']['required'])
        lines.append(f'**{required or item["title"]}**')
        lines.append(f"    applies to {item['applicability']['boards']} boards, "
                     f"families {', '.join(f[:8] for f in item['applicability']['families'])}")
        optional = [row['text'] for row in item['transformations']['optional']]
        if optional:
            lines.append(f"    common but not required: {', '.join(optional[:4])}")
        effects = [row['text'] for row in
                   item['transformations'].get('expected_consequence') or []]
        if effects:
            lines.append(f"    expect as a consequence (not something you do): "
                         f"{', '.join(effects)}")
        ordering = item['ordering']
        lines.append(f"    order: {'fixed' if ordering['constrained'] else 'largely free'}"
                     f" (flexibility {ordering['flexibility']:.2f})")
        diagnostic = item.get('coherence') or {}
        if diagnostic:
            lines.append(f"    coherence: {diagnostic['distinct_orderings']} distinct "
                         f"orderings, entropy {diagnostic['ordering_entropy_bits']:.2f} bits, "
                         f"{diagnostic['conditional_branches']} conditional branches")
        if item.get('retained_because'):
            lines.append(f"    note: {item['retained_because']}")
        lines.append('')

    if decisions:
        lines.append(f'\n## 3. Positions worth memorising exactly ({len(decisions)})\n')
        lines.append('Admitted only where the schemas above leave measurable value behind.\n')
        for item in decisions:
            why = item['why_admitted']
            lines.append(f"**{item['title']}** — play {' '.join(item['prescribes']['moves'])}. "
                         f"Left over after the schemas: {why['residual_before']:.4f}; this "
                         f"removes {why['value_gained']:.4f} "
                         f"({why['share_of_residual']:.0%} of the residual).")
        lines.append('')

    if exceptions:
        lines.append(f'\n## 4. Where the plans change ({len(exceptions)} deviations)\n')
        lines.append('Each one belongs to the schema or decision above it and is kept only '
                     'when it changes what you would play.\n')
        for item in exceptions:
            why = item['why_selected']
            lines.append(f"* under `{item['modifies'].split(':')[0]}`: play "
                         f"{why['observed_move']} instead on "
                         f"{item.get('occurrences', 1)} board(s) — strong players choose it "
                         f"{why['observed_share']:.0%} of the time there, while the rule "
                         f"covers {why['schema_share_of_strong_play']:.0%}")
        lines.append('')

    if notes:
        lines.append('\n## 5. Resolved conflicts\n')
        for item in notes:
            lines.append(f"* {item['title']} (condition: {item['condition']['explicit']})")
        lines.append('')

    (out / 'course-walkthrough.md').write_text('\n'.join(lines) + '\n')
    return out / 'course-walkthrough.md'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=Path, default=ROOT / 'analysis' / 'course.json')
    parser.add_argument('--budget', default='25')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    options = parser.parse_args()
    print(render(json.loads(options.course.read_text()), options.budget, options.out))


if __name__ == '__main__':
    main()
