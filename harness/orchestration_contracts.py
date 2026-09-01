"""Typed control-graph contracts for Marionette-owned task DAGs.

Puppetmaster already stores job graphs. This module is the fail-closed
boundary Marionette uses before submitting depends_on maps: unknown ids,
duplicates, and cycles are violations, not runtime surprises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple


@dataclass(frozen=True)
class DagNode:
    id: str
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DagViolation:
    code: str
    node_id: str
    detail: str


def parse_dag_nodes(raw: Iterable[object]) -> Tuple[DagNode, ...]:
    """Parse a list of {id, depends_on} maps. Invalid rows become empty-id nodes."""
    nodes: List[DagNode] = []
    for item in raw:
        if not isinstance(item, dict):
            nodes.append(DagNode(id=""))
            continue
        node_id = item.get("id")
        deps = item.get("depends_on") or ()
        if not isinstance(node_id, str):
            node_id = ""
        if isinstance(deps, str):
            dep_tuple = (deps,) if deps else ()
        elif isinstance(deps, (list, tuple)):
            dep_tuple = tuple(d for d in deps if isinstance(d, str) and d)
        else:
            dep_tuple = ()
        nodes.append(DagNode(id=node_id.strip(), depends_on=dep_tuple))
    return tuple(nodes)


def validate_task_dag(nodes: Sequence[DagNode]) -> Tuple[DagViolation, ...]:
    """Return every structural violation. Empty result means the graph may run."""
    violations: List[DagViolation] = []
    seen: Set[str] = set()
    ids: Set[str] = set()
    for node in nodes:
        if not node.id:
            violations.append(DagViolation("empty_id", "", "task id is required"))
            continue
        if node.id in seen:
            violations.append(DagViolation("duplicate_id", node.id, "task id repeats"))
            continue
        seen.add(node.id)
        ids.add(node.id)
    known = {node.id for node in nodes if node.id}
    for node in nodes:
        if not node.id:
            continue
        for dep in node.depends_on:
            if dep == node.id:
                violations.append(DagViolation("cycle", node.id, "task depends on itself"))
            elif dep not in known:
                violations.append(
                    DagViolation("unknown_dependency", node.id, "depends_on %r is not a task" % (dep,))
                )
    violations.extend(_cycle_violations(nodes, known))
    return tuple(violations)


def _cycle_violations(nodes: Sequence[DagNode], known: Set[str]) -> Tuple[DagViolation, ...]:
    graph: Dict[str, List[str]] = {node.id: [] for node in nodes if node.id}
    for node in nodes:
        if not node.id:
            continue
        graph[node.id] = [dep for dep in node.depends_on if dep in known and dep != node.id]
    visiting: Set[str] = set()
    visited: Set[str] = set()
    cyclic: List[str] = []

    def visit(node_id: str) -> None:
        if node_id in visited or node_id in cyclic:
            return
        if node_id in visiting:
            cyclic.append(node_id)
            return
        visiting.add(node_id)
        for nxt in graph.get(node_id, ()):
            visit(nxt)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in graph:
        visit(node_id)
    return tuple(
        DagViolation("cycle", node_id, "depends_on cycle")
        for node_id in cyclic
    )
