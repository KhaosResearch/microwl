"""Extensibility experiment (RQ4): add Azure Container Instances (ACI) as a new
platform in a separate module that imports MicrOWL without modifying it.

The module defines ACIConfig, ACICPUResource and ACIMemoryResource, two typed
datatype properties, one ACI-specific rule (S6: vCPU limit per container group),
and the ACI deployment of MyWebApp. The script then checks that
  (1) the core ontology file is byte-identical before and after,
  (2) the core rules S1-S5 apply unchanged to the ACI deployment, and
  (3) the module rule S6 fires when the ACI vCPU limit is exceeded.

Usage: python extend_aci.py
"""
import hashlib
import json
from pathlib import Path

import rdflib
from rdflib import RDF, RDFS, OWL, XSD, URIRef, Literal, BNode
from rdflib.collection import Collection
from owlready2 import World, sync_reasoner_pellet

from microwl_lib import ROOT

CORE = ROOT / "ontology" / "microwl.owl"
MODULE = ROOT / "ontology" / "microwl-aci.owl"
MW = rdflib.Namespace("http://www.khaos.uma.es/microwl/")
ACI = rdflib.Namespace("http://www.khaos.uma.es/microwl/aci/")
OMSAC = rdflib.Namespace("http://www.semanticweb.org/ontologyOfMicroservicesArchitecture/")
SWRL = rdflib.Namespace("http://www.w3.org/2003/11/swrl#")
SWRLB = rdflib.Namespace("http://www.w3.org/2003/11/swrlb#")
VCPU_LIMIT = 4


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def build_module():
    g = rdflib.Graph()
    ont = URIRef("http://www.khaos.uma.es/microwl/aci")
    g.add((ont, RDF.type, OWL.Ontology))
    g.add((ont, OWL.imports, URIRef("http://www.khaos.uma.es/microwl")))
    stats = {"classes": 0, "datatype_properties": 0, "rules": 0, "individuals": 0}

    def cls(c, parent):
        g.add((c, RDF.type, OWL.Class)); g.add((c, RDFS.subClassOf, parent)); stats["classes"] += 1

    def dprop(p, dom, rng):
        g.add((p, RDF.type, OWL.DatatypeProperty)); g.add((p, RDF.type, OWL.FunctionalProperty))
        g.add((p, RDFS.domain, dom)); g.add((p, RDFS.range, rng)); stats["datatype_properties"] += 1

    def ind(i, *types):
        g.add((i, RDF.type, OWL.NamedIndividual))
        for t in types:
            g.add((i, RDF.type, t))
        stats["individuals"] += 1

    cls(ACI.ACIConfig, MW.PlatformSpecificConfiguration)
    cls(ACI.ACICPUResource, MW.PlatformResource)
    cls(ACI.ACIMemoryResource, MW.PlatformResource)
    dprop(ACI.hasCpuCores, ACI.ACICPUResource, XSD.decimal)
    dprop(ACI.hasMemoryGB, ACI.ACIMemoryResource, XSD.decimal)

    ind(ACI.ACIPlatform, MW.DeploymentPlatform)
    ind(ACI.MyWebApp_ACIConfig, ACI.ACIConfig)
    g.add((ACI.MyWebApp_ACIConfig, MW.deployedOn, ACI.ACIPlatform))
    g.add((MW.MyWebApp, MW.hasPlatformConfig, ACI.MyWebApp_ACIConfig))
    g.add((MW.MyWebApp, MW.deployedTo, ACI.ACIPlatform))
    for name, types, extra in (
            ("MyWebApp_ACI_Cpu", [ACI.ACICPUResource], [(ACI.hasCpuCores, Literal("1.0", datatype=XSD.decimal))]),
            ("MyWebApp_ACI_Memory", [ACI.ACIMemoryResource], [(ACI.hasMemoryGB, Literal("1.5", datatype=XSD.decimal))]),
            ("MyWebApp_ACI_RestartPolicy", [MW.PlatformResource], [(MW.resourceType, Literal("restartPolicy")),
                                                                  (MW.resourceValue, Literal("Always"))]),
            ("MyWebApp_ACI_DnsName", [MW.PlatformResource], [(MW.resourceType, Literal("dnsNameLabel")),
                                                            (MW.resourceValue, Literal("mywebapp-demo"))])):
        i = ACI[name]
        ind(i, *types)
        g.add((ACI.MyWebApp_ACIConfig, MW.hasResource, i))
        for p, o in extra:
            g.add((i, p, o))

    # S6: ACI container groups are limited in vCPU (VCPU_LIMIT in this module)
    def var(n):
        v = ACI[n]; g.add((v, RDF.type, SWRL.Variable)); return v
    m, cfg, r, c = var("m"), var("cfg"), var("r"), var("c")

    def atom(kind, pred, *args):
        a = BNode()
        if kind == "class":
            g.add((a, RDF.type, SWRL.ClassAtom)); g.add((a, SWRL.classPredicate, pred)); g.add((a, SWRL.argument1, args[0]))
        elif kind in ("obj", "data"):
            g.add((a, RDF.type, SWRL.IndividualPropertyAtom if kind == "obj" else SWRL.DatavaluedPropertyAtom))
            g.add((a, SWRL.propertyPredicate, pred)); g.add((a, SWRL.argument1, args[0])); g.add((a, SWRL.argument2, args[1]))
        else:
            g.add((a, RDF.type, SWRL.BuiltinAtom)); g.add((a, SWRL.builtin, pred))
            lst = BNode(); Collection(g, lst, list(args)); g.add((a, SWRL.arguments, lst))
        return a
    rule = BNode()
    g.add((rule, RDF.type, SWRL.Imp)); g.add((rule, RDFS.label, Literal("S6_ACI_VcpuLimit")))
    body, head = BNode(), BNode()
    Collection(g, body, [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasPlatformConfig, m, cfg),
                         atom("class", ACI.ACIConfig, cfg), atom("obj", MW.hasResource, cfg, r),
                         atom("data", ACI.hasCpuCores, r, c),
                         atom("builtin", SWRLB.greaterThan, c, Literal(str(VCPU_LIMIT), datatype=XSD.decimal))])
    Collection(g, head, [atom("class", MW.InconsistentDeployment, m)])
    g.add((rule, SWRL.body, body)); g.add((rule, SWRL.head, head))
    stats["rules"] += 1
    g.serialize(str(MODULE), format="xml")
    return stats


def classify(mutator=None):
    w = World()
    core = w.get_ontology("file://" + str(CORE)).load()
    mod = w.get_ontology("file://" + str(MODULE)).load()
    mw, aci = core.get_namespace(str(MW)), core.get_namespace(str(ACI))
    if mutator:
        with mod:
            mutator(mw, aci)
    with mod:
        sync_reasoner_pellet(w, infer_property_values=True, infer_data_property_values=True, debug=0)
    return sorted(i.name for i in mw.InconsistentDeployment.instances())


def main():
    before = sha(CORE)
    stats = build_module()
    res = {"module_stats": stats, "core_sha256_before": before}
    res["baseline"] = classify()
    res["fault_in_abstract_layer_targetPort_8080"] = classify(lambda mw, aci: setattr(mw.MyWebApp_NetworkSpec, "hasTargetPort", 8080))
    res["aci_vcpu_8"] = classify(lambda mw, aci: setattr(aci.MyWebApp_ACI_Cpu, "hasCpuCores", 8.0))
    res["core_sha256_after"] = sha(CORE)
    res["core_unchanged"] = res["core_sha256_before"] == res["core_sha256_after"]
    with open(ROOT / "results" / "extensibility_aci.json", "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
