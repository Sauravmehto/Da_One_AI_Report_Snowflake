"""How many tabs an AI-built dashboard gets — decided here, not left to a prompt.

The planner is TOLD to use one tab for a small dashboard and to keep every tab
worth clicking into, and it did not reliably listen: a nine-panel dashboard
came back across four tabs of two charts each, and a run of edits grew one to
six tabs with a single chart on the last. Readers took the empty space to mean
there was little in the dashboard at all.

Two entry points, for the two ways the AI shapes a dashboard:

- `consolidate_sections` for a NEW plan: the whole layout is the model's
  proposal, so all of it is brought within the rules.
- `settle_new_sections` for an EDIT: only tabs the edit itself introduces are
  checked. An edit promises to change nothing outside the request, so tabs that
  already exist are never merged or renamed by it — however they got there.

Neither runs on a person's own arrangement (the Arrange view, the panel
editor). A tab someone made on purpose is theirs.
"""

from __future__ import annotations

from typing import Any

#: Fewer body panels than this and the dashboard is one tab. A reader should not
#: click to see what fits on one scrolling page. Twice MIN_PANELS_PER_TAB: below
#: it, no split could give every tab its minimum.
SINGLE_TAB_BELOW = 12
#: A tab holding fewer chart/table panels than this is folded into its neighbour.
#: Six, so a tab someone clicks into is a page of its own — the planner is asked
#: for 6 to 8 per tab, and this is the part of that which is enforced.
MIN_PANELS_PER_TAB = 6
#: Past this many tabs, navigation costs more than the grouping gives back.
MAX_TABS = 4


def _is_kpi(panel: dict[str, Any]) -> bool:
    """KPI panels render in a band above the grid, so they fill no tab."""
    kind = (panel.get("chart_type") or "").lower()
    display = (panel.get("display") or "").lower()
    return kind == "kpi" or display == "kpi"


def _section(panel: dict[str, Any]) -> str | None:
    value = panel.get("section")
    return value.strip() or None if isinstance(value, str) else None


def _ordered_sections(panels: list[dict[str, Any]]) -> list[str]:
    """Named sections in the order a reader meets them, which is tab order."""
    seen: list[str] = []
    for panel in panels:
        name = _section(panel)
        if name and name not in seen:
            seen.append(name)
    return seen


def _body_counts(panels: list[dict[str, Any]], mapping: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for panel in panels:
        name = _section(panel)
        if name is None:
            continue
        target = mapping[name]
        counts.setdefault(target, 0)
        if not _is_kpi(panel):
            counts[target] += 1
    return counts


def _renamed(panels: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for panel in panels:
        name = _section(panel)
        target = mapping.get(name) if name else None
        out.append({**panel, "section": target} if target and target != panel.get("section")
                   else dict(panel))
    return out


def consolidate_sections(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A new plan's tabs, brought within the rules. Panels keep their order; only
    `section` is rewritten.

    A tab too small to keep is folded into the tab BEFORE it (the one after, for
    the first tab): the planner orders panels as a story, so the neighbour is
    the grouping the stray panels were written to follow. The smallest tab goes
    first, which is the merge that changes the fewest panels.
    """
    order = _ordered_sections(panels)
    if len(order) <= 1:
        return [dict(p) for p in panels]

    mapping = {name: name for name in order}
    body_total = sum(1 for p in panels if not _is_kpi(p))
    if body_total < SINGLE_TAB_BELOW:
        # Every panel onto the first tab — including any the planner left
        # unsectioned, which would otherwise hang off a tab bar that no longer
        # exists.
        first = order[0]
        return [{**p, "section": first} for p in panels]

    while True:
        live = [name for name in order if mapping[name] == name]
        if len(live) <= 1:
            break
        counts = _body_counts(panels, mapping)
        small = [name for name in live if counts.get(name, 0) < MIN_PANELS_PER_TAB]
        if small:
            victim = min(small, key=lambda n: (counts.get(n, 0), live.index(n)))
        elif len(live) > MAX_TABS:
            victim = min(live, key=lambda n: (counts.get(n, 0), live.index(n)))
        else:
            break
        at = live.index(victim)
        into = live[at - 1] if at > 0 else live[1]
        for name, target in mapping.items():
            if target == victim:
                mapping[name] = into
    return _renamed(panels, mapping)


def settle_new_sections(before: list[dict[str, Any]],
                        after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An edited plan, with any tab the edit INVENTED held to the rules.

    This is how the six-tab dashboard happened: "add a panel about demand" put
    the new chart on a new "Demand" tab of its own, and nothing stopped it.
    A new tab survives only with enough panels to be worth a click, and only if
    it does not push the dashboard past the tab ceiling. Otherwise its panels
    join the tab of the panel they were added next to — the nearest one before
    them, or after them when the new panels open the dashboard.

    Only a tab made up entirely of panels this edit ADDED is checked. A tab that
    holds any panel from before the edit is a request about existing panels —
    renaming a tab, or regrouping charts — and is exactly what was asked for.
    Judging tabs by name alone got that wrong: renaming a three-chart tab looked
    like inventing a small new one, and the whole tab was folded away.
    """
    before_titles = {p.get("title") for p in before if p.get("title")}
    before_sql = {p.get("sql") for p in before if p.get("sql")}

    def was_there(panel: dict[str, Any]) -> bool:
        # Title OR SQL, so a panel the edit retitled or re-queried still counts
        # as one that existed. Only a panel matching neither is new.
        return (panel.get("title") in before_titles
                or (bool(panel.get("sql")) and panel.get("sql") in before_sql))

    existing = set(_ordered_sections(before))
    existing |= {name for p in after if was_there(p) and (name := _section(p))}
    order = _ordered_sections(after)
    new = [name for name in order if name not in existing]
    if not new or not existing:
        return [dict(p) for p in after]

    mapping = {name: name for name in order}
    kept_tabs = len([name for name in order if name in existing])
    counts = _body_counts(after, mapping)
    for name in new:
        if counts.get(name, 0) >= MIN_PANELS_PER_TAB and kept_tabs < MAX_TABS:
            kept_tabs += 1
            existing.add(name)
            continue
        mapping[name] = ""   # resolved per panel below

    out: list[dict[str, Any]] = []
    for i, panel in enumerate(after):
        name = _section(panel)
        if name is None or mapping.get(name, name) == name:
            out.append(dict(panel))
            continue
        home = _neighbour_section(after, i, existing)
        out.append({**panel, "section": home} if home else dict(panel))
    return out


def _neighbour_section(panels: list[dict[str, Any]], at: int,
                       allowed: set[str]) -> str | None:
    """The nearest surviving tab to a panel: looking back first, then forward."""
    for i in range(at - 1, -1, -1):
        name = _section(panels[i])
        if name in allowed:
            return name
    for i in range(at + 1, len(panels)):
        name = _section(panels[i])
        if name in allowed:
            return name
    return None


# --- Telling the assistant ----------------------------------------------------
#
# Without a word about it, the chat assistant compared the tabs it asked for with
# the tabs it got, concluded the planner had ignored the request, and planned the
# whole dashboard again: a second paid planner call that the rules above turn
# into exactly the same result.

_NOT_AN_ERROR = (
    " This is intentional, not a planning error: do not plan or edit again to get "
    "the tabs back. Tell the user in one sentence."
)


def _quoted(names: list[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def consolidation_note(planned: list[dict[str, Any]],
                       consolidated: list[dict[str, Any]]) -> str | None:
    """What consolidate_sections changed about a new plan, or None if nothing.

    `planned` and `consolidated` are the same panels in the same order, as
    consolidate_sections guarantees, so a tab that disappeared can be followed
    to where its panels went.
    """
    asked = _ordered_sections(planned)
    got = _ordered_sections(consolidated)
    if asked == got:
        return None

    charts = sum(1 for panel in consolidated if not _is_kpi(panel))
    if len(got) == 1 and charts < SINGLE_TAB_BELOW:
        return (
            f"Layout rule applied: a dashboard with fewer than {SINGLE_TAB_BELOW} charts "
            f'uses one tab, so all {charts} charts are on the "{got[0]}" tab instead of '
            f"{_quoted(asked)}." + _NOT_AN_ERROR + " If they still want separate tabs, they "
            "can ask for an edit that moves existing charts onto a named tab."
        )

    merged = []
    for name in asked:
        if name in got:
            continue
        into = next((after.get("section") for before, after in zip(planned, consolidated)
                     if _section(before) == name), None)
        merged.append(f'"{name}" into "{into}"')
    return (
        f"Layout rule applied: each tab needs at least {MIN_PANELS_PER_TAB} charts and a "
        f"dashboard has at most {MAX_TABS} tabs, so these tabs were merged: "
        + ", ".join(merged) + "." + _NOT_AN_ERROR
    )


def settlement_note(proposed: list[dict[str, Any]],
                    settled: list[dict[str, Any]]) -> str | None:
    """What settle_new_sections changed about an edit, or None if nothing."""
    moved = [(before, after) for before, after in zip(proposed, settled)
             if _section(before) != _section(after)]
    if not moved:
        return None

    invented: list[str] = []
    homes: list[str] = []
    for before, after in moved:
        if (name := _section(before)) and name not in invented:
            invented.append(name)
        if (home := _section(after)) and home not in homes:
            homes.append(home)
    return (
        f"Layout rule applied: the new tab {_quoted(invented)} would have held fewer than "
        f"{MIN_PANELS_PER_TAB} charts or taken the dashboard past {MAX_TABS} tabs, so the "
        f"added charts were placed on {_quoted(homes)}." + _NOT_AN_ERROR
        + f" A new tab can be created once it will hold at least {MIN_PANELS_PER_TAB} charts."
    )

