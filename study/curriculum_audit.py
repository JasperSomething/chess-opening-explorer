"""Audit the generated course by scanning its own output for failure modes.

The audit is computed from ``analysis/course.json``; it does not repair anything. It
looks for the specific ways a machine-generated course goes wrong: items that teach
nothing, items that cannot be checked against a board, items nobody reaches, items a
learner cannot parse, and items that contradict each other.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# components the learner can actually complete on a board: a goal is a piece or pawn
# reaching a square, a transform is the exact move, a castle is the castle itself.
# 'transition' is an effect of the position, not something the learner does.
ACTION_COMPONENTS = {'goal', 'transform', 'castle'}


def audit(course):
    findings = defaultdict(list)
    for budget, entry in course['courses'].items():
        items = entry['items']
        schemas = [item for item in items if item['kind'] == 'schema']
        decisions = [item for item in items if item['kind'] == 'decision']
        orientation = [item for item in items if item['kind'] == 'orientation']

        # 1. schemas that cannot be checked against any board: nothing the learner does
        for item in schemas:
            required = item['transformations']['required']
            decidable = [row for row in required
                         if row['component'][0] in ACTION_COMPONENTS]
            if not decidable:
                findings['cannot_be_checked'].append(
                    (budget, item['title'], f"{len(required)} transformations, none of them "
                                            f"an action the learner can complete"))

        # 2. schemas whose required set is contained in another schema's required set.
        # Both surviving is allowed, but the containment must be declared in the course.
        for a in schemas:
            for b in schemas:
                if a is b:
                    continue
                set_a = {tuple(row['component']) for row in a['transformations']['required']}
                set_b = {tuple(row['component']) for row in b['transformations']['required']}
                if set_a and set_a < set_b:
                    # declared means: the selector recorded the containment and stated why
                    # it was retained. One narrow schema can sit inside several broader
                    # ones, so the declaration is a property of the item, not of the pair.
                    declared = bool(a.get('contained_in')) and bool(a.get('retained_because'))
                    key = 'redundant_declared' if declared else 'redundant_undeclared'
                    findings[key].append(
                        (budget, a['title'],
                         f"contained in '{b['title']}'"
                         + (f"; retained for its novel applicability: "
                            f"{a.get('retained_because')}" if declared else
                            "; the selector never recorded this containment")))

        # 3. duplicate titles: the learner sees the same thing twice
        titles = Counter(item['title'] for item in items)
        for title, count in titles.items():
            if count > 1:
                findings['duplicate_titles'].append((budget, title, f"{count} items"))

        # 4. items nobody reaches
        for item in items:
            reach = None
            if item.get('board'):
                reach = item['board'].get('reach')
            elif item.get('representative_board'):
                reach = item['representative_board'].get('reach')
            if reach is not None and reach < 1e-4:
                findings['unreachable'].append(
                    (budget, item['title'], f"reach {reach:.2e}"))

        # 5. recognition items whose leading fact still fails to separate the family
        # from the alternatives. The headline is the distinctive fact list; the raw
        # occupancy block is provenance and is deliberately not what is judged.
        for item in orientation:
            distinctive = item.get('distinctive') or []
            if not distinctive:
                findings['uninformative_recognition'].append(
                    (budget, item['title'], 'no distinguishing fact survived scoring'))
                continue
            top = distinctive[0]
            if top['lift'] < 1.2:
                findings['uninformative_recognition'].append(
                    (budget, item['title'],
                     f"its leading fact '{top['fact']}' appears at "
                     f"{top['present_in_family']:.2f} here against "
                     f"{top['present_elsewhere']:.2f} elsewhere (lift {top['lift']:.2f}): "
                     f"almost no separation"))

        # 6. items a learner cannot parse at a glance
        for item in schemas:
            required = item['transformations']['required']
            if len(required) >= 4:
                findings['hard_to_parse'].append(
                    (budget, item['title'], f"{len(required)} required transformations"))
            if (item['ordering']['flexibility'] is not None
                    and item['ordering']['flexibility'] > 0.4 and len(required) > 2):
                findings['hard_to_parse'].append(
                    (budget, item['title'],
                     f"ordering flexibility {item['ordering']['flexibility']:.2f} with "
                     f"{len(required)} transformations: the order is neither fixed nor free"))

        # 7. decisions that contradict an exception attached to them
        for item in entry.get('exceptions') or []:
            if not item['modifies'].startswith('decision:'):
                continue
            parent = next((candidate for candidate in decisions
                           if candidate['item_id'] == item['modifies']), None)
            if parent is None:
                findings['orphan_exceptions'].append(
                    (budget, item['item_id'], f"modifies {item['modifies']}, not selected"))
                continue
            taught = parent['prescribes']['moves']
            observed = item['prescribes']['moves']
            if set(taught).isdisjoint(set(observed)):
                findings['contradictory'].append(
                    (budget, item['modifies'],
                     f"decision says {' '.join(taught)}, the deviation under it says "
                     f"{' '.join(observed)}"))

        # 8. deviations outnumbering the rules they hang from
        exceptions = entry.get('exceptions') or []
        if len(exceptions) > 5 * max(1, len(schemas) + len(decisions)):
            findings['deviation_swamp'].append(
                (budget, f"{len(exceptions)} deviations",
                 f"against {len(schemas)} schemas and {len(decisions)} decisions"))

    return findings


LABELS = {
    'cannot_be_checked': 'Schemas that cannot be checked against a board',
    'redundant_undeclared': 'Schemas contained in another schema, never declared',
    'redundant_declared': 'Schemas contained in another schema, declared and justified',
    'duplicate_titles': 'Items the learner sees twice under the same name',
    'unreachable': 'Items attached to positions nobody reaches',
    'uninformative_recognition': 'Recognition items that convey no distinction',
    'hard_to_parse': 'Items that are hard to read at a glance',
    'contradictory': 'Items that contradict another selected item',
    'orphan_exceptions': 'Deviations whose parent was not selected',
    'deviation_swamp': 'Deviation counts that overwhelm the rules',
}


def render(course, out=ROOT / 'analysis'):
    findings = audit(course)
    lines = ['# Audit of the generated curriculum\n']
    lines.append('Produced by scanning the generator\'s own output for the ways a '
                 'machine-generated course goes wrong. Nothing here has been repaired: '
                 'the failures are exactly what the algorithm produced.\n')
    lines.append(f"Budgets audited: {', '.join(str(b) for b in course['budgets'])}.\n")
    total = sum(len(rows) for rows in findings.values())
    lines.append(f'Total flagged findings: **{total}** across '
                 f'{len([k for k, v in findings.items() if v])} categories.\n')

    for key, label in LABELS.items():
        rows = findings.get(key) or []
        lines.append(f'\n## {label} — {len(rows)}\n')
        if not rows:
            lines.append('None found.\n')
            continue
        lines.append('| budget | item | why it is a problem |')
        lines.append('|---|---|---|')
        seen = set()
        for budget, item, reason in rows:
            signature = (item, reason)
            if signature in seen:
                continue
            seen.add(signature)
            lines.append(f'| {budget} | {str(item)[:80]} | {reason} |')
        if len(rows) > len(seen):
            lines.append(f'\n({len(rows) - len(seen)} further repeats omitted.)\n')

    lines.append('\n## Reading of the failures\n')
    lines.append(_reading(findings, course))
    (out / 'curriculum-audit.md').write_text('\n'.join(lines) + '\n')
    return findings


def _reading(findings, course):
    """A plain description of what the flagged categories mean, written from the counts."""
    parts = []
    if findings.get('cannot_be_checked'):
        parts.append(
            f"{len(findings['cannot_be_checked'])} schema selections are built entirely "
            f"from transformations that are effects rather than actions (for example the "
            f"position leaving its structural family). A learner cannot perform them and "
            f"the interface cannot mark them complete, so the selected 'rule' is not a rule "
            f"— yet the selector ranked it highly, which is a flaw in the objective rather "
            f"than in the data.")
    if findings.get('redundant_undeclared'):
        parts.append(
            f"{len(findings['redundant_undeclared'])} schemas are strict subsets of another "
            f"selected schema without the selector recording it: a learner would be shown "
            f"a weaker version of something already presented, with no stated reason.")
    if findings.get('redundant_declared'):
        parts.append(
            f"{len(findings['redundant_declared'])} containments are declared and justified: "
            f"in each case the narrower schema reaches boards the broader one does not (or "
            f"is materially better on shared boards), so it is retained on purpose rather "
            f"than duplicated by accident. A human may still judge that learning both is not "
            f"worth it, which is exactly the kind of judgement this audit is meant to expose "
            f"rather than settle.")
    if findings.get('uninformative_recognition'):
        parts.append(
            f"{len(findings['uninformative_recognition'])} orientation items lead with a "
            f"fact that holds almost everywhere, so they read as 'notice that the king is "
            f"on e8' — technically true, practically empty.")
    if findings.get('unreachable'):
        parts.append(
            f"{len(findings['unreachable'])} items sit on positions with negligible "
            f"reach, so the course asks the learner to learn something essentially no game "
            f"arrives at.")
    if findings.get('hard_to_parse'):
        parts.append(
            f"{len(findings['hard_to_parse'])} schemas are hard to hold in mind: several "
            f"required transformations with no fixed order, which a learner cannot "
            f"distinguish from a list of suggestions.")
    if findings.get('contradictory'):
        parts.append(
            f"{len(findings['contradictory'])} deviations directly contradict the decision "
            f"they are attached to, presenting two different moves for the same board "
            f"without saying which takes precedence.")
    if not parts:
        parts.append('No findings in the flagged categories.')
    return '\n\n'.join(parts) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=Path, default=ROOT / 'analysis' / 'course.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    options = parser.parse_args()
    course = json.loads(options.course.read_text())
    findings = render(course, options.out)
    print(json.dumps({key: len(rows) for key, rows in findings.items()}, indent=1))


if __name__ == '__main__':
    main()
