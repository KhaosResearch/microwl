"""Number of workloads of the RQ2 corpus on which each rule can be evaluated.

A rule is applicable to a workload when the translated descriptor provides,
from independent fields, every fact that its antecedent needs; under the
open-world assumption the rule can never fire on the other workloads, whatever
their configuration. For standalone ECS task definitions, the translator takes
the target port and the container port from the same containerPort field, so
S2c cannot be violated and is not counted as applicable (the number of task
definitions that do provide both facts is reported as S2c_same_field). For ECS
services, the target port comes from the service (load balancers and service
registries) and the container ports from the task definition.

  S1   replicas and a shared storage specification with an access mode
  S2c  a target port and at least one container port
  S3   a NodePort (Kubernetes only)
  S5   replicas and a storage specification with an access mode
  S7   replicas and a host port
  S8   Compose: a host port, and another service of the same file with a host port;
       Kubernetes: an endpoint with a NodePort, and another one in the same unit (counted on endpoints)
  S9   a dependency with a port (or port name) that resolves to an endpoint with ports
  S10  a dependency
  S11  replicas and an image tag
  S12  an endpoint with a selector (counted on endpoints)
  S13  an Ingress with a backend (counted on Ingresses)
  S14  a probe port and at least one container port
  S15  replicas and a shared storage specification with an access mode
  S16  every workload (the constraint checks the absence of a memory limit)

Usage: python rule_applicability.py <corpora_dir>
"""
import json
import sys

from microwl_lib import ROOT
from real_world import load

PLATFORM = {"online-boutique": "kubernetes", "sock-shop": "kubernetes", "k8s-examples": "kubernetes",
            "awesome-compose": "compose", "ecs-task-defs": "ecs", "ecs-services": "ecs-services",
            "apps-k8s": "kubernetes", "apps-compose": "compose"}
def resolves(c, d, cfgs):
    return any(e.kind == "endpoint" and e.deployment == c.deployment and d.host in e.names
               and any(p.exposed is not None or p.name for p in e.ports) for e in cfgs)


def s8(c, cfgs):
    if c.kind == "workload" and c.platform == "compose":
        mine = lambda x: x.kind == "workload" and any(p.host_port is not None for p in x.ports)
    elif c.kind == "endpoint" and c.platform == "kubernetes":
        mine = lambda x: x.kind == "endpoint" and any(p.node_port is not None for p in x.ports)
    else:
        return False
    return mine(c) and any(mine(x) and x.id != c.id and x.deployment == c.deployment for x in cfgs)


RULES = {
    "S1": lambda c, a: c.replicas is not None and any(s.access_mode and s.scope == "shared" for s in c.storage),
    "S2c": lambda c, a: any(p.target is not None for p in c.ports) and bool(c.container_ports),
    "S3": lambda c, a: any(p.node_port is not None for p in c.ports),
    "S5": lambda c, a: c.replicas is not None and any(s.access_mode for s in c.storage),
    "S7": lambda c, a: c.replicas is not None and any(p.host_port is not None for p in c.ports),
    "S8": None,
    "S9": lambda c, a: any((d.port is not None or d.port_name) and resolves(c, d, a) for d in c.dependencies),
    "S10": lambda c, a: bool(c.dependencies),
    "S11": lambda c, a: c.replicas is not None and bool(c.image_tag),
    "S12": None,
    "S13": None,
    "S14": lambda c, a: bool(c.probe_ports) and bool(c.container_ports),
    "S15": lambda c, a: c.replicas is not None and any(s.access_mode and s.scope == "shared" for s in c.storage),
    "S16": lambda c, a: True,
}


def main():
    by_platform = {}
    for corpus, cfgs in load().items():
        by_platform.setdefault(PLATFORM[corpus], []).append(cfgs)
    res = {}
    for plat, groups in by_platform.items():
        allc = [c for g in groups for c in g]
        wl = [c for c in allc if c.kind == "workload"]
        res[plat] = {"workloads": len(wl),
                     "endpoints_with_selector": sum(c.kind == "endpoint" and c.selector for c in allc),
                     "ingresses": sum(c.kind == "ingress" for c in allc)}
        for r, f in RULES.items():
            if r == "S8":
                res[plat][r] = sum(s8(c, g) for g in groups for c in g)
            elif r == "S12":
                res[plat][r] = res[plat]["endpoints_with_selector"]
            elif r == "S13":
                res[plat][r] = sum(c.kind == "ingress" and bool(c.dependencies) for c in allc)
            else:
                res[plat][r] = sum(f(c, g) for g in groups for c in g if c.kind == "workload")
        if plat == "ecs":
            res[plat]["S2c_same_field"] = res[plat]["S2c"]
            res[plat]["S2c"] = 0
    print(json.dumps(res, indent=2))
    (ROOT / "results" / "rule_applicability.json").write_text(json.dumps(res, indent=2) + "\n")


if __name__ == "__main__":
    main()
