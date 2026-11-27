"""Quality checks of the MicrOWL ontology (TBox), run locally.

The checks follow pitfalls of the OOPS! catalogue (Poveda-Villalon et al., 2014) that can be
detected from the ontology alone:
  P04  unconnected ontology elements (classes and properties not used in any axiom besides their declaration)
  P08  missing annotations (classes and properties without rdfs:comment or rdfs:label)
  P11  missing domain or range in properties
  P13  inverse relationships not explicitly declared (object properties whose inverse exists in the
       vocabulary but is not declared with owl:inverseOf) -- reported as object properties with an inverse
  P22  inconsistent naming (classes not in UpperCamelCase, properties not in lowerCamelCase)
  P41  no license declared
plus coherence (unsatisfiable classes) and consistency of the TBox with the case-study ABox,
checked with Pellet.

Usage: python ontology_quality.py
"""
import json
import re

import rdflib
from rdflib import OWL, RDF, RDFS

from microwl_lib import ROOT

ONTO = ROOT / "ontology" / "microwl.owl"
MW = "http://www.khaos.uma.es/microwl/"
DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")


def local(u):
    return re.split(r"[/#]", str(u))[-1]


def main():
    g = rdflib.Graph()
    g.parse(str(ONTO), format="xml")
    own = lambda u: isinstance(u, rdflib.URIRef) and str(u).startswith(MW)
    classes = sorted({c for c in g.subjects(RDF.type, OWL.Class) if own(c)})
    oprops = sorted({p for p in g.subjects(RDF.type, OWL.ObjectProperty) if own(p)})
    dprops = sorted({p for p in g.subjects(RDF.type, OWL.DatatypeProperty) if own(p)})
    props = oprops + dprops
    entities = classes + props

    annotated = [e for e in entities if (e, RDFS.comment, None) in g or (e, RDFS.label, None) in g]
    no_domain = [p for p in props if (p, RDFS.domain, None) not in g]
    no_range = [p for p in props if (p, RDFS.range, None) not in g]
    with_inverse = [p for p in oprops if (p, OWL.inverseOf, None) in g or (None, OWL.inverseOf, p) in g]

    def used(e):
        # used in any triple other than its own declaration and annotations
        for s, p, o in g.triples((None, None, e)):
            if p not in (RDF.type,) or s != e:
                return True
        for s, p, o in g.triples((e, None, None)):
            if p not in (RDF.type, RDFS.comment, RDFS.label):
                return True
        return False

    unconnected = [e for e in entities if not used(e)]
    bad_class_names = [c for c in classes if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", local(c))]
    bad_prop_names = [p for p in props if not re.fullmatch(r"[a-z][A-Za-z0-9]*", local(p))]
    ont = next(g.subjects(RDF.type, OWL.Ontology))
    license_ = g.value(ont, DCTERMS.license)

    # coherence and consistency with Pellet
    from owlready2 import World, sync_reasoner_pellet
    w = World()
    onto = w.get_ontology("file://" + str(ONTO)).load()
    consistent, unsat = True, []
    try:
        with onto:
            sync_reasoner_pellet(w, infer_property_values=True, debug=0)
        unsat = sorted(c.name for c in w.inconsistent_classes())
    except Exception as e:  # inconsistent ontology
        consistent = False
        unsat = [str(e).splitlines()[-1]]

    res = dict(
        classes=len(classes), object_properties=len(oprops), datatype_properties=len(dprops),
        annotated=f"{len(annotated)}/{len(entities)}",
        not_annotated=sorted(local(e) for e in entities if e not in annotated),
        properties_without_domain=sorted(local(p) for p in no_domain),
        properties_without_range=sorted(local(p) for p in no_range),
        object_properties_with_inverse=len(with_inverse),
        unconnected=sorted(local(e) for e in unconnected),
        naming_classes=sorted(local(c) for c in bad_class_names),
        naming_properties=sorted(local(p) for p in bad_prop_names),
        license=str(license_) if license_ else None,
        consistent_with_case_study=consistent, unsatisfiable_classes=unsat,
    )
    with open(ROOT / "results" / "ontology_quality.json", "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
