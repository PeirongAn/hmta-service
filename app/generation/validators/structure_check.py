"""Structure integrity checks for BT JSON."""

from __future__ import annotations


def check_structure_integrity(bt: dict) -> list[dict]:
    """
    Rules:
    - root_id must exist in nodes.
    - All children references must resolve.
    - Composite nodes must have at least one child.
    - Action/condition nodes must not have children.
    - Decorator nodes must have exactly one child.
    - All nodes must be reachable from the root.
    - The tree must not contain cycles or self-references.
    """
    violations = []
    nodes: dict = bt.get("nodes") or {}
    root_id: str = bt.get("root_id", "")

    if not root_id:
        violations.append({"rule": "missing_root_id", "message": "BT has no root_id"})
        return violations

    if root_id not in nodes:
        violations.append({
            "rule": "root_not_found",
            "message": f"root_id '{root_id}' not found in nodes",
        })
        return violations

    composite_types = {"sequence", "selector", "parallel"}
    leaf_types = {"action", "humanGate", "condition"}
    decorator_types = {"timeout", "retry"}

    for node_id, node in nodes.items():
        children = node.get("children", [])
        ntype = node.get("type", "")

        if node.get("node_id") != node_id:
            violations.append({
                "rule": "node_id_mismatch",
                "node_id": node_id,
                "declared_node_id": node.get("node_id"),
                "message": f"Node key '{node_id}' does not match node_id '{node.get('node_id')}'",
            })

        # Unresolved child references
        for cid in children:
            if cid not in nodes:
                violations.append({
                    "rule": "unresolved_child",
                    "node_id": node_id,
                    "child_id": cid,
                    "message": f"Node '{node_id}' references unknown child '{cid}'",
                })

        # Composite must have children
        if ntype in composite_types and not children:
            violations.append({
                "rule": "empty_composite",
                "node_id": node_id,
                "type": ntype,
                "message": f"Composite node '{node_id}' ({ntype}) has no children",
            })

        # Leaf must not have children
        if ntype in leaf_types and children:
            violations.append({
                "rule": "leaf_has_children",
                "node_id": node_id,
                "type": ntype,
                "message": f"Leaf node '{node_id}' ({ntype}) should not have children",
            })

        if ntype in decorator_types and len(children) != 1:
            violations.append({
                "rule": "invalid_decorator_arity",
                "node_id": node_id,
                "type": ntype,
                "child_count": len(children),
                "message": f"Decorator node '{node_id}' ({ntype}) must have exactly one child",
            })

    visited: set[str] = set()
    visiting: set[str] = set()

    def walk(node_id: str, path: list[str]) -> None:
        if node_id in visiting:
            cycle_path = [*path, node_id]
            violations.append({
                "rule": "cycle_detected",
                "node_id": node_id,
                "path": cycle_path,
                "message": f"Cycle detected in BT path: {' -> '.join(cycle_path)}",
            })
            return

        if node_id in visited or node_id not in nodes:
            return

        visited.add(node_id)
        visiting.add(node_id)
        node = nodes[node_id]
        for child_id in node.get("children", []):
            if child_id == node_id:
                violations.append({
                    "rule": "self_loop",
                    "node_id": node_id,
                    "message": f"Node '{node_id}' must not reference itself as a child",
                })
                continue
            walk(child_id, [*path, node_id])
        visiting.remove(node_id)

    walk(root_id, [])

    for node_id in nodes:
        if node_id not in visited:
            violations.append({
                "rule": "unreachable_node",
                "node_id": node_id,
                "message": f"Node '{node_id}' is not reachable from root '{root_id}'",
            })

    return violations
