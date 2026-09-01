from __future__ import annotations

from harness.orchestration_contracts import DagNode, parse_dag_nodes, validate_task_dag


def test_valid_linear_dag_is_empty():
    nodes = parse_dag_nodes([
        {"id": "explore", "depends_on": []},
        {"id": "audit", "depends_on": ["explore"]},
    ])
    assert validate_task_dag(nodes) == ()


def test_unknown_and_duplicate_and_empty_ids():
    nodes = parse_dag_nodes([
        {"id": "a", "depends_on": ["missing"]},
        {"id": "a"},
        {"id": "  "},
        "nope",
    ])
    codes = {v.code for v in validate_task_dag(nodes)}
    assert codes == {"unknown_dependency", "duplicate_id", "empty_id"}


def test_self_edge_and_two_node_cycle():
    self_edge = validate_task_dag((DagNode("loop", ("loop",)),))
    assert self_edge[0].code == "cycle"
    cycle = validate_task_dag((
        DagNode("a", ("b",)),
        DagNode("b", ("a",)),
    ))
    assert {v.code for v in cycle} == {"cycle"}
