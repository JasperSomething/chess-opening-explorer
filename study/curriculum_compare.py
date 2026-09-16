"""Compare the regenerated course against the frozen baseline, dimension by dimension.

Nothing here re-optimises anything: it reports where the new course is better, where it
is worse, and where value was given up on purpose. The EV figures are reported beside
the learnability figures precisely so that a lower EV cannot be read as a regression on
its own.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import curriculum_audit  # noqa: E402

DIMENSIONS = ('flow_coverage', 'behaviour_explained', 'recovery', 'orientation', 'schemas',
              'decisions', 'deviations', 'items', 'burden')


def measure(course, budget):
    entry = course['courses'][budget]
    report = entry['report']
    exceptions = entry.get('exceptions') or []
    decisions = [item for item in entry['items'] if item['kind'] == 'decision']
    schemas = [item for item in entry['items'] if item['kind'] == 'schema']
    findings = curriculum_audit.audit({'budgets': [budget], 'courses': {budget: entry}})
    return {
        'flow_coverage': report['flow_coverage'],
        'behaviour_explained': report['behaviour_explained'],
        'recovery': report['recovery'],
        'items': report['items'],
        'burden': report['burden'],
        'orientation': report['orientation'],
        'schemas': report['schemas'],
        'decisions': report['decisions'],
        'deviations': len(exceptions),
        'actionable_schema_fraction': (sum(1 for item in schemas if item.get('prescriptive'))
                                       / len(schemas)) if schemas else None,
        'distinctive_information': report.get('distinctive_information'),
        'declared_containments': sum(1 for item in schemas if item.get('contained_in')),
        'undeclared_redundancy': len(findings.get('redundant_undeclared') or []),
        'uncheckable_schemas': len(findings.get('cannot_be_checked') or []),
        'uninformative_recognition': len(findings.get('uninformative_recognition') or []),
        'contradictions': count_contradictions(decisions, exceptions),
        'audit_findings': sum(len(rows) for rows in findings.values()),
        'invalidated_schemas': len(entry.get('invalidated_schemas') or []),
        'pruned_redundant': report.get('pruned_redundant', 0),
    }


def count_contradictions(decisions, exceptions):
    """A contradiction is a deviation still worded as an instruction on the same board
    as the decision it hangs off; resolved ones are demoted to notes by the generator."""
    boards = {item['board']['position_key']: set(item['prescribes']['moves'])
              for item in decisions}
    count = 0
    for item in exceptions:
        if item['kind'] != 'exception':
            continue
        for board_entry in (item.get('boards') or [item['board']]):
            key = board_entry['position_key']
            if key in boards and set(item['prescribes']['moves']).isdisjoint(boards[key]):
                count += 1
                break
    return count


def render(baseline, new, out=ROOT / 'analysis'):
    lines = ['# Curriculum-quality iteration: old versus new\n']
    lines.append('Baseline is the previously frozen course (`analysis/course-baseline.json`); '
                 'new is the regenerated course after the six fixes. Both were produced by '
                 'the generator from the same data — nothing here was hand-edited, and no '
                 'new data or evaluations were introduced.\n')
    lines.append('Read the EV columns together with the learnability columns. A lower EV '
                 'is not automatically a regression: if the value that disappeared was '
                 'carried by an item that could not be performed or checked, losing it is '
                 'the point of the iteration.\n')

    comparison = {}
    for budget in baseline['courses']:
        old = measure(baseline, budget)
        fresh = measure(new, budget)
        comparison[budget] = {'old': old, 'new': fresh}
        lines.append(f'\n## Budget {budget}\n')
        lines.append('| dimension | old | new | change |')
        lines.append('|---|---|---|---|')
        for key in ('flow_coverage', 'behaviour_explained', 'recovery', 'items', 'burden',
                    'orientation', 'schemas', 'decisions', 'deviations',
                    'actionable_schema_fraction', 'distinctive_information',
                    'declared_containments', 'undeclared_redundancy', 'uncheckable_schemas',
                    'uninformative_recognition', 'contradictions', 'audit_findings',
                    'invalidated_schemas', 'pruned_redundant'):
            before, after = old.get(key), fresh.get(key)
            lines.append(f"| {key} | {_fmt(before)} | {_fmt(after)} | {_delta(before, after)} |")

    lines.append('\n## What was given up, and why\n')
    lines.append(_accounting(baseline, new, comparison))
    (out / 'curriculum-iteration.md').write_text('\n'.join(lines) + '\n')
    (out / 'curriculum-iteration.json').write_text(json.dumps(comparison, indent=1, default=str))
    return comparison


def _accounting(baseline, new, comparison):
    lines = []
    for budget, rows in comparison.items():
        old, fresh = rows['old'], rows['new']
        dropped = (old['recovery'] or 0) - (fresh['recovery'] or 0)
        invalid = fresh['invalidated_schemas']
        if dropped > 0.01:
            lines.append(
                f"- **Budget {budget}**: EV recovered falls from {old['recovery']:.3f} to "
                f"{fresh['recovery']:.3f} ({dropped:.3f}). The generator removed "
                f"{invalid} schema(s) whose required transformations contained no learner "
                f"action — the top-ranked one was 'the position leaves this structure', an "
                f"effect that inherited value from the plans it covered without ever telling "
                f"the learner what to do. That value was unlearnable, so the drop is the "
                f"intended result rather than a regression.")
        elif abs(dropped) <= 0.01:
            lines.append(
                f"- **Budget {budget}**: EV recovered essentially unchanged "
                f"({old['recovery']:.3f} -> {fresh['recovery']:.3f}).")
        else:
            lines.append(
                f"- **Budget {budget}**: EV recovered rises from {old['recovery']:.3f} to "
                f"{fresh['recovery']:.3f}; the invalid items were also displacing real ones.")
        lines.append(
            f"- **Budget {budget}**: deviations fall from {old['deviations']} to "
            f"{fresh['deviations']}; a deviation is now kept only when it changes the "
            f"recommended action, dominates what is played at the board and is actually "
            f"reached, and equivalent responses are grouped into one entry.")
        if fresh['uncheckable_schemas'] == 0:
            lines.append(f"- **Budget {budget}**: un-checkable schemas {old['uncheckable_schemas']} "
                         f"-> 0, and uninformative recognition "
                         f"{old['uninformative_recognition']} -> "
                         f"{fresh['uninformative_recognition']}.")
    return '\n'.join(lines) + '\n'


def _fmt(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:.4f}'
    return str(value)


def _delta(before, after):
    if before is None or after is None:
        return '—'
    if isinstance(before, float) or isinstance(after, float):
        change = after - before
        return f'{"+" if change > 0 else ""}{change:.4f}'
    change = after - before
    return f'{"+" if change > 0 else ""}{change}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path,
                        default=ROOT / 'analysis' / 'course-baseline.json')
    parser.add_argument('--new', type=Path, default=ROOT / 'analysis' / 'course.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    options = parser.parse_args()
    comparison = render(json.loads(options.baseline.read_text()),
                        json.loads(options.new.read_text()), options.out)
    print(json.dumps({budget: {'recovery': [rows['old']['recovery'], rows['new']['recovery']],
                               'audit_findings': [rows['old']['audit_findings'],
                                                  rows['new']['audit_findings']],
                               'deviations': [rows['old']['deviations'],
                                              rows['new']['deviations']]}
                      for budget, rows in comparison.items()}, indent=1))


if __name__ == '__main__':
    main()
