"""Competency questions: SPARQL queries over the case-study knowledge base
(MicrOWL ABox, materialised with Pellet). Results are written to
results/competency_questions.json.

Usage: python competency_questions.py
"""
import json
import tempfile
from pathlib import Path

import rdflib
from owlready2 import World, sync_reasoner_pellet

from microwl_lib import ROOT, SHAPES_HYBRID, shacl_validate, shacl_rules, SHACL_RULES

PREFIXES = """PREFIX mw: <http://www.khaos.uma.es/microwl/>
PREFIX omsac: <http://www.semanticweb.org/ontologyOfMicroservicesArchitecture/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
CQS = {
    "CQ1": ("Which microservices are deployed on Kubernetes?",
            """SELECT ?ms WHERE { ?ms mw:hasPlatformConfig ?c . ?c a mw:KubernetesConfig . }"""),
    "CQ2": ("What are the abstract specifications of MyWebApp?",
            """SELECT ?replicas ?image ?cport ?eport ?tport ?size ?mode WHERE {
                 mw:MyWebApp mw:hasWorkloadSpec/mw:hasReplicas ?replicas ;
                   mw:hasContainerSpec ?c ; mw:hasNetworkSpec ?n ; mw:hasStorageSpec ?s .
                 ?c mw:hasContainerImage ?image ; mw:hasContainerPort ?cport .
                 ?n mw:hasExposedPort ?eport ; mw:hasTargetPort ?tport .
                 ?s mw:hasStorageSize ?size ; mw:hasAccessMode ?mode . }"""),
    "CQ3": ("Which microservices have an inconsistent deployment?",
            """SELECT ?ms WHERE { ?ms a mw:InconsistentDeployment . }"""),
    "CQ4": ("Which Kubernetes configurations use an out-of-range NodePort?",
            """SELECT ?cfg ?p WHERE { ?cfg a mw:KubernetesConfig ; mw:hasResource ?r .
                 ?r a mw:NodePortResource ; mw:hasNodePortValue ?p . FILTER(?p < 30000 || ?p > 32767) }"""),
    "CQ5": ("Which platform-specific parameters does each configuration of DataAggregator define?",
            """SELECT ?cfg ?type ?value WHERE { mw:DataAggregator mw:hasPlatformConfig ?cfg .
                 ?cfg mw:hasResource ?r . ?r mw:resourceType ?type .
                 OPTIONAL { ?r mw:resourceValue ?value } }"""),
    "CQ6": ("Which microservices target more than one platform?",
            """SELECT ?ms (COUNT(DISTINCT ?c) AS ?n) WHERE { ?ms mw:hasPlatformConfig ?c . }
               GROUP BY ?ms HAVING (COUNT(DISTINCT ?c) > 1)"""),
    "CQ7": ("Which microservices replicate a workload over shared ReadWriteOnce storage?",
            """SELECT ?ms ?r WHERE { ?ms mw:hasWorkloadSpec/mw:hasReplicas ?r ; mw:hasStorageSpec ?s .
                 ?s a mw:SharedStorageSpecification ; mw:hasAccessMode ?m .
                 FILTER(?r > 1 && str(?m) = "ReadWriteOnce") }"""),
    "CQ8": ("On which infrastructure targets is each microservice deployed?",
            """SELECT DISTINCT ?ms ?infra WHERE { ?ms mw:hasPlatformConfig ?c .
                 ?c mw:deployedOnInfrastructure ?infra . }"""),
    "CQ9": ("On which microservices does MyWebApp depend?",
            """SELECT ?ms WHERE { mw:MyWebApp mw:dependsOn ?ms . }"""),
    "CQ10": ("Which microservices are affected if PaymentProcessor fails?",
             """SELECT ?ms WHERE { ?ms mw:dependsOn mw:PaymentProcessor . }"""),
    "CQ11": ("Which dependencies do not resolve to an endpoint of their deployment unit?",
             """SELECT ?ms ?host WHERE { ?ms mw:hasDependency ?d . ?d mw:dependsOnHost ?host .
                  FILTER NOT EXISTS { ?d mw:resolvesTo ?e } }"""),
    "CQ12": ("Which constraints does each microservice violate, and with which severity?",
             """SELECT ?ms ?id ?severity WHERE { ?ms mw:violates ?c . ?c mw:hasConstraintId ?id .
                  { ?c a mw:ErrorConstraint . BIND("error" AS ?severity) } UNION
                  { ?c a mw:WarningConstraint . BIND("warning" AS ?severity) } }"""),
}


def short(v):
    s = str(v)
    return s.rsplit("/", 1)[-1] if s.startswith("http") else s


def main():
    w = World()
    onto = w.get_ontology("file://" + str(ROOT / "ontology" / "microwl.owl")).load()
    with onto:
        sync_reasoner_pellet(w, infer_property_values=True, infer_data_property_values=True, debug=0)
    tmp = Path(tempfile.mkdtemp()) / "materialised.owl"
    onto.save(file=str(tmp), format="rdfxml")
    g = rdflib.Graph()
    g.parse(str(tmp), format="xml")
    out = {}
    for cq, (question, q) in CQS.items():
        rows = [[short(x) for x in row] for row in g.query(PREFIXES + q)]
        out[cq] = dict(question=question, query=q, n_answers=len(rows), answers=rows)
        print(cq, question, "->", rows)
    # closed-world shapes of the hybrid validator over the reasoned case-study knowledge base
    shacl_validate(g, SHAPES_HYBRID)
    out["shacl_hybrid_case_study"] = {k: sorted(v) for k, v in SHACL_RULES.items()}
    print("SHACL (hybrid shapes):", out["shacl_hybrid_case_study"])
    with open(ROOT / "results" / "competency_questions.json", "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
