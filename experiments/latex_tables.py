"""Print the LaTeX rows of the scalability and linter tables from the JSON results.

Usage: python latex_tables.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from microwl_lib import ROOT

R = ROOT / "results"


def scalability_rows():
    rows = json.load(open(R / "scalability.json"))
    out = []
    for r in rows:
        sh = f"{r['shacl_mean_s']:.2f}" if r.get("shacl_mean_s") is not None else "--"
        out.append(f"{r['n']:,} & {r['abox_triples']:,} & {r['pellet_mean_s']:.2f} & {r['hybrid_mean_s']:.2f} & "
                   f"{sh} & {r['opa_mean_s']:.3f} \\\\".replace(",", "{,}"))
    return out


def linter_rows():
    res = json.load(open(R / "linters.json"))["detection"]
    cases = ["F1_rwo_replicas", "F2_targetport", "F3_nodeport_low", "F4_nodeport_high", "F5_rwo_zero"]
    out = []
    for tool, det in res.items():
        cells = " & ".join("$\\bullet$" if det[c] else "--" for c in cases)
        out.append(f"{tool} & {cells} \\\\")
    return out


if __name__ == "__main__":
    for name, fn in (("SCAL_ROWS", scalability_rows), ("LINTERS_ROWS", linter_rows)):
        try:
            print(f"% {name}")
            print("\n".join(fn()))
        except FileNotFoundError as e:
            print(f"% missing: {e.filename}")
