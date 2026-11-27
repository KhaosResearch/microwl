# MicrOWL: artifacts for the ESWA paper

Everything needed to reproduce the results reported in the paper.

```
ontology/
  microwl.owl              MicrOWL knowledge base: TBox, SWRL rules (S1-S5, S7, S8a, S8b, S11,
                           S15, and the dependency-resolution rules D1 and D2) and case-study ABox
                           (built by experiments/build_microwl.py). The open-world SWRL
                           rule S2 is kept for comparison but annotated as disabled
                           (swrla:isRuleEnabled false); Pellet ignores the annotation, so
                           the hybrid validator reasons over microwl-tbox-noS2.owl and
                           evaluates S2 with the closed-world SHACL shape S2c instead
  microwl-ablated.owl      ablated variant without typed NodePort resources, storage scope
                           and target port (used in the ablation study)
  microwl-tbox.owl         microwl.owl without individuals (generated)
  microwl-aci.owl          Azure Container Instances module (built by extend_aci.py)
experiments/
  build_microwl.py         builds microwl.owl from the ablated variant; every design
                           decision is documented in the docstring
  microwl_lib.py           Config model (workloads, endpoints, Ingresses, deployment units),
                           Pellet/SHACL/OPA validators with rule attribution, hybrid validator,
                           explanations
  translate.py             Kubernetes / Docker Compose / ECS (task definitions and CloudFormation
                           service templates) translators: workloads, endpoints (Services, Compose
                           service names, ECS load balancers), Ingresses, dependencies read from
                           environment variables; coverage; config-level mutation operators
  build_shapes.py          generates the SHACL shape files in shapes/
  shapes/                  SHACL shapes: equivalent to the SWRL rules, closed-world variant of every
                           constraint, and the closed-world shapes of the hybrid validator
                           (S2c, S9, S10, S12, S13, S14, S16), which it evaluates over the graph
                           reasoned by Pellet (S9, S10 and S13 use the resolvesTo relations of D1);
                           every message reports the values involved
  rego/                    Rego policies (equivalent, closed-world)
  benchmark.py             RQ1 + RQ3 (synthetic benchmark: part A, 250 single-microservice
                           configurations; part B, 335 deployment units for S7-S16; hard negatives,
                           ablation, timings)
  real_world.py            RQ2 (public corpora, manual-inspection findings, mutation analysis)
  linters.py               RQ2 (kubeconform, kube-linter, kube-score, Checkov, and the Kubernetes
                           API server (server-side dry run and real apply) vs MicrOWL, fault
                           categories M1-M5 and M7-M16)
  explanations.py          RQ3 (size and time of the Pellet justifications of every element flagged
                           by the SWRL rules, per deployment unit; failure-impact query over the
                           dependsOn relations inferred for eShopOnContainers)
  rule_applicability.py    RQ2 (workloads on which each rule can fire, per platform)
  mutate_files.py          RQ2 (mutation analysis on the descriptor files, categories M1-M5 and
                           M7-M16: mutated manifests and templates are translated again and every
                           mutated deployment unit is validated; results/mutation_files.json).
                           real_world.py also reports an auxiliary mutation of the translated
                           configurations, which the paper does not use
  linter_cases/            faulty manifests and valid controls used by linters.py
  extend_aci.py            RQ4 (ACI module, core hash check)
  scalability.py           RQ5 (single-microservice configurations and deployment units; hybrid
                           validator over the whole ABox and per deployment unit, and an ablation
                           whose shapes resolve names in SPARQL over the asserted ABox)
  competency_questions.py  CQ1-CQ12 over the case-study knowledge base
  probe_ablated_nodeport.py  shows that the NodePort rules of the ablated variant never fire
  ontology_quality.py      local checks of the ontology for OOPS! pitfalls that can be detected from
                           the ontology itself (annotations, domain/range, naming, unconnected
                           elements, license) plus coherence and consistency with Pellet
  real_fixes.py            RQ2 (recall on real fix commits: downloads the deployment unit before
                           and after each commit of results/real_fixes_candidates.json with the
                           GitHub API and validates both versions; the manual review of every
                           commit that is not detected is recorded in the script)
  run_all.sh               runs the experiments in sequence
results/                   raw JSON results and logs produced by the scripts
  real_fixes_candidates.json  the 60 fix commits found by targeted searches (repository, commit,
                              parent, category, files, rationale) plus the 5 in-scope commits of
                              the generic sample that are not among them
  real_fixes_coverage.json    100 fix commits returned by generic searches, labeled by fault type
```

## Requirements

* Python 3.14 with `owlready2==0.47` (bundles Pellet 2.3.1; newer Owlready2 releases ship
  Jena classes that need Java 25), `rdflib`, `pyshacl==0.40.1`, `pyyaml`
* Java 21
* OPA 1.21.4 (`opa` on the PATH)
* Helm 4.3.1 (to render the DeathStarBench charts)
* For `linters.py`: kubeconform 0.8.0, kube-linter 0.8.3, kube-score 1.20.0, Checkov 3.4.3, and,
  for the API-server baselines, kind 0.33.0 (Kubernetes 1.37) with kubectl

## Running

The corpora must be checked out at the commits recorded in `results/real_world.json`:

```bash
fetch() { git init -q "corpora/$2" && git -C "corpora/$2" fetch -q --depth 1 "https://github.com/$1.git" "$3" && git -C "corpora/$2" checkout -q FETCH_HEAD; }
fetch GoogleCloudPlatform/microservices-demo       GoogleCloudPlatform_microservices-demo       38e7348eb289eb5b87c0c6e8cb19ced0449dc389
fetch microservices-demo/microservices-demo        microservices-demo_microservices-demo        9dff06fae4981921caec6a62393a6ebfce4b3e3f
fetch kubernetes/examples                          kubernetes_examples                          d6b8cd27eacb51e651a1aa6f7c190a28713eff6e
fetch docker/awesome-compose                       docker_awesome-compose                       30f4b7f6a6c3b0c0ecf4d4efb0de203c48d11562
fetch aws-samples/aws-containers-task-definitions  aws-samples_aws-containers-task-definitions  75aee9ed5a9d0c63880d1d2a3c03e462aa8f13b9
fetch aws-cloudformation/aws-cloudformation-templates awslabs_aws-cloudformation-templates    a0f43bc6d20813052892546f445037cf84c75b54
fetch aws-samples/ecs-refarch-cloudformation       aws-samples_ecs-refarch-cloudformation       a257e226b33bd9d2a721e5afd9d7e8b66dbacfdc
fetch nathanpeck/ecs-cloudformation                nathanpeck_ecs-cloudformation                6d274b43bbff12ba6960f0557270d2de85a0c8ce
fetch aws-samples/serverless-patterns              aws-samples_serverless-patterns              23b5a9ec6b25c30268bae70b334b0dffca74d7ee
fetch FudanSELab/train-ticket                      FudanSELab_train-ticket                      313886e99befb94be6cd45f085c98e0019f59829
fetch delimitrou/DeathStarBench                    delimitrou_DeathStarBench                    6ecb09706140f8730b5385c08f1386c654c3c526
fetch DescartesResearch/TeaStore                   DescartesResearch_TeaStore                   34b37f7e7be433ce72d5f9455e66922a13116749
fetch dotnet-architecture/eShopOnContainers        dotnet-architecture_eShopOnContainers        b6965936842cab32553543c1abe8a68714956f44
fetch spring-petclinic/spring-petclinic-microservices spring-petclinic_spring-petclinic-microservices e8b747ab522721076cacb5cf688eb8a54ba8c5c9
fetch istio/istio                                  istio_istio                                  dd6f272a9f0d5cbfe6edd36365def9d0f2bba5ed
mkdir -p corpora/rendered   # Helm 4.3.1
helm template dsb-socialnetwork corpora/delimitrou_DeathStarBench/socialNetwork/helm-chart/socialnetwork > corpora/rendered/dsb-socialnetwork.yaml
helm template dsb-mediamicroservices corpora/delimitrou_DeathStarBench/mediaMicroservices/helm-chart/mediamicroservices > corpora/rendered/dsb-mediamicroservices.yaml
python experiments/build_microwl.py ontology/microwl-ablated.owl ontology/microwl.owl
experiments/run_all.sh "$(which python)" "$PWD/corpora"
kind create cluster --name microwl                  # disposable cluster for the API-server baselines
python experiments/linters.py "$(which checkov)" kind-microwl   # run from experiments/
python experiments/explanations.py "$PWD/corpora"   # run from experiments/
python experiments/ontology_quality.py                # run from experiments/
python experiments/real_fixes.py "$PWD/fixes"        # run from experiments/; needs an authenticated gh CLI
python experiments/rule_applicability.py "$PWD/corpora"   # run from experiments/
python experiments/mutate_files.py "$PWD/corpora"         # run from experiments/, after real_world.py
```

## License

The MicrOWL ontology (`ontology/microwl.owl` and the files derived from it) is released under the
Creative Commons Attribution 4.0 International license (CC BY 4.0), as declared in the ontology
header (`dcterms:license`).
