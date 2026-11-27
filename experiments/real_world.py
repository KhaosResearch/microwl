"""Evaluation on third-party deployment descriptors (RQ2) and mutation analysis.

Corpora (shallow clones, commit hashes recorded in results/real_world.json):
  * Online Boutique (GoogleCloudPlatform/microservices-demo)  -- Kubernetes
  * Sock Shop (microservices-demo/microservices-demo)           -- Kubernetes
  * kubernetes/examples                                         -- Kubernetes
  * docker/awesome-compose                                      -- Docker Compose
  * reference applications TrainTicket, DeathStarBench (Hotel Reservation, Social Network and
    Media Microservices; the last two rendered from their Helm charts), TeaStore, Bookinfo
    (Kubernetes), and TrainTicket, DeathStarBench, TeaStore, eShopOnContainers and Spring
    PetClinic Microservices (Docker Compose)
  * aws-samples/aws-containers-task-definitions                 -- AWS ECS (task definitions)
  * CloudFormation templates that define an ECS service together with its task
    definition, from awslabs/aws-cloudformation-templates,
    aws-samples/ecs-refarch-cloudformation, nathanpeck/ecs-cloudformation and
    aws-samples/serverless-patterns                             -- AWS ECS (services)

Steps: translate every descriptor to MicrOWL Configs (workloads, endpoints and
Ingresses), report which abstract properties can be populated per platform,
validate the unmodified corpus with Pellet, SHACL and OPA, attribute every
finding to the constraints it violates (findings are then inspected manually),
and inject faults M1-M5 into the translated configurations (auxiliary mutation
analysis; the main mutation analysis modifies the descriptor files, see
mutate_files.py).

Usage: python real_world.py <corpora_dir>
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import microwl_lib
from microwl_lib import (SEVERITY, PelletValidator, shacl_validate, shacl_flagged_ids, shacl_rules, opa_validate, opa_rules,
                         opa_closed, hybrid_validate, SHAPES_CLOSED, REGO, explain, safe, ROOT, MW)
import yaml
from translate import (k8s_corpus, k8s_group_to_configs, load_k8s_docs, is_templated, compose_to_configs,
                       merge_compose, ecs_to_configs, ecs_cfn_to_configs, coverage, mutate)

CORPORA = Path(sys.argv[1])
REPOS = {
    "online-boutique": "GoogleCloudPlatform_microservices-demo",
    "sock-shop": "microservices-demo_microservices-demo",
    "k8s-examples": "kubernetes_examples",
    "awesome-compose": "docker_awesome-compose",
    "ecs-task-defs": "aws-samples_aws-containers-task-definitions",
    "cfn-templates": "awslabs_aws-cloudformation-templates",
    "ecs-refarch": "aws-samples_ecs-refarch-cloudformation",
    "ecs-cloudformation": "nathanpeck_ecs-cloudformation",
    "serverless-patterns": "aws-samples_serverless-patterns",
    "trainticket": "FudanSELab_train-ticket",
    "deathstarbench": "delimitrou_DeathStarBench",
    "teastore": "DescartesResearch_TeaStore",
    "eshop": "dotnet-architecture_eShopOnContainers",
    "petclinic": "spring-petclinic_spring-petclinic-microservices",
    "bookinfo": "istio_istio",
}
# Reference microservice applications: one deployment unit per application and format.
# Helm charts are rendered with `helm template` into corpora/rendered/ (see the README).
APPS_K8S = [
    ("trainticket", ["deployment/kubernetes-manifests/k8s-with-jaeger/ts-deployment-part1.yml",
                     "deployment/kubernetes-manifests/k8s-with-jaeger/ts-deployment-part2.yml",
                     "deployment/kubernetes-manifests/k8s-with-jaeger/ts-deployment-part3.yml"]),
    ("deathstarbench", ["hotelReservation/kubernetes"]),
    ("rendered", ["dsb-socialnetwork.yaml"]),
    ("rendered", ["dsb-mediamicroservices.yaml"]),
    ("teastore", ["examples/kubernetes/teastore-clusterip.yaml"]),
    ("bookinfo", ["samples/bookinfo/platform/kube/bookinfo.yaml"]),
]
APPS_COMPOSE = [
    ("trainticket", ["deployment/docker-compose-manifests/quickstart-docker-compose.yml"]),
    ("deathstarbench", ["hotelReservation/docker-compose.yml"]),
    ("deathstarbench", ["socialNetwork/docker-compose.yml"]),
    ("deathstarbench", ["mediaMicroservices/docker-compose.yml"]),
    ("teastore", ["examples/docker/docker-compose_default.yaml"]),
    ("eshop", ["src/docker-compose.yml", "src/docker-compose.override.yml"]),
    ("petclinic", ["docker-compose.yml"]),
]


def repo_dir(key):
    return CORPORA / ("rendered" if key == "rendered" else REPOS[key])


def app_k8s_units():
    """(source, docs, incomplete) for every reference application deployed on Kubernetes."""
    out = []
    for key, rel in APPS_K8S:
        r = repo_dir(key)
        files = []
        for x in rel:
            p = r / x
            files += sorted(q for q in p.rglob("*") if q.suffix in (".yaml", ".yml")) if p.is_dir() else [p]
        docs = [d for f in files for d in load_k8s_docs(f)]
        out.append((str((r / rel[0]).relative_to(CORPORA)), docs, any(is_templated(f) for f in files)))
    return out


def app_compose_units():
    """(path, root, merged document) for every reference application deployed with Compose."""
    out = []
    for key, rel in APPS_COMPOSE:
        r = repo_dir(key)
        doc = None
        for x in rel:
            d = yaml.safe_load((r / x).read_text(errors="ignore"))
            doc = d if doc is None else merge_compose(doc, d)
        out.append((r / rel[0], CORPORA, doc))
    return out
ECS_CFN = ("cfn-templates", "ecs-refarch", "ecs-cloudformation", "serverless-patterns")


def cfn_files(r):
    """CloudFormation templates that mention an ECS service (JSON copies of YAML templates skipped)."""
    files = sorted(p for p in r.rglob("*") if p.suffix in (".yaml", ".yml", ".json", ".template")
                   and ".git" not in p.parts and "AWS::ECS::Service" in p.read_text(errors="ignore"))
    return [f for f in files if not (f.suffix == ".json" and any(f.with_suffix(x).exists() for x in (".yaml", ".yml")))]


def commit(repo):
    d = CORPORA / repo
    return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def load():
    out = {}
    r = CORPORA / REPOS["online-boutique"]
    out["online-boutique"] = k8s_corpus(r, [r / "release" / "kubernetes-manifests.yaml"])[0]
    r = CORPORA / REPOS["sock-shop"]
    out["sock-shop"] = k8s_corpus(r, [r / "deploy" / "kubernetes" / "complete-demo.yaml"])[0]
    r = CORPORA / REPOS["k8s-examples"]
    files = sorted(p for p in r.rglob("*") if p.suffix in (".yaml", ".yml") and ".git" not in p.parts)
    out["k8s-examples"] = k8s_corpus(r, files)[0]
    r = CORPORA / REPOS["awesome-compose"]
    files = sorted(p for p in r.glob("*/*") if p.name in ("compose.yaml", "compose.yml",
                                                       "docker-compose.yaml", "docker-compose.yml"))
    out["awesome-compose"] = [c for f in files for c in compose_to_configs(f, r)]
    r = CORPORA / REPOS["ecs-task-defs"]
    out["ecs-task-defs"] = [c for f in sorted(r.rglob("*.json")) for c in ecs_to_configs(f, r)]
    out["apps-k8s"] = [c for src, docs, inc in app_k8s_units() for c in k8s_group_to_configs(docs, src, incomplete=inc)]
    out["apps-compose"] = [c for path, root, doc in app_compose_units() for c in compose_to_configs(path, root, doc)]
    out["ecs-services"] = []
    for key in ECS_CFN:
        r = CORPORA / REPOS[key]
        out["ecs-services"] += [c for f in cfn_files(r) for c in ecs_cfn_to_configs(f, CORPORA)]
    for cfgs in out.values():           # make identifiers unique after IRI sanitisation
        seen = {}
        for c in cfgs:
            k = safe(c.id)
            if k in seen:
                seen[k] += 1
                c.id = f"{c.id}~{seen[k]}"
            else:
                seen[k] = 0
    return out


TARGET = {"M1": ("S1",), "M2": ("S2", "S2c"), "M3": ("S3",), "M4": ("S3",), "M5": ("S5",)}


def main():
    v = PelletValidator(ROOT / "ontology" / "microwl-tbox.owl")
    data = load()
    tmp = Path(tempfile.mkdtemp())
    report = {"commits": {k: commit(r) for k, r in REPOS.items()}, "corpora": {}}
    all_findings = []
    for name, cfgs in data.items():
        workloads = [c for c in cfgs if c.kind == "workload"]
        abox = tmp / f"{name}.owl"
        pel, t_pel, err = v.validate(cfgs, keep_file=abox)
        pel_rules, depends_on = v.rules, sorted(v.depends_on)
        entry = {"n_workloads": len(workloads), "n_endpoints": sum(c.kind == "endpoint" for c in cfgs),
                 "n_ingresses": sum(c.kind == "ingress" for c in cfgs),
                 "n_sources": len({c.source for c in workloads}),
                 "coverage": coverage(cfgs), "pellet_error": err}
        if err is None:
            focus, t_sh = shacl_validate(abox)
            sh = shacl_flagged_ids(focus, cfgs)
            opa, t_opa = opa_validate(cfgs)
            opa_r = opa_rules(cfgs, REGO, "data.microwl.violation")
            shc = shacl_flagged_ids(shacl_validate(abox, SHAPES_CLOSED)[0], cfgs)
            opc, _ = opa_closed(cfgs)
            opc_r = opa_rules(cfgs)
            hyb, t_hyb = hybrid_validate(cfgs, tmp)
            hyb_r = dict(microwl_lib.HYBRID_RULES)
            by_rule = {}
            for rs in hyb_r.values():
                for r in rs:
                    by_rule[r] = by_rule.get(r, 0) + 1
            sev = dict(elements_with_error=sum(any(SEVERITY[r] == "error" for r in rs) for rs in hyb_r.values()),
                       elements_with_only_warnings=sum(all(SEVERITY[r] == "warning" for r in rs)
                                                       for rs in hyb_r.values()))
            entry.update(by_severity_hybrid=sev, n_dependencies=sum(len(c.dependencies) for c in workloads),
                         flagged=dict(pellet=len(pel), shacl=len(sh), opa=len(opa), shacl_closed=len(shc),
                                      opa_closed=len(opc), hybrid=len(hyb)),
                         flagged_by_rule_hybrid=dict(sorted(by_rule.items())),
                         inferred_depends_on=len(depends_on),
                         agreement_closed=dict(hybrid_vs_shacl_closed=hyb == shc, hybrid_vs_opa_closed=hyb == opc,
                                               rules_hybrid_vs_opa_closed=hyb_r == opc_r),
                         agreement=dict(pellet_vs_shacl=pel == sh, pellet_vs_opa=pel == opa),
                         seconds=dict(pellet=round(t_pel, 3), shacl=round(t_sh, 3), opa=round(t_opa, 3),
                                      hybrid=round(t_hyb, 3)))
            for c in cfgs:
                if c.id in pel or c.id in opa or c.id in hyb or c.id in opc:
                    expl = ""
                    if c.id in pel and c.kind == "workload":
                        expl = explain(abox, MW + "ms_" + safe(c.id), MW + "InconsistentDeployment")
                    all_findings.append(dict(corpus=name, id=c.id, kind=c.kind,
                                             rules_open=sorted(opa_r.get(c.id, [])),
                                             rules_closed=sorted(opc_r.get(c.id, [])),
                                             pellet=c.id in pel, shacl=c.id in sh, opa=c.id in opa,
                                             hybrid=c.id in hyb, shacl_closed=c.id in shc, opa_closed=c.id in opc,
                                             config=c.to_json(), explanation=expl))
            # ---- auxiliary mutation analysis on the translated configurations (M1-M5) ----
            mutants = []
            for c in workloads:
                done = hyb_r.get(c.id, set())
                k8s = c.platform == "kubernetes"
                svc_ports = [p for p in c.ports if p.target is not None or p.node_port is not None]
                has_port = bool(svc_ports) and svc_ports[0].target is not None
                if c.replicas is not None:
                    mutants += [mutate(c, m) for m in ("M1", "M5") if TARGET[m][0] not in done]
                # a standalone ECS task definition takes the target port from the container port itself,
                # so a target-port fault cannot occur in it (see rule_applicability.py)
                if has_port and len(c.container_ports) == 1 and name != "ecs-task-defs" and "S2c" not in done:
                    mutants.append(mutate(c, "M2"))
                if k8s and svc_ports and "S3" not in done:
                    mutants += [mutate(c, "M3"), mutate(c, "M4")]
            if mutants:
                mp, _, merr = v.validate(mutants)
                mp_r = v.rules
                shacl_validate(_save(v, mutants, tmp / f"{name}-mut.owl"))
                ms_r = shacl_rules(mutants)
                mo_r = opa_rules(mutants, REGO, "data.microwl.violation")
                hybrid_validate(mutants, tmp)
                mh_r = dict(microwl_lib.HYBRID_RULES)
                by = {}
                for m in mutants:
                    b = by.setdefault(m.expected, dict(n=0, pellet=0, shacl=0, opa=0, hybrid=0))
                    hit = lambda rr: bool(set(rr.get(m.id, ())) & set(TARGET[m.expected]))
                    b["n"] += 1
                    b["pellet"] += hit(mp_r)
                    b["shacl"] += hit(ms_r)
                    b["opa"] += hit(mo_r)
                    b["hybrid"] += hit(mh_r)
                entry["mutation"] = by
                entry["mutation_error"] = merr
        report["corpora"][name] = entry
        print(name, json.dumps(entry)[:600])
    (ROOT / "results").mkdir(exist_ok=True)
    with open(ROOT / "results" / "real_world.json", "w") as f:
        json.dump(report, f, indent=1)
    with open(ROOT / "results" / "real_world_findings.json", "w") as f:
        json.dump(all_findings, f, indent=1)
    print("findings:", len(all_findings))


def _save(v, cfgs, path):
    from owlready2 import World
    w = World()
    onto = w.get_ontology("file://" + str(v.tbox_path)).load()
    v._populate(onto, cfgs)
    onto.save(file=str(path), format="rdfxml")
    return path


if __name__ == "__main__":
    main()
