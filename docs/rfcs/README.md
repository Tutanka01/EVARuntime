# RFC techniques

Les RFC sont des propositions de conception encore ouvertes. Elles doivent
indiquer le problème, les invariants à préserver, les alternatives, les risques,
la méthode d’évaluation et une recommandation provisoire.

Les premiers sujets attendus sont :

- contrat `BackendDriver` et registre multi-runtime ;
- vLLM contre SGLang pour les profils de charge EVA ;
- `ReplicaGroup` et gang scheduling ;
- attribution de l’énergie sous continuous batching ;
- snapshots KV et cache multi-tenant.

Une RFC n’est pas une promesse de livraison. Quand elle est acceptée, créer une
ADR courte et une issue GitHub actionnable ; quand elle est rejetée, conserver
la décision et sa raison pour éviter de rouvrir le même débat.
