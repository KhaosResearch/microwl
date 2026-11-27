"""Check whether the NodePort rules (S3a/S3b) of the ablated variant fire.
The nodePort value is rewritten directly in the RDF (as xsd:string, as stored in
the ablated variant, and as xsd:integer) and Pellet is run on each variant."""
import sys, tempfile, os
import rdflib
from rdflib import Literal, URIRef, XSD
from owlready2 import World, sync_reasoner_pellet
src = sys.argv[1]
MW = "http://www.khaos.uma.es/microwl/"
NP, RV = URIRef(MW + "MyWebApp_K8s_NodePort"), URIRef(MW + "resourceValue")
for label, lit in [("stored 30080 (string)", Literal("30080")),
                   ("8080 as xsd:string", Literal("8080")),
                   ("8080 as xsd:integer", Literal(8080, datatype=XSD.integer)),
                   ("40000 as xsd:integer", Literal(40000, datatype=XSD.integer))]:
    g = rdflib.Graph(); g.parse(src, format="xml")
    g.remove((NP, RV, None)); g.add((NP, RV, lit))
    fd, tmp = tempfile.mkstemp(suffix=".owl"); os.close(fd)
    g.serialize(tmp, format="xml")
    w = World(); onto = w.get_ontology("file://" + tmp).load()
    ns = onto.get_namespace(MW)
    try:
        with onto:
            sync_reasoner_pellet(w, infer_property_values=True, infer_data_property_values=True, debug=0)
        print(f"{label:28s} NodePortOutOfRange={[i.name for i in ns.NodePortOutOfRange.instances()]} "
              f"Inconsistent={[i.name for i in ns.InconsistentDeployment.instances()]}")
    except Exception as e:
        print(f"{label:28s} reasoner error: {str(e).splitlines()[-1][:160]}")
    os.remove(tmp)
