# EVARuntime — vision produit

Ce document résume la direction produit. Le plan détaillé, les preuves, les
limites et les jalons sont dans [ROADMAP.md](../ROADMAP.md).

## Ambition

Faire d’EVARuntime la référence pragmatique pour servir des LLM privés sur une
flotte de GPU partagés : un système qui sait dire où se trouve un modèle, dans
quel état il se trouve, quelle mémoire il occupe, combien de temps il faudra
pour le réveiller et combien d’énergie sa réponse a coûté.

## Différenciation

EVARuntime ne cherche pas à remplacer les moteurs d’inférence. Il les
supervise dans un plan de contrôle local-first qui conserve :

- la souveraineté des prompts, modèles et journaux ;
- une gestion explicite des états et des requêtes actives ;
- la libération effective des ressources lorsque la demande disparaît ;
- une chaîne d’approvisionnement vérifiable ;
- des quotas, une gouvernance multi-utilisateur et un audit sans contenu privé ;
- des mesures de latence, de capacité et d’énergie comparables entre moteurs.

## Trois piliers

### Warm Fleet

Conserver les sessions et préfixes utiles sans conserver inutilement les poids
sur le GPU : snapshots KV optionnels, restore vérifié, cache-aware routing et
session pinning. Cette fonctionnalité reste expérimentale tant que les formats
de snapshot, la confidentialité multi-tenant et les coûts disque ne sont pas
qualifiés.

### Tokens par watt

Faire de l’énergie une métrique de service, au même titre que TTFT et le débit :
puissance idle, énergie par runtime, estimation par requête, joules par token,
température et throttling. Les compteurs bruts par GPU sont conservés ; toute
attribution à une requête batchée est explicitement présentée comme une
estimation.

### Local-first explicite

Le trafic reste local par défaut. Une escalade externe éventuelle doit être
opt-in, contrôlée par politique, plafonnée en coût et inscrite dans l’audit.
EVARuntime ne devient pas un proxy universel multi-fournisseurs.

## Moteurs

- **llama.cpp** reste le backend par défaut pour GGUF, les machines
  hétérogènes et les GPU partagés.
- **vLLM** est le premier backend secondaire à qualifier pour Safetensors,
  le batching soutenu et le tensor parallel mono-nœud.
- **SGLang** sera évalué pour les charges agentiques, le cache de préfixes,
  les MoE et les optimisations avancées.
- **TensorRT-LLM/Triton** reste un spike spécialisé NVIDIA, pas le socle.

La compatibilité OpenAI est une responsabilité d’EVARuntime : chaque backend
doit passer un contrat commun sur les erreurs, le streaming, les tools, le
reasoning, les sorties structurées et les métriques.

## Public prioritaire

1. universités et laboratoires partageant des GPU entre entraînement et
   inférence ;
2. organisations publiques ou souveraines soumises à des exigences de
   résidence et d’audit ;
3. équipes plateforme de PME disposant de quelques GPU et souhaitant éviter
   une pile Kubernetes lourde.

## Ce que le projet ne promet pas encore

Le chemin logiciel llama.cpp est largement testé. La preuve complète sur GPU
physique, le comptage énergétique, vLLM, les réplicas et le sharding d’un grand
modèle sur plusieurs nœuds sont des jalons à venir. Cette distinction doit
rester visible dans le README, les releases et les benchmarks.
