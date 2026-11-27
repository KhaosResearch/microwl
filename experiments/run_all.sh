#!/usr/bin/env bash
# Run every MicrOWL experiment in sequence. Usage: ./run_all.sh <python> <corpora_dir>
set -euo pipefail
PY="$1"; CORPORA="$2"; FROM="${3:-benchmark}"
cd "$(dirname "$0")"
run() { "$PY" -I -c "import sys; sys.path.insert(0,'.'); import runpy; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')" "$@"; }
"$PY" -I -c "import sys; sys.path.insert(0,'.'); from microwl_lib import make_tbox, ROOT; make_tbox(ROOT/'ontology/microwl.owl', ROOT/'ontology/microwl-tbox.owl')"
[[ "$FROM" == benchmark ]] && run benchmark.py            > ../results/benchmark.log 2>&1;            echo BENCH_OK
[[ "$FROM" =~ ^(benchmark|real_world)$ ]] && run real_world.py "$CORPORA" > ../results/real_world.log 2>&1;          echo REAL_OK
run extend_aci.py           > ../results/extensibility_aci.log 2>&1;    echo ACI_OK
run competency_questions.py > ../results/competency_questions.log 2>&1; echo CQ_OK
run scalability.py          > ../results/scalability.log 2>&1;          echo SCAL_OK
