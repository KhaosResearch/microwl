"""Mutation analysis at the level of the descriptor files (RQ2).

Every mutant is a modified copy of the original descriptors (Kubernetes manifests, Compose files
or CloudFormation templates). The mutated descriptors are translated again and validated, so the
whole pipeline (parsing, translation, reasoning) is exercised. The whole mutated unit (group, file or
template) is translated; for validation, each mutant keeps the elements that its targeted constraint
relates: the mutated elements and, for M8, M9, M10, M12 and M13, the endpoints of the unit. One fault is injected into every applicable element on which the
targeted constraint is not already reported in the unmodified corpus:

  M1  Kubernetes: replicas = 3 and the pod mounts a ReadWriteOnce claim (the access mode of an
      existing claim of the group is changed, or a claim and a volume are added)        -> S1
  M2  target port of a selecting Kubernetes Service, container port of the load balancer of an
      ECS service, or container port of a Compose port mapping = container port + 1     -> S2c
  M3  Kubernetes: a selecting Service becomes NodePort with nodePort 8080                -> S3
  M4  Kubernetes: same with nodePort 40000                                               -> S3
  M5  Kubernetes: replicas = 0 and the same storage change as M1                         -> S5
  M7  Kubernetes: replicas = 3 and hostPort on the first container port; Compose: deploy.replicas
      = 3 for a service that publishes a host port; ECS (bridge mode): fixed HostPort and
      DesiredCount = 3                                                                   -> S7
  M8  Kubernetes: two Services of the group become NodePort with the same nodePort; Compose: a
      second service publishes the host port of the first one                           -> S8
  M9  the port of a dependency declared in an environment variable is changed to a port that its
      endpoint does not serve                                                            -> S9
  M10 the host of such a dependency is misspelled                                        -> S10
  M11 the image tag is removed and the replicas are set to 2 when lower                  -> S11
  M12 Kubernetes: a selector value of a selecting Service is changed; ECS: the container name of a
      load balancer is changed                                                           -> S12
  M13 Kubernetes: the port of an Ingress backend is changed                              -> S13
  M14 Kubernetes: a probe checks a port that is not a container port                     -> S14
  M15 Kubernetes: as M1 with a ReadWriteOncePod claim                                    -> S15
  M16 the memory limits of the workload are removed (ECS: EC2 launch type only, a memory
      reservation is kept so that the template stays valid)                              -> S16

A mutant is detected by an engine when the engine reports the targeted constraint on the mutated
element (rule attribution: violatesConstraint for Pellet, the constraint identifier in the SHACL
message, the Rego violation tuples). M2 also counts the open-world S2.

Usage: python mutate_files.py <corpora_dir>
"""
import copy
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from microwl_lib import (PelletValidator, pellet_rules_chunked, chunks_by_unit, shacl_rules_chunked, hybrid_rules_chunked, opa_rules, safe, ROOT, REGO, SHAPES,
                         SHAPES_CLOSED, SHAPES_HYBRID)
from translate import (load_k8s_docs, is_templated, k8s_group_to_configs, compose_to_configs, load_cfn,
                       ecs_cfn_doc_to_configs, _resolve, UNRESOLVED)
from real_world import CORPORA, REPOS, ECS_CFN, cfn_files, app_k8s_units, app_compose_units

RELATIONAL = {"M8", "M9", "M10", "M12", "M13"}     # categories whose constraint relates elements of a unit
STORAGE_EDITS = defaultdict(int)      # how M1/M5/M15 obtained their claim
TARGET = {"M1": {"S1"}, "M2": {"S2", "S2c"}, "M3": {"S3"}, "M4": {"S3"}, "M5": {"S5"}, "M7": {"S7"},
          "M8": {"S8"}, "M9": {"S9"}, "M10": {"S10"}, "M11": {"S11"}, "M12": {"S12"}, "M13": {"S13"},
          "M14": {"S14"}, "M15": {"S15"}, "M16": {"S16"}}


# ----------------------------------------------------------------------------- helpers
def dedupe(cfgs):
    """Make identifiers unique as real_world.load() does (k-th duplicate -> id~k)."""
    seen = {}
    for c in cfgs:
        k = safe(c.id)
        if k in seen:
            seen[k] += 1
            c.id = f"{c.id}~{seen[k]}"
        else:
            seen[k] = 0
    return cfgs


def isolate(cfgs, key):
    """Give the elements of one mutant unique identifiers and their own deployment unit."""
    for c in cfgs:
        c.id = f"{c.id}#{key}"
        c.deployment = f"{c.deployment}#{key}"
        c.routes_to = [f"{r}#{key}" for r in c.routes_to]
        c.expected = key.rsplit("#", 1)[-1]
    return cfgs


def ns(d):
    return (d.get("metadata") or {}).get("namespace", "default")


def bump(port, used):
    p = port + 1
    while p in used:
        p += 1
    return p


def set_env(container_env, key, fn):
    """Apply fn to the literal value of environment variable `key` (Kubernetes list or Compose
    dict/list form). Returns True if a value was changed."""
    if isinstance(container_env, dict):
        if isinstance(container_env.get(key), str):
            container_env[key] = fn(container_env[key])
            return True
        return False
    for i, e in enumerate(container_env or []):
        if isinstance(e, dict) and e.get("name") == key and isinstance(e.get("value"), str):
            e["value"] = fn(e["value"])
            return True
        if isinstance(e, str) and e.split("=", 1)[0] == key and "=" in e:
            container_env[i] = f"{key}={fn(e.split('=', 1)[1])}"
            return True
    return False


def env_items(env):
    if isinstance(env, dict):
        return dict(env)
    out = {}
    for e in env or []:
        if isinstance(e, dict) and isinstance(e.get("value"), str):
            out[e.get("name")] = e["value"]
        elif isinstance(e, str) and "=" in e:
            k, v = e.split("=", 1)
            out[k] = v
    return out


def change_dep_port(env, key, old, new):
    """Change the port of the dependency declared in `key`: in the value itself (host:port, URL)
    or in the sibling variable <prefix>_PORT."""
    value = env_items(env).get(key, "")
    if re.search(rf":{old}(?=$|[/?#,])", value):
        return set_env(env, key, lambda v: re.sub(rf":{old}(?=$|[/?#,])", f":{new}", v, count=1))
    prefix = re.sub(r"_?(HOST|HOSTNAME|ADDR|ADDRESS|SERVER|SERVICE|ENDPOINT)$", "", key, flags=re.I)
    return set_env(env, f"{prefix}_PORT", lambda v: str(new) if v == str(old) else v)


def misspell(value, host):
    first = host.split(".")[0]
    return re.sub(rf"(?<![A-Za-z0-9-]){re.escape(first)}(?![A-Za-z0-9-])", first + "x", value, count=1)


def untag(image):
    if "@" in image:
        image = image.split("@", 1)[0]
    head, _, last = image.rpartition("/")
    return (head + "/" if head else "") + last.split(":", 1)[0]


# ----------------------------------------------------------------------------- Kubernetes
def k8s_groups():
    """(source, docs, incomplete) for every Kubernetes group, grouped as in real_world.load()."""
    out = []
    specs = [(REPOS["online-boutique"], ["release/kubernetes-manifests.yaml"]),
             (REPOS["sock-shop"], ["deploy/kubernetes/complete-demo.yaml"]),
             (REPOS["k8s-examples"], None)]
    for repo, rel in specs:
        r = CORPORA / repo
        files = [r / f for f in rel] if rel else sorted(
            p for p in r.rglob("*") if p.suffix in (".yaml", ".yml") and ".git" not in p.parts)
        groups = defaultdict(list)
        for f in files:
            for d in load_k8s_docs(f):
                groups[f.parent].append(d)
        incomplete = {f.parent for f in files if is_templated(f)}
        out += [(str(directory.relative_to(r)), docs, directory in incomplete)
                for directory, docs in sorted(groups.items())]
    return out + app_k8s_units()


def find_workload(docs, cid, k=0):
    kind, name = cid.split("::", 1)[1].split("~")[0].split("/", 1)
    matches = [d for d in docs if d.get("kind") == kind and (d.get("metadata") or {}).get("name") == name]
    return matches[k]


def effective_services(docs):
    """Services of a group; a Service defined twice is one object, and the last definition wins
    (as with kubectl apply and in the translator)."""
    last = {}
    for d in docs:
        if d.get("kind") == "Service":
            last[((d.get("metadata") or {}).get("name"), ns(d))] = d
    return [d for d in docs if d.get("kind") == "Service" and any(d is x for x in last.values())]


def selecting_services(docs, wl):
    labels = ((wl.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {}
    out = []
    for s in effective_services(docs):
        if s.get("kind") != "Service":
            continue
        sel = (s.get("spec") or {}).get("selector") or {}
        if sel and ns(s) == ns(wl) and all(labels.get(k) == v for k, v in sel.items()) \
                and (s.get("spec") or {}).get("ports"):
            out.append(s)
    return out


def add_claim(docs, wl, pspec, mode):
    pvcs = {((d.get("metadata") or {}).get("name"), ns(d)): d for d in docs if d.get("kind") == "PersistentVolumeClaim"}
    claims = [(v.get("persistentVolumeClaim") or {}).get("claimName") for v in pspec.get("volumes") or []]
    found = [pvcs[(c, ns(wl))] for c in claims if (c, ns(wl)) in pvcs]
    if found:
        found[0].setdefault("spec", {})["accessModes"] = [mode]
        STORAGE_EDITS["existing claim"] += 1
        return
    STORAGE_EDITS["added claim"] += 1
    pspec.setdefault("volumes", []).append({"name": "mutation-data", "persistentVolumeClaim": {"claimName": "mutation-pvc"}})
    pspec["containers"][0].setdefault("volumeMounts", []).append({"name": "mutation-data", "mountPath": "/mutation-data"})
    meta = {"name": "mutation-pvc"}
    if "namespace" in (wl.get("metadata") or {}):
        meta["namespace"] = wl["metadata"]["namespace"]
    docs.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta,
                 "spec": {"accessModes": [mode], "resources": {"requests": {"storage": "1Gi"}}}})


def k8s_mutants(source, docs, cfgs, before):
    """Yield (category, target element ids, mutated docs) for one Kubernetes group."""
    occurrence = defaultdict(int)
    endpoints = {c.id: c for c in cfgs if c.kind == "endpoint"}
    for c in cfgs:
        if c.kind != "workload":
            continue
        base_id = c.id.split("~")[0]
        k = occurrence[base_id]
        occurrence[base_id] += 1
        done = before.get(c.id, set())
        svc_ports = [p for p in c.ports if p.target is not None or p.node_port is not None]

        def mutant(cat, edit, targets=None):
            if TARGET[cat] & done and targets is None:
                return None
            m = copy.deepcopy(docs)
            wl = find_workload(m, c.id, k)
            if edit(m, wl) is False:
                return None
            return cat, targets or [c.id], m

        def m_storage(replicas, mode):
            def edit(m, wl):
                wl.setdefault("spec", {})["replicas"] = replicas
                add_claim(m, wl, wl["spec"].setdefault("template", {}).setdefault("spec", {}), mode)
            return edit

        def m_service(fn):
            def edit(m, wl):
                fn(selecting_services(m, wl)[0])
            return edit

        def m2(svc):
            svc["spec"]["ports"][0]["targetPort"] = c.container_ports[0] + 1

        def nodeport(value):
            def fn(svc):
                if svc["spec"].get("type") not in ("NodePort", "LoadBalancer"):
                    svc["spec"]["type"] = "NodePort"
                svc["spec"]["ports"][0]["nodePort"] = value
            return fn

        def m7(m, wl):
            wl["spec"]["replicas"] = max(3, wl["spec"].get("replicas", 1) or 0)
            p = wl["spec"]["template"]["spec"]["containers"][0]["ports"][0]
            p["hostPort"] = p["containerPort"]

        def m11(m, wl):
            ct = wl["spec"]["template"]["spec"]["containers"][0]
            ct["image"] = untag(ct["image"])
            if (wl["spec"].get("replicas", 1) or 0) < 2:
                wl["spec"]["replicas"] = 2

        def m14(m, wl):
            used = set(c.container_ports)
            for ct in wl["spec"]["template"]["spec"]["containers"]:
                for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
                    for h in ("httpGet", "tcpSocket", "grpc"):
                        spec = (ct.get(probe) or {}).get(h)
                        if isinstance(spec, dict) and "port" in spec:
                            spec["port"] = bump(max(used), used)
                            return
            return False

        def m16(m, wl):
            for ct in wl["spec"]["template"]["spec"]["containers"]:
                lim = (ct.get("resources") or {}).get("limits") or {}
                lim.pop("memory", None)

        out = []
        if c.replicas is not None:
            out += [mutant("M1", m_storage(3, "ReadWriteOnce")), mutant("M5", m_storage(0, "ReadWriteOnce")),
                    mutant("M15", m_storage(3, "ReadWriteOncePod"))]
            if c.container_ports and (find_workload(docs, c.id, k)["spec"]["template"]["spec"]["containers"][0]
                                      .get("ports")):
                out.append(mutant("M7", m7))
            if c.image_tag and c.image_tag != "latest":
                out.append(mutant("M11", m11))
        if svc_ports and svc_ports[0].target is not None and len(c.container_ports) == 1:
            out.append(mutant("M2", m_service(m2)))
        if svc_ports:
            out += [mutant("M3", m_service(nodeport(8080))), mutant("M4", m_service(nodeport(40000)))]
        if c.probe_ports and c.container_ports:
            out.append(mutant("M14", m14))
        if c.memory_limit and c.memory_limit != "LimitRange default":
            out.append(mutant("M16", m16))
        # M8: the first selecting Service and another Service of the group share a NodePort
        wl0 = find_workload(docs, c.id, k)
        sel = selecting_services(docs, wl0)
        sel = [x for x in sel if (x.get("spec") or {}).get("clusterIP") != "None"] or sel
        if sel:
            s0 = sel[0]
            others = [s for s in effective_services(docs) if s is not s0 and (s.get("spec") or {}).get("ports")
                      and (s.get("spec") or {}).get("type") != "ExternalName"
                      and (s.get("spec") or {}).get("clusterIP") != "None"]
            e0 = f"{source}::Service/{s0['metadata']['name']}"
            if others and (s0.get("spec") or {}).get("clusterIP") != "None" and not ({"S8"} & before.get(e0, set())):
                i1 = next(i for i, x in enumerate(docs) if x is s0)
                i2 = next(i for i, x in enumerate(docs) if x is others[0])

                def m8(m, wl, i1=i1, i2=i2):
                    for i in (i1, i2):
                        nodeport(30080)(m[i])
                e1 = f"{source}::Service/{others[0]['metadata']['name']}"
                out.append(mutant("M8", m8, [e0, e1]))
            # M12: a selector value of the first selecting Service is changed
            if e0 in endpoints and c.id in endpoints[e0].routes_to and len(endpoints[e0].routes_to) == 1 \
                    and not ({"S12"} & before.get(e0, set())):
                i1 = next(i for i, x in enumerate(docs) if x is s0)

                def m12(m, wl, i1=i1):
                    sel_ = m[i1]["spec"]["selector"]
                    key = sorted(sel_)[0]
                    sel_[key] = f"{sel_[key]}-x"
                out.append(mutant("M12", m12, [e0]))
        # M9/M10: dependencies declared in environment variables
        for d in c.dependencies:
            ep = [e for e in endpoints.values() if d.host in e.names]
            if not ep:
                continue
            served = {p.exposed for p in ep[0].ports if p.exposed is not None}

            def containers(wl):
                return wl["spec"]["template"]["spec"]["containers"]
            if d.port is not None and served:
                new = bump(max(served), served)
                out.append(mutant("M9", lambda m, wl, d=d, new=new: any(
                    change_dep_port(ct.get("env"), d.key, d.port, new) for ct in containers(wl)) or False))
            out.append(mutant("M10", lambda m, wl, d=d: any(
                set_env(ct.get("env"), d.key, lambda v: misspell(v, d.host)) for ct in containers(wl)) or False))
            break                                  # one dependency per workload
        yield from (o for o in out if o)
    # M13: Ingress backends
    for c in cfgs:
        if c.kind != "ingress" or {"S13"} & before.get(c.id, set()):
            continue
        name = c.id.split("::Ingress/", 1)[1]

        def m13(m, wl=None, name=name):
            ing = [d for d in m if d.get("kind") == "Ingress" and d["metadata"]["name"] == name][0]
            for rule in ing.get("spec", {}).get("rules") or []:
                for path in (rule.get("http") or {}).get("paths") or []:
                    b = path.get("backend") or {}
                    if "service" in b:
                        port = b["service"].setdefault("port", {})
                        if "number" in port:
                            port["number"] += 1
                        else:
                            port["name"] = f"{port.get('name', 'http')}-x"
                        return
                    if "servicePort" in b:
                        b["servicePort"] = b["servicePort"] + 1 if isinstance(b["servicePort"], int) else f"{b['servicePort']}-x"
                        return
            return False
        m = copy.deepcopy(docs)
        if m13(m) is not False:
            yield "M13", [c.id], m


# ----------------------------------------------------------------------------- Compose
def compose_mutants(path, doc, cfgs, before):
    wl = {c.id.split("::", 1)[1]: c for c in cfgs if c.kind == "workload"}
    eps = [c for c in cfgs if c.kind == "endpoint"]
    hosted = [n for n, c in wl.items() if any(p.host_port is not None for p in c.ports)]
    for sname, c in wl.items():
        done = before.get(c.id, set())
        out = []

        def mutant(cat, edit, targets=None):
            if TARGET[cat] & done:
                return None
            m = copy.deepcopy(doc)
            if edit(m["services"][sname], m) is False:
                return None
            return cat, targets or [c.id], m
        if c.ports and c.ports[0].target is not None and len(c.container_ports) == 1:
            def m2(s, m):
                p = s["ports"][0]
                if isinstance(p, dict):
                    p["target"] = c.container_ports[0] + 1
                else:
                    parts = str(p).split(":")
                    parts[-1] = str(c.container_ports[0] + 1)
                    s["ports"][0] = ":".join(parts)
            out.append(mutant("M2", m2))
        if sname in hosted:
            out.append(mutant("M7", lambda s, m: s.setdefault("deploy", {}).__setitem__("replicas", 3)))
            other = [n for n in hosted if n != sname]
            if other:
                first = [p for p in wl[other[0]].ports if p.host_port is not None][0].host_port

                def m8(s, m, first=first):
                    for i, p in enumerate(s["ports"]):
                        if isinstance(p, dict) and "published" in p:
                            p["published"] = first
                            return
                        if not isinstance(p, dict) and str(p).count(":") >= 1:
                            parts = str(p).split(":")
                            parts[-2] = str(first)
                            s["ports"][i] = ":".join(parts)
                            return
                    return False
                out.append(mutant("M8", m8, [c.id, f"{c.id.split('::')[0]}::{other[0]}"]))
        if c.image_tag and c.image_tag != "latest" and isinstance((doc["services"][sname]).get("image"), str):
            def m11(s, m):
                s["image"] = untag(s["image"])
                s.setdefault("deploy", {})
                if (s["deploy"].get("replicas") or 1) < 2:
                    s["deploy"]["replicas"] = 2
            out.append(mutant("M11", m11))
        if c.memory_limit:
            def m16(s, m):
                s.pop("mem_limit", None)
                ((s.get("deploy") or {}).get("resources") or {}).get("limits", {}).pop("memory", None)
            out.append(mutant("M16", m16))
        for d in c.dependencies:
            ep = [e for e in eps if d.host in e.names]
            if not ep:
                continue
            served = {p.exposed for p in ep[0].ports if p.exposed is not None}

            if d.port is not None and served:
                new = bump(max(served), served)
                out.append(mutant("M9", lambda s, m, d=d, new=new:
                                  change_dep_port(s.get("environment"), d.key, d.port, new) or False))
            out.append(mutant("M10", lambda s, m, d=d:
                              set_env(s.get("environment"), d.key, lambda v: misspell(v, d.host)) or False))
            break
        yield from (o for o in out if o)


# ----------------------------------------------------------------------------- ECS services
def ecs_mutants(doc, cfgs, before):
    res, params = doc["Resources"], doc.get("Parameters") or {}
    for c in cfgs:
        if c.kind != "workload":
            continue
        lid = c.id.rsplit("::", 1)[1]
        done = before.get(c.id, set())
        props = res[lid]["Properties"]
        td_id = props["TaskDefinition"]["Ref"]
        tdp = res[td_id]["Properties"]
        out = []

        def mutant(cat, edit, targets=None):
            if TARGET[cat] & done and targets is None:
                return None
            m = copy.deepcopy(doc)
            if edit(m["Resources"][lid]["Properties"], m["Resources"][td_id]["Properties"], m) is False:
                return None
            return cat, targets or [c.id], m

        if c.ports and c.ports[0].target is not None and len(c.container_ports) == 1:
            def m2(p, t, m):
                for key in ("LoadBalancers", "ServiceRegistries"):
                    for lb in p.get(key) or []:
                        if isinstance(lb, dict) and "ContainerPort" in lb:
                            lb["ContainerPort"] = c.container_ports[0] + 1
                            return
                return False
            out.append(mutant("M2", m2))
        image = (tdp.get("ContainerDefinitions") or [{}])[0].get("Image")
        if c.image_tag and c.image_tag != "latest":
            def m11(p, t, m):
                img = t["ContainerDefinitions"][0]["Image"]
                if isinstance(img, str):
                    t["ContainerDefinitions"][0]["Image"] = untag(img)
                elif isinstance(img, dict) and "Ref" in img:
                    m["Parameters"][img["Ref"]]["Default"] = untag(m["Parameters"][img["Ref"]]["Default"])
                else:
                    return False
                p["DesiredCount"] = max(2, c.replicas or 0)
            out.append(mutant("M11", m11))
        compat = tdp.get("RequiresCompatibilities") or []
        if c.memory_limit and "FARGATE" not in compat:
            def m16(p, t, m):
                t.pop("Memory", None)
                for d in t.get("ContainerDefinitions") or []:
                    mem = d.pop("Memory", None)
                    d.setdefault("MemoryReservation", mem or 256)
            out.append(mutant("M16", m16))
        mode = _resolve(tdp.get("NetworkMode"), params)
        if c.replicas is not None and mode in ("bridge", None) and "FARGATE" not in compat and c.container_ports:
            def m7(p, t, m):
                pm = t["ContainerDefinitions"][0].get("PortMappings") or []
                if not pm:
                    return False
                pm[0]["HostPort"] = pm[0]["ContainerPort"]
                p["DesiredCount"] = 3
            out.append(mutant("M7", m7))
        for j, e in enumerate(x for x in cfgs if x.kind == "endpoint" and x.id.startswith(c.id + "@lb")):
            if e.routes_to and "S12" not in before.get(e.id, set()):
                def m12(p, t, m, j=j):
                    lbs = [lb for lb in p.get("LoadBalancers") or [] if isinstance(lb, dict)
                           and _resolve(lb.get("ContainerName"), params) is not UNRESOLVED]
                    lbs[j]["ContainerName"] = "missing-container"
                out.append(mutant("M12", m12, [e.id]))
        yield from (o for o in out if o)


# ----------------------------------------------------------------------------- driver
def main():
    units, failed = [], []        # units: (category, platform, target ids, mutated unit configs)
    n = 0

    def add(cat, platform, targets, cfgs):
        nonlocal n
        key = f"m{n:04d}#{cat}"
        n += 1
        cfgs = dedupe(cfgs)
        ids = {c.id for c in cfgs}
        if not all(t in ids for t in targets):
            failed.append((targets, cat, "not translated"))
            return
        # the whole mutated unit is translated; for validation, only the elements that the targeted
        # constraint relates are kept: the mutated elements and, for the constraints that resolve
        # names or compare elements of a unit, the endpoints of the unit
        keep = [c for c in cfgs if c.id in targets or (cat in RELATIONAL and c.kind == "endpoint")]
        units.append((cat, platform, [f"{t}#{key}" for t in targets], isolate(keep, key)))

    for source, docs, incomplete in k8s_groups():
        cfgs = dedupe(k8s_group_to_configs(docs, source, incomplete=incomplete))
        before = opa_rules(cfgs)
        for cat, targets, mdocs in k8s_mutants(source, docs, cfgs, before):
            # S10, S12 and S13 are not evaluated on incomplete units, so M10, M12, M13 are not injected there
            if incomplete and cat in ("M10", "M12", "M13"):
                continue
            add(cat, "kubernetes", targets, k8s_group_to_configs(mdocs, source, incomplete=incomplete))

    r = CORPORA / REPOS["awesome-compose"]
    compose_units = [(f, r, None) for f in sorted(p for p in r.glob("*/*") if p.name in (
        "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"))] + app_compose_units()
    for f, root, merged in compose_units:
        cfgs = compose_to_configs(f, root, merged)
        if not cfgs:
            continue
        doc = merged if merged is not None else yaml.safe_load(f.read_text(errors="ignore"))
        before = opa_rules(cfgs)
        for cat, targets, mdoc in compose_mutants(f, doc, cfgs, before):
            add(cat, "compose", targets, compose_to_configs(f, root, mdoc))

    for key in ECS_CFN:
        rr = CORPORA / REPOS[key]
        for f in cfn_files(rr):
            doc = load_cfn(f)
            if doc is None:
                continue
            src = str(f.relative_to(CORPORA))
            cfgs = ecs_cfn_doc_to_configs(doc, src)
            if not cfgs:
                continue
            before = opa_rules(cfgs)
            for cat, targets, mdoc in ecs_mutants(doc, cfgs, before):
                add(cat, "ecs", targets, ecs_cfn_doc_to_configs(mdoc, src))

    allc = [c for _, _, _, cf in units for c in cf]
    tmp = Path(tempfile.mkdtemp())
    tbox = ROOT / "ontology" / "microwl-tbox.owl"
    tbox_nos2 = ROOT / "ontology" / "microwl-tbox-noS2.owl"
    # Pellet and pySHACL are run on chunks of whole deployment units (all rules and shapes are
    # evaluated within one unit), so that large mutated applications fit in memory
    pel, err = pellet_rules_chunked(allc, tbox)
    # hybrid validator: Pellet without S2, then the hybrid shapes over the reasoned graph of each chunk
    hyb, err2 = hybrid_rules_chunked(allc)
    err = err or err2
    # pySHACL grows super-linearly with the size of the data graph, so the shapes are evaluated on
    # chunks of whole deployment units (all constraints are evaluated within one unit)
    sh, shc = shacl_rules_chunked(allc, tbox, [SHAPES, SHAPES_CLOSED], tmp)
    opa, opc = {}, {}
    for chunk in chunks_by_unit(allc, 3000):
        opa.update(opa_rules(chunk, REGO, "data.microwl.violation"))
        opc.update(opa_rules(chunk))

    by = defaultdict(lambda: defaultdict(lambda: dict(n=0, pellet=0, shacl=0, opa=0, shacl_closed=0,
                                                      opa_closed=0, hybrid=0)))
    missed = []
    for cat, platform, targets, _ in units:
        b = by[platform][cat]
        b["n"] += 1
        hit = lambda rules: any(TARGET[cat] & set(rules.get(t, ())) for t in targets)
        for name, rules in (("pellet", pel), ("shacl", sh), ("opa", opa), ("shacl_closed", shc),
                            ("opa_closed", opc), ("hybrid", hyb)):
            b[name] += hit(rules)
        if not hit(hyb):
            missed.append((cat, platform, targets))
    total = {cat: sum(by[p][cat]["n"] for p in by if cat in by[p]) for cat in TARGET}
    report = {"pellet_error": err, "by_platform": by, "total_by_category": total, "total": len(units),
              "n_elements": len(allc), "storage_edits_m1_m5_m15": STORAGE_EDITS, "not_translated": failed,
              "missed_by_hybrid": missed}
    (ROOT / "results" / "mutation_files.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({"total": len(units), "elements": len(allc), "not_translated": len(failed),
                      "missed": len(missed), "by_category": total, "by_platform": by}, indent=1))


if __name__ == "__main__":
    main()
