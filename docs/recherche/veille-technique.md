# Veille technique — Moteur d'inférence & optimisations

> Synthèse des recherches menées sur le choix du moteur d'inférence et les évolutions
> à venir pour EVARuntime. Mise à jour : avril 2026.

---

## Table des matières

1. [vLLM vs llama.cpp — Pourquoi on reste sur llama.cpp](#1-vllm-vs-llamacpp--pourquoi-on-reste-sur-llamacpp)
2. [Maintenance llama.cpp — Quand et comment recompiler](#2-maintenance-llamacpp--quand-et-comment-recompiler)
3. [TurboQuant — La prochaine grande évolution du KV cache](#3-turboquant--la-prochaine-grande-évolution-du-kv-cache)

---

## 1. vLLM vs llama.cpp — Pourquoi on reste sur llama.cpp

### Contexte de l'analyse

La question a été posée : vaut-il mieux migrer vers **vLLM** (le moteur d'inférence le plus populaire en environnement cloud/data center) ou rester sur **llama.cpp** ?

La réponse est claire : **rester sur llama.cpp**. Pas par conservatisme, mais parce que le contexte d'EVARuntime rend les avantages de vLLM non pertinents et ses inconvénients rédhibitoires.

### Les contraintes d'EVARuntime qui décident tout

| Contrainte | Conséquence sur le choix du moteur |
|---|---|
| GPU partagé entre inférence et entraînement | Le moteur **doit** libérer 100% de la VRAM quand idle |
| ~100 utilisateurs, trafic académique intermittent | Pas de charge soutenue — le débit sous forte concurrence est secondaire |
| Modèles en format GGUF (Q4_K_M) | Le format doit être nativement supporté |
| Architecture subprocess (tuer/relancer à la demande) | Le temps de redémarrage après idle est critique |
| Efficacité énergétique (~30W vs ~350W) | Impossible sans libération réelle de la VRAM |

### Pourquoi vLLM ne convient pas à ce contexte

#### 1. La libération VRAM est impossible nativement dans vLLM

C'est le point bloquant absolu. vLLM **pré-alloue 90% de la VRAM totale dès le démarrage** et la conserve indéfiniment, même sans aucune requête active. C'est un choix de conception délibéré pour maximiser le débit.

Sur notre L40S 48 GB, vLLM monopoliserait ~43 GB en permanence dès le lancement — rendant impossible le partage du GPU avec des workloads d'entraînement.

La seule solution connue est de tuer le process group entier. Mais vLLM spawne plusieurs sous-processus workers CUDA (+ jusqu'à 32 `compile_worker`) : tuer uniquement le parent laisse des processus zombies qui tiennent toujours la VRAM. La gestion est nettement plus fragile que le subprocess unique de llama.cpp.

> **Sources GitHub :** Issues #1908, #6544, #23793, #15287 — tous des demandes de libération dynamique de VRAM, toutes marquées *stale* ou non implémentées.

#### 2. Le temps de redémarrage est 2 à 3 fois plus long

| Moteur | Redémarrage d'un modèle 70B (cache local chaud) |
|---|---|
| **llama.cpp** | **60–90 secondes** |
| **vLLM** | **2–5 minutes** (jusqu'à 1h+ si cache CUDA froid) |

Avec notre idle timeout de 5 minutes, les utilisateurs attendraient 2 à 5 minutes à chaque première requête après une période calme. Pour un outil académique, c'est une régression majeure de l'expérience utilisateur.

Le problème est structurel : vLLM compile des CUDA graphs et effectue des transformations Dynamo à chaque démarrage, même avec les fichiers déjà en cache local.

#### 3. Le format GGUF est expérimental et dégradé dans vLLM

Nos modèles sont en `.gguf` (Q4_K_M). Dans vLLM, le GGUF est officiellement documenté mais labelisé **"highly experimental and likely slower than other quantization types"**.

En pratique :
- GGUF dans vLLM : **~93 tok/s**
- AWQ/Marlin (format natif vLLM) : **~741 tok/s**

Soit 8x plus lent que le format natif de vLLM. Pour profiter vraiment de vLLM, il faudrait re-télécharger tous les modèles au format AWQ — une migration non-triviale qui casse la compatibilité avec les fichiers GGUF existants.

#### 4. L'avantage principal de vLLM ne s'applique pas ici

vLLM brille sous **forte concurrence soutenue** grâce à PagedAttention et au batching itératif. Les gains sont réels :

| Concurrence | Débit vLLM | Débit llama.cpp |
|---|---|---|
| 10 utilisateurs simultanés | ~485 tok/s | ~148 tok/s |
| 50 utilisateurs simultanés | ~920 tok/s | ~155 tok/s |

Mais notre contexte, c'est 4 slots parallèles pour ~100 utilisateurs avec un trafic académique **intermittent**. On n'a jamais 50 requêtes en parallèle de façon soutenue. L'avantage de vLLM n'entre jamais vraiment en jeu.

### Tableau de décision

| Critère | llama.cpp | vLLM | Gagnant pour EVARuntime |
|---|---|---|---|
| Libération VRAM 100% à l'idle | Natif, fiable | Process kill complexe, instable | **llama.cpp** |
| Temps de redémarrage (70B) | 60–90s | 2–5 min | **llama.cpp** |
| Support GGUF natif | Excellent | Expérimental, ~8x plus lent | **llama.cpp** |
| Débit sous forte concurrence | Bon | 3–6x meilleur | vLLM (non pertinent ici) |
| Gestion subprocess | Simple (1 processus) | Complexe (multi-workers) | **llama.cpp** |
| VRAM overhead | Minimal (modèle + KV cache) | ~90% pré-alloué au démarrage | **llama.cpp** |
| GPU partagé avec entraînement | Parfaitement adapté | Architecture inadaptée | **llama.cpp** |

### Quand reconsidérer vLLM ?

Un seul scénario justifierait la migration :

> L'UPPA acquiert un **GPU dédié exclusivement à l'inférence** (pas partagé avec l'entraînement), et l'usage passe à **20–50+ utilisateurs simultanés en continu**.

Dans ce cas, et seulement dans ce cas, il faudrait : télécharger les modèles au format AWQ, réécrire le `ServerManager` pour gérer un process group vLLM, et accepter les 2–5 min de démarrage (qui n'auraient plus d'impact puisque le serveur ne serait jamais tué).

---

## 2. Maintenance llama.cpp — Quand et comment recompiler

### Pourquoi recompiler ?

llama.cpp est un projet en développement très actif (releases hebdomadaires). Pour un usage production stable, on ne suit pas le HEAD en continu. On recompile pour des raisons précises :

| Raison | Exemple concret | Quand agir |
|---|---|---|
| **Nouveau type de quantisation KV cache** | TurboQuant (`tq3_0`), nouveaux types IQ | Dès que mergé et testé |
| **Nouveau modèle non supporté** | Architecture Llama 4, Qwen3, etc. | Avant d'adopter le modèle |
| **Fix de performance GPU** | Optimisation CUDA Ada Lovelace, fix Flash Attention | Quand le fix cible notre matériel |
| **Fix de sécurité critique** | Rare, mais possible | Immédiatement |

### Fréquence recommandée

**Toutes les 8 à 12 semaines** en routine. Pas besoin de suivre chaque release.

Exceptions qui déclenchent une recompilation immédiate :
- TurboQuant mergé upstream (voir section 3)
- Un modèle qu'on souhaite déployer nécessite une architecture non supportée
- Un bug CUDA spécifique au L40S est corrigé

### Procédure de recompilation

```bash
# 1. Récupérer les dernières sources
cd /opt/llama.cpp
git pull

# 2. Recompiler avec les mêmes flags qu'à l'installation
#    (Ada Lovelace = compute capability 8.9 = L40S)
cmake --build build --config Release -j$(nproc)

# 3. Installer le nouveau binaire
sudo cp build/bin/llama-server /usr/local/bin/

# 4. Redémarrer la gateway
sudo systemctl restart llm-gateway
```

Aucune donnée n'est perdue : la base SQLite, `models.yaml` et `/etc/llm-gateway/env` ne sont jamais touchés. Les modèles GGUF restent en place. La gateway redémarre et les modèles se rechargent à la prochaine requête.

> **Conseil :** avant de `git pull` en production, consulter les [releases GitHub](https://github.com/ggml-org/llama.cpp/releases) pour repérer d'éventuels changements de flags CLI. Ces breaking changes sont rares mais existent (ex: renommage de `--n-gpu-layers` en `-ngl`).

---

## 3. TurboQuant — La prochaine grande évolution du KV cache

### Qu'est-ce que TurboQuant ?

**TurboQuant** est un algorithme de compression publié par Google Research (arXiv : avril 2025, présenté à ICLR 2026). Il s'attaque au KV cache — le principal goulot d'étranglement mémoire pendant l'inférence des LLMs à long contexte.

L'idée centrale : au lieu de quantifier les vecteurs K/V directement comme le font `q8_0` ou `q4_0`, TurboQuant applique d'abord une **rotation aléatoire fixe** (Walsh-Hadamard Transform) qui uniformise la distribution des valeurs, puis quantifie avec un codebook scalaire pré-calculé. Résultat : compression extrême **sans entraînement, sans calibration**, applicable en ligne pendant le décodage.

```
Approche classique (q8_0) :  K/V → quantifier directement → 8 bits/valeur
TurboQuant (tq3_0)        :  K/V → rotation WHT → quantifier → ~3.5 bits/valeur
```

### Les chiffres de compression

| Format | Bits/valeur | Compression vs FP16 | MSE | Disponibilité |
|---|---|---|---|---|
| `f16` (FP16) | 16 | 1x (référence) | 0 | Disponible |
| `q8_0` | 8 | ~2x | faible | **Disponible — utilisé actuellement** |
| `q4_0` | 4 | ~4x | modéré | Disponible |
| `tq4_0` (TurboQuant 4-bit) | ~4 | **3.8x** | 0.009 | En cours d'intégration |
| `tq3_0` (TurboQuant 3-bit) | ~3.5 | **4.9x** | 0.034 | En cours d'intégration |
| Mixed 3-bit + outliers 8-bit | ~3 | **jusqu'à 9x** | ~0 PPL | Expérimental |

TurboQuant atteint une efficacité proche de la **limite théorique de Shannon** — autrement dit, on approche du maximum de compression sans perte d'information pour ce type de données.

### État d'intégration dans llama.cpp (avril 2026)

La situation est active mais pas encore finalisée dans le main upstream :

- **Discussion #20969** (llama.cpp officiel) : thread ouvert sur l'intégration TurboQuant, avec une implémentation C complète (CPU, zéro dépendance), 18/18 tests passants, MSE matchant le paper à 1% près.
- **PR #21010** (backend Vulkan) : ajout du type `GGML_TYPE_TQ3_0`, soumis puis **fermé pour raison de politique** (code généré par IA sans review humaine suffisante). La porte n'est pas fermée — une soumission propre est attendue.
- **Fork `turboquant_plus`** : implémentation Metal/Apple Silicon fonctionnelle avec `-ctk turbo3 -ctv turbo3`, validée de 1.5B à 104B paramètres. Un modèle 104B tourne à 128K de contexte sur MacBook avec turbo3.
- **Plan d'intégration upstream** documenté en 6 phases : enregistrement type GGML, chemins KV read/write, intégration Flash Attention, flags CLI.

**Pas encore mergé upstream, mais ça arrive.** Le code de référence existe et fonctionne.

### Impact concret pour EVARuntime

Notre configuration actuelle sur le 70B (4 slots parallèles, contexte 8K, `q8_0`) :

```
KV cache actuel ≈ 2.7 GB
```

Avec `tq3_0` une fois disponible :

```
KV cache tq3_0 ≈ 0.55 GB  (−80%)
```

Ce que ça libère comme options, au choix :

| Option | Avec q8_0 (actuel) | Avec tq3_0 |
|---|---|---|
| Slots parallèles (contexte 8K fixe) | 4 | ~16 |
| Contexte par slot (4 slots fixe) | 8 192 tokens | ~40 000 tokens |
| Contexte total du pool | 32 768 tokens | ~160 000 tokens |

**Pour notre usage académique**, le gain le plus pertinent n'est pas d'avoir 16 slots simultanés (le trafic ne le justifie pas), mais de **proposer un contexte beaucoup plus long** aux chercheurs et doctorants :

- Analyser un article scientifique entier sans le tronquer
- Travailler sur une base de code volumineuse
- Mener des sessions de travail longues sans perte de contexte

Passer de 8K à 32–40K tokens de contexte par utilisateur serait une amélioration qualitative significative, sans coût VRAM supplémentaire.

### Comment activer TurboQuant (une fois mergé upstream)

La migration est **triviale** : recompiler llama.cpp, puis modifier deux lignes dans `models.yaml` :

```yaml
# models.yaml — modifier pour chaque modèle concerné
llama_params:
  cache_type_k: "tq3_0"   # remplace "q8_0"
  cache_type_v: "tq3_0"
```

Aucun changement d'architecture, aucune migration de base de données, aucun changement de format de modèle. Le `.gguf` reste identique — seul le KV cache en mémoire est affecté.

```bash
# Utilisation CLI directe (pour tester)
llama-server -m /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf \
  -ngl 999 -c 131072 --parallel 4 \
  --cache-type-k tq3_0 --cache-type-v tq3_0 \
  --flash-attn
```

### À surveiller

Le thread à suivre : **[Discussion #20969 sur le dépôt llama.cpp](https://github.com/ggml-org/llama.cpp/discussions/20969)**.

Dès qu'un PR TurboQuant passe la review et est mergé sur `main`, la procédure est :
1. `git pull` + recompiler llama.cpp (voir section 2)
2. Tester en dev avec `-ctk tq3_0 -ctv tq3_0` sur le 70B
3. Comparer la perplexité et les performances avec `q8_0`
4. Si concluant, mettre à jour `models.yaml` en production

---

## Résumé exécutif

| Sujet | Décision / État | Action |
|---|---|---|
| vLLM vs llama.cpp | **Rester sur llama.cpp** — les avantages de vLLM ne s'appliquent pas à notre contexte | Aucune migration à prévoir |
| Fréquence de recompilation | **Toutes les 8–12 semaines** en routine | Planifier une recompilation trimestrielle |
| TurboQuant | **Pas encore upstream** — implémentations communautaires fonctionnelles | Surveiller Discussion #20969, migrer dès le merge |
| Gain TurboQuant attendu | KV cache −80%, contexte × 5 (de 8K à ~40K tokens) | Priorité haute dès disponibilité |

---

*Sources :*
- [Google Research Blog — TurboQuant](https://research.google/blog/turboquant-redefining-ai-efficiency-with-extreme-compression/)
- [llama.cpp Discussion #20969 — TurboQuant integration](https://github.com/ggml-org/llama.cpp/discussions/20969)
- [llama.cpp PR #21010 — Vulkan TQ3_0](https://github.com/ggml-org/llama.cpp/pull/21010)
- [VentureBeat — Google's TurboQuant, 8x memory speedup](https://venturebeat.com/infrastructure/googles-new-turboquant-algorithm-speeds-up-ai-memory-8x-cutting-costs-by-50)
- [The Kaitchup — TurboQuant: Finally, Fast and Widely Available Low-Bit KV Cache Quantization?](https://kaitchup.substack.com/p/turboquant-finally-fast-and-widely)
- [Red Hat Developer — vLLM or llama.cpp: Choosing the right LLM inference engine](https://developers.redhat.com/articles/2025/09/30/vllm-or-llamacpp-choosing-right-llm-inference-engine-your-use-case)
