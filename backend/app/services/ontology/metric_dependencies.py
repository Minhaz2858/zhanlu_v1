"""D3 — governed metric dependency validation.

Explicit metric-to-metric dependencies (``MetricDefinition.depends_on_metrics``)
must be represented in the ontology graph. This module enforces the acceptance
constraints at WRITE time and builds the dependency edges for the graph:

- references resolve to existing metrics in the SAME org (no cross-org leakage);
- a metric may not depend on itself;
- circular dependencies are REJECTED at write time (never silently accepted);
- version consistency: a dependency must be ``published`` and enabled (a draft/
  retired/disabled metric is not a valid dependency).

At graph-build time we also run cycle detection defensively and DROP any edge
that would close a loop, surfacing it rather than hiding it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _resolve_ref(db: Session, org_id: str, ref: str) -> Any | None:
    from app.models.metric_definition import MetricDefinition

    ref = (ref or "").strip()
    if not ref:
        return None
    by_id = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.id == ref,
            MetricDefinition.org_id == org_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if by_id is not None:
        return by_id
    rows = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.org_id == org_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    ref_l = ref.lower()
    for r in rows:
        names = [str(r.name or "").lower()] + [
            str(s or "").lower() for s in (r.synonyms or [])
        ]
        if ref_l in names:
            return r
    return None


def validate_metric_dependencies(
    db: Session, org_id: str, metric_id: str, depends_on: list | None
) -> tuple[bool, list[str]]:
    """Validate a proposed ``depends_on_metrics`` list for ``metric_id``.

    Returns (ok, errors). On failure the caller should refuse the write.
    """
    errors: list[str] = []
    if not depends_on:
        return True, errors

    if not isinstance(depends_on, list):
        return False, ["depends_on_metrics must be a list"]

    resolved_ids: list[str] = []
    seen = set()
    for ref in depends_on:
        ref = str(ref or "").strip()
        if not ref:
            errors.append("empty dependency reference")
            continue
        dep = _resolve_ref(db, org_id, ref)
        if dep is None:
            errors.append(f"dependency {ref!r} does not resolve in this org")
            continue
        if dep.id == metric_id:
            errors.append("a metric may not depend on itself")
            continue
        if dep.id in seen:
            errors.append(f"duplicate dependency {ref!r}")
            continue
        seen.add(dep.id)
        # version consistency — a dependency must be published + enabled
        if (dep.lifecycle or "published") != "published":
            errors.append(
                f"dependency {dep.name!r} is {dep.lifecycle}, not published")
        if not dep.is_enabled:
            errors.append(f"dependency {dep.name!r} is disabled")
        resolved_ids.append(dep.id)

    if errors:
        return False, errors

    # Cycle detection: does adding these edges create a loop?
    cycle = _introduces_cycle(db, org_id, metric_id, resolved_ids)
    if cycle:
        errors.append(
            "circular dependency detected: " + " -> ".join(cycle))
        return False, errors

    return True, errors


def _dep_map(db: Session, org_id: str) -> dict[str, list[str]]:
    from app.models.metric_definition import MetricDefinition

    out: dict[str, list[str]] = {}
    rows = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.org_id == org_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for r in rows:
        deps = r.depends_on_metrics or []
        out[r.id] = [str(d) for d in deps]
    return out


def _introduces_cycle(
    db: Session, org_id: str, metric_id: str, new_deps: list[str]
) -> list[str] | None:
    """Return a cycle path (list of ids) if adding new_deps to metric_id
    creates a loop, else None."""
    graph = _dep_map(db, org_id)
    # Apply the proposed edge.
    graph = {k: list(v) for k, v in graph.items()}
    graph.setdefault(metric_id, [])
    graph[metric_id].extend(new_deps)

    # DFS from metric_id; if we can return to metric_id, it's a cycle.
    path: list[str] = []
    visited_in_path = set()

    def dfs(node: str) -> list[str] | None:
        path.append(node)
        visited_in_path.add(node)
        for nxt in graph.get(node, []):
            if nxt == metric_id:
                return list(path) + [metric_id]
            if nxt not in visited_in_path:
                res = dfs(nxt)
                if res:
                    return res
        path.pop()
        visited_in_path.discard(node)
        return None

    return dfs(metric_id)


def build_dependency_edges(db: Session, org_id: str) -> list[dict]:
    """Edges for the graph: (from = dependent, to = dependency).

    Cycles are DROPPED defensively (we surface them via logging, never hide a
    loop in the visualization). All edges are org-scoped.
    """
    graph = _dep_map(db, org_id)
    edges: list[dict] = []
    cyclic = set()

    for src, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                # dangling dependency — skip (resolution should have caught it)
                continue
            edges.append({"from_metric_id": src, "to_metric_id": dep})

    # Detect any remaining cycle (e.g. seeded bypassing validation) and drop it.
    seen_loops = _find_cycles(graph)
    if seen_loops:
        logger.warning(
            "metric_dependencies: dropping %d cyclic edge(s): %s",
            len(seen_loops), seen_loops)
        cyclic.update(seen_loops)
    return [e for e in edges if (e["from_metric_id"], e["to_metric_id"]) not in cyclic]


def _find_cycles(graph: dict[str, list[str]]) -> set[tuple[str, str]]:
    loops: set[tuple[str, str]] = set()
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(node: str, stack: list[str]):
        state[node] = 0
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 0:
                # found a back-edge -> record the loop edges
                idx = stack.index(nxt)
                for a, b in zip(stack[idx:], stack[idx + 1:] + [nxt]):
                    loops.add((a, b))
            elif nxt not in state:
                visit(nxt, stack)
        stack.pop()
        state[node] = 1

    for n in list(graph.keys()):
        if n not in state:
            visit(n, [])
    return loops
