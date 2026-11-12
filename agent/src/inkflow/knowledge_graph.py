"""Graphify 仅接收已分层的知识投影，不探测密钥、不自行调用模型。"""
from __future__ import annotations

from heapq import nlargest
from importlib.metadata import version
from math import ceil, cos, log1p, pi, sin, sqrt


def _orbital_slots(size: int) -> tuple[list[dict], dict[int, tuple[float, float, float]]]:
    """轨道沿长度分配位置，按少量不同倾角展开，避免所有节点挤在同一壳层。"""
    count = min(size, 12, max(3, ceil(sqrt(size) / 3)))
    radii = [.25 + .75 * (index + 1) / count for index in range(count)]
    tracks, slots = [], {}
    previous = cumulative = 0
    for index, radius in enumerate(radii):
        tilt = -.18 + .47 * (index % 4) / 3
        minor = radius * (.83 + .08 * (index % 3))
        tracks.append({"radius": radius, "minor": minor, "tilt": tilt})
        cumulative += radius
        target = size if index == count - 1 else round(size * cumulative / sum(radii))
        for step in range(target - previous):
            angle = 2 * pi * step / (target - previous) + index * .61
            depth = minor * sin(angle)
            slots[len(slots)] = (radius * cos(angle), depth * sin(tilt), depth * cos(tilt))
        previous = target
    return tracks, slots


def knowledge_graph(snapshot: dict, *, focus: str = "", kind: str = "", limit: int | None = 50) -> dict:
    """生成离线图布局；业务边仍用墨流原始多关系，避免简单图合并边丢语义。"""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("图中对象上限必须为正整数，或选择不限数量。")
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    if focus and focus not in nodes:
        raise ValueError("所选知识对象已变化，请刷新后重新选择。")
    if kind:
        nodes = {key: node for key, node in nodes.items() if node["kind"] == kind or key == focus}
    edges = [edge for edge in snapshot["edges"] if edge["source"] in nodes and edge["target"] in nodes]
    if focus:
        neighbors = {focus}
        neighbors.update(edge[key] for edge in edges if focus in {edge["source"], edge["target"]} for key in ("source", "target"))
        nodes = {key: node for key, node in nodes.items() if key in neighbors}
    total = len(nodes)
    sources = {source["id"]: source for source in snapshot.get("sources", [])}
    stats = {}
    for key, node in nodes.items():
        # 同一来源同一句只计一次。推断、留档和已被替代的条目不放大证据权重。
        sections = [section for section in node.get("sections", []) if section["state"] != "dismissed"
                    and section.get("current") is not False]
        evidence = {(section["source_id"], section["quote"].strip()) for section in sections
                    if section["state"] not in {"unverified", "conflict", "hypothesis"} and section.get("quote", "").strip()}
        for section in sections:
            if not section.get("record_id"):
                continue
            for ref in sources.get(section["source_id"], {}).get("references", []):
                if ref.get("source_current") and ref.get("quote", "").strip():
                    origin = (f"chapter:{ref['chapter_no']}" if ref.get("chapter_no") and ref.get("source_kind") == "canonical"
                              else ref.get("path") or ref.get("artifact_id") or section["source_id"])
                    evidence.add((origin, ref["quote"].strip()))
        information = len(sections)
        weight = log1p(information) + 1.45 * log1p(len(evidence))
        stats[key] = (information, len(evidence), weight)
    ordered = sorted(nodes, key=lambda key: (key != focus, -stats[key][2], -len(nodes[key]["sources"]), nodes[key]["name"], key))
    ordered = ordered[:limit]
    selected = set(ordered)
    edges = [edge for edge in edges if edge["source"] in selected and edge["target"] in selected]
    from graphify.build import build_from_json
    import networkx as nx

    graph = build_from_json({"nodes": [{"id": key, "label": nodes[key]["name"], "file_type": "concept",
        "source_file": "knowledge", "source_location": None} for key in ordered],
        "edges": [{"source": edge["source"], "target": edge["target"], "relation": edge["relation"],
            "source_file": edge["source_id"],
            "confidence": "EXTRACTED" if edge["state"] == "canon" else "AMBIGUOUS" if edge["state"] in {"conflict", "unverified"} else "INFERRED"}
                  for edge in edges]}, directed=True).to_undirected()
    graph.remove_edges_from(list(nx.selfloop_edges(graph)))
    hub = focus or max(ordered, key=lambda key: (graph.degree(key), stats[key][2]), default="")
    satellites = graph.subgraph([key for key in ordered if key != hub])
    connected = satellites.subgraph([key for key in satellites if satellites.degree(key)])
    # 依据实际关系划分邻近对象，类型只负责颜色和目录筛选。
    groups = sorted((sorted(group) for group in nx.community.louvain_communities(connected, weight=None, seed=17)),
                    key=lambda group: (-len(group), group[0])) if connected else []
    labels = {}
    layout = {hub: (0, 0, 0)} if hub else {}
    tracks, slots = _orbital_slots(max(0, len(ordered) - 1))
    for index, group in enumerate(groups):
        center = max(group, key=lambda member: (graph.degree(member), stats[member][2], member))
        labels.update(dict.fromkeys(group, nodes[center]["name"]))
        angle = index * pi * (3 - sqrt(5))
        seed_x, seed_z = .65 * cos(angle), .65 * sin(angle)
        nearest = nlargest(len(group), slots, key=lambda key: -(
            (slots[key][0] - seed_x) ** 2 + slots[key][1] ** 2 + (slots[key][2] - seed_z) ** 2))
        area = {key: slots.pop(key) for key in nearest}
        layout[center] = area.pop(nearest[0])
        # 从群内连边展开，同一对象的关联沿相邻球面位置连续排布。
        for parent, child in nx.bfs_edges(graph.subgraph(group), center, sort_neighbors=sorted):
            x, y, z = layout[parent]
            closest = min(area, key=lambda key: (area[key][0] - x) ** 2 + (area[key][1] - y) ** 2 + (area[key][2] - z) ** 2)
            layout[child] = area.pop(closest)
        for member, point in zip((key for key in group if key not in layout), area.values()):
            layout[member] = point
    for member, point in zip((key for key in ordered if key not in layout), slots.values()):
        layout[member] = point
    max_weight = max((item[2] for item in stats.values()), default=0) or 1
    return {"engine": "Graphify", "version": version("graphifyy"), "total": total, "shown": len(selected),
        "center": hub, "tracks": tracks,
        "nodes": [{"id": key, "name": nodes[key]["name"], "kind": nodes[key]["kind"],
                   "information": stats[key][0], "evidence": stats[key][1], "size": stats[key][2] / max_weight,
                   "group": labels.get(key), "x": float(layout[key][0]), "y": float(layout[key][1]),
                   "z": float(layout[key][2])} for key in ordered], "edges": edges}
