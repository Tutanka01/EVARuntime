# ADR-002 — Stratégie multi-runtime

- **Statut :** accepté pour la roadmap, détails d’implémentation à spécifier
- **Date :** 2026-09-03

## Contexte

EVARuntime est aujourd’hui couplé à `llama.cpp` et au format GGUF. Les besoins
futurs couvrent aussi les modèles Hugging Face/Safetensors, le batching soutenu,
le tensor parallel et les charges agentiques.

## Décision

- Conserver `llama.cpp` comme backend par défaut pour GGUF, les plateformes
  hétérogènes et les GPU partagés.
- Introduire d’abord une interface `BackendDriver` et un registre backend-neutral.
- Qualifier ensuite vLLM sur un nœud, puis son tensor parallel local.
- Évaluer SGLang comme backend expérimental pour cache de préfixes, agentique,
  MoE et structured output.
- Ne pas accepter un `extra_args` arbitraire dans le registre ou l’API admin.
- Chaque backend doit publier des capabilities et passer le même contrat
  OpenAI, SSE, erreurs, sécurité, métriques et lifecycle.

## Conséquences

L’intégration vLLM/SGLang commence par un travail d’architecture et de contrat,
pas par une branche conditionnelle dans `proxy.py`. Les artefacts GGUF et
Safetensors peuvent coexister pour un même modèle.
