#!/usr/bin/env python3
"""Simulate the frontend buildElements + applyFamilyLayout filtering logic.

Usage (from inside the backend container or with API accessible):
    python scripts/test_graph_filtering.py Q317521
    python scripts/test_graph_filtering.py Q317521 --api http://localhost:8000

Prints which nodes are removed by each cap and which spouse nodes survive.
"""
import json
import sys
import argparse
from collections import deque, defaultdict

try:
    import requests
except ImportError:
    import urllib.request
    class requests:  # noqa: E302
        @staticmethod
        def get(url):
            with urllib.request.urlopen(url) as r:
                class R:
                    def json(self_):
                        return json.loads(r.read())
                return R()


FAMILY_EDGE_TYPES = {'parent', 'father', 'mother', 'child', 'sibling', 'spouse', 'relative'}
FAMILY_ROW_CAP = 16
ORG_MEMBER_CAP = 20
ORG_MEMBER_CAP_FAMILY = 3
FOCAL_ORG_CAP = 12
FOCAL_PERSON_CAP = 40


def simulate(qid: str, api_base: str = "http://localhost:8000"):
    url = f"{api_base}/api/graph/neighborhood/{qid}"
    print(f"Fetching {url} …")
    data = requests.get(url).json()
    vertices = [v for v in data.get("vertices", []) if v]
    edges = [e for e in data.get("edges", []) if e]

    if not vertices:
        print("No data returned (QID not found?).")
        return

    # Build node metadata
    nodes = {}
    names = {}
    for v in vertices:
        key = v.get("_key") or v.get("_id", "").split("/")[-1]
        vid = v.get("_id", "")
        etype = "organization" if vid.startswith("organizations/") else (
            "event" if vid.startswith("events/") else "person")
        sc = v.get("wikidata_statements_count") or 0
        nodes[key] = {"entityType": etype, "sc": sc}
        names[key] = v.get("name_en") or v.get("label_en") or key

    removed = set()
    reasons = {}

    def mark_removed(key, reason):
        if key not in removed:
            removed.add(key)
            reasons[key] = reason

    # 1. Org member cap (family mode uses cap=3)
    org_conn = {}
    for e in edges:
        fk = (e.get("_from") or "").split("/")[-1]
        tk = (e.get("_to") or "").split("/")[-1]
        for k, other in [(fk, tk), (tk, fk)]:
            if k in nodes and nodes[k]["entityType"] == "organization":
                org_conn.setdefault(k, set()).add(other)

    cap = ORG_MEMBER_CAP_FAMILY
    for org_key, conn in org_conn.items():
        if len(conn) <= cap:
            continue
        sorted_conn = sorted(conn, key=lambda k: -(nodes.get(k, {}).get("sc", 0)))
        for pk in sorted_conn[cap:]:
            if pk == qid:
                continue
            can_remove = not any(
                ((e.get("_from") or "").split("/")[-1] == pk and (e.get("_to") or "").split("/")[-1] == qid) or
                ((e.get("_to") or "").split("/")[-1] == pk and (e.get("_from") or "").split("/")[-1] == qid)
                for e in edges
            )
            if can_remove:
                mark_removed(pk, f"org-member-cap({org_key})")

    # 2. Secondary org suppression
    focal_direct_orgs = set()
    for e in edges:
        fk = (e.get("_from") or "").split("/")[-1]
        tk = (e.get("_to") or "").split("/")[-1]
        nk = tk if fk == qid else (fk if tk == qid else None)
        nid = (e.get("_to") or "") if fk == qid else ((e.get("_from") or "") if tk == qid else "")
        if nk and nid.startswith("organizations/"):
            focal_direct_orgs.add(nk)

    print(f"\n--- buildElements caps (family mode, orgMemberCap={cap}) ---")
    print(f"  focal direct orgs: {len(focal_direct_orgs)} (secondary orgs removed if >= {cap})")

    if len(focal_direct_orgs) >= cap:
        for key, info in nodes.items():
            if info["entityType"] != "organization":
                continue
            if key in focal_direct_orgs:
                continue
            mark_removed(key, "secondary-org-suppression")

    # 3. FOCAL_ORG_CAP and FOCAL_PERSON_CAP
    focal_neighbors = {"person": [], "other": []}
    for e in edges:
        fk = (e.get("_from") or "").split("/")[-1]
        tk = (e.get("_to") or "").split("/")[-1]
        nk = tk if fk == qid else (fk if tk == qid else None)
        nid = (e.get("_to") or "") if fk == qid else ((e.get("_from") or "") if tk == qid else "")
        if not nk or nk in removed or nk not in nodes:
            continue
        bucket = "person" if nid.startswith("persons/") else "other"
        focal_neighbors[bucket].append({"key": nk, "sc": nodes[nk]["sc"]})

    seen_o = set()
    unique_other = [x for x in focal_neighbors["other"]
                    if not (x["key"] in seen_o or seen_o.add(x["key"]))]
    if len(unique_other) > FOCAL_ORG_CAP:
        unique_other.sort(key=lambda x: -x["sc"])
        for x in unique_other[FOCAL_ORG_CAP:]:
            mark_removed(x["key"], "FOCAL_ORG_CAP")
    print(f"  unique org neighbors: {len(unique_other)} (cap={FOCAL_ORG_CAP})")

    seen_p = set()
    unique_persons = [x for x in focal_neighbors["person"]
                      if not (x["key"] in seen_p or seen_p.add(x["key"]))]
    if len(unique_persons) > FOCAL_PERSON_CAP:
        unique_persons.sort(key=lambda x: -x["sc"])
        for x in unique_persons[FOCAL_PERSON_CAP:]:
            mark_removed(x["key"], "FOCAL_PERSON_CAP")
    print(f"  unique person neighbors: {len(unique_persons)} (cap={FOCAL_PERSON_CAP})")

    print(f"  total removed: {len(removed)}")

    # 4. Family layout: computeGenerations + FAMILY_ROW_CAP
    gen = {qid: 0}
    queue = deque([qid])
    while queue:
        current = queue.popleft()
        for e in edges:
            if e.get("type") not in FAMILY_EDGE_TYPES:
                continue
            fk = (e.get("_from") or "").split("/")[-1]
            tk = (e.get("_to") or "").split("/")[-1]
            t = e.get("type")
            if t in ("parent", "father", "mother"):
                if fk == current and tk not in gen:
                    gen[tk] = gen[current] - 1
                    queue.append(tk)
                if tk == current and fk not in gen:
                    gen[fk] = gen[current] + 1
                    queue.append(fk)
            elif t == "child":
                if fk == current and tk not in gen:
                    gen[tk] = gen[current] + 1
                    queue.append(tk)
                if tk == current and fk not in gen:
                    gen[fk] = gen[current] - 1
                    queue.append(fk)
            elif t in ("sibling", "spouse", "relative"):
                if fk == current and tk not in gen:
                    gen[tk] = gen[current]
                    queue.append(tk)
                if tk == current and fk not in gen:
                    gen[fk] = gen[current]
                    queue.append(fk)

    by_gen = defaultdict(list)
    for k, g in gen.items():
        by_gen[g].append(k)

    print("\n--- applyFamilyLayout generations ---")
    layout_hidden = set()
    for g in sorted(by_gen.keys()):
        keys = by_gen[g]
        mark = "  **CAPPED**" if len(keys) > FAMILY_ROW_CAP else ""
        print(f"  gen {g}: {len(keys)} nodes{mark}")
        if len(keys) > FAMILY_ROW_CAP:
            kept = {qid}
            rest = sorted([k for k in keys if k != qid],
                          key=lambda k: -(nodes.get(k, {}).get("sc", 0)))
            kept.update(rest[:FAMILY_ROW_CAP - 1])
            for k in keys:
                if k not in kept:
                    layout_hidden.add(k)
                    reasons[k] = reasons.get(k, f"FAMILY_ROW_CAP(gen{g})")

    # Spouse analysis
    spouse_pairs = []
    seen_sp = set()
    for e in edges:
        if e.get("type") != "spouse":
            continue
        fk = (e.get("_from") or "").split("/")[-1]
        tk = (e.get("_to") or "").split("/")[-1]
        pair = tuple(sorted([fk, tk]))
        if pair not in seen_sp:
            seen_sp.add(pair)
            spouse_pairs.append((fk, tk))

    spouse_nodes = set()
    for fk, tk in spouse_pairs:
        spouse_nodes.add(fk)
        spouse_nodes.add(tk)

    print(f"\n--- Spouse analysis ---")
    print(f"  unique spouse pairs (after dedup): {len(spouse_pairs)}")
    print(f"  unique nodes in spouse edges: {len(spouse_nodes)}")
    for sp in sorted(spouse_nodes):
        g = gen.get(sp, "unreachable")
        status = []
        if sp in removed:
            status.append(f"REMOVED by {reasons[sp]}")
        if sp in layout_hidden:
            status.append(f"LAYOUT_HIDDEN by {reasons.get(sp,'?')}")
        status_str = ", ".join(status) if status else "visible"
        direct = any(
            ((e.get("_from") or "").split("/")[-1] == qid and (e.get("_to") or "").split("/")[-1] == sp) or
            ((e.get("_to") or "").split("/")[-1] == qid and (e.get("_from") or "").split("/")[-1] == sp)
            for e in edges if e.get("type") == "spouse"
        )
        print(f"  {sp} ({names.get(sp,'?')}): gen={g}, direct_spouse={direct}, {status_str}")

    print(f"\n--- Edge type summary ---")
    etypes = defaultdict(int)
    for e in edges:
        etypes[e.get("type", "?")] += 1
    for t, cnt in sorted(etypes.items(), key=lambda x: -x[1]):
        print(f"  {t}: {cnt}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate frontend graph filtering for a QID")
    parser.add_argument("qid", help="Wikidata QID e.g. Q317521")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()
    simulate(args.qid, args.api)
