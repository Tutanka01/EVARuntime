# Guide utilisateur — API du Cluster EVA

Ce guide s'adresse aux doctorants, enseignants-chercheurs, ingénieurs et stagiaires
qui souhaitent exploiter le service d'inférence de grands modèles de langage (LLM)
du cluster EVA depuis leurs scripts, notebooks ou applications.

L'API est **entièrement compatible avec le standard OpenAI** : tout code existant
utilisant `openai-python`, LangChain, LiteLLM ou `curl` fonctionnera sans modification,
en changeant uniquement l'URL de base et la clé d'authentification.

> **Contacts principaux**
> - **Mohamad El Akhal El Bouzidi** — responsable technique du service
> - **Benjamin Mascret** — administrateur système et infrastructure

---

## Table des matières

1. [Obtenir une clé API](#1-obtenir-une-clé-api)
2. [Premier test — curl](#2-premier-test--curl)
3. [Python — openai-python](#3-python--openai-python)
4. [Python — requêtes HTTP directes (httpx)](#4-python--requêtes-http-directes-httpx)
5. [Streaming](#5-streaming)
6. [Paramètres de génération](#6-paramètres-de-génération)
   - [6.1 Paramètres standard OpenAI](#61-paramètres-standard-openai)
   - [6.2 Paramètres avancés de sampling (llama.cpp)](#62-paramètres-avancés-de-sampling-llamacpp)
7. [Endpoints natifs llama.cpp](#7-endpoints-natifs-llamacpp)
8. [Intégration LangChain](#8-intégration-langchain)
9. [JavaScript / Node.js](#9-javascript--nodejs)
10. [Comportement au premier appel](#10-comportement-au-premier-appel)
11. [Codes d'erreur et solutions](#11-codes-derreur-et-solutions)
12. [Limites et quotas](#12-limites-et-quotas)
13. [Exemples complets par cas d'usage](#13-exemples-complets-par-cas-dusage)

---

## 1. Obtenir une clé API

L'accès au service est nominatif. Pour en bénéficier, contactez l'un des responsables
du service en précisant votre nom, votre adresse email institutionnelle et le cadre
d'utilisation prévu (thèse, enseignement, projet de recherche, stage, etc.).

Vous recevrez une clé au format suivant :
```
llmgw-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

> **Important :** la clé brute vous est transmise **une seule fois** et n'est jamais
> stockée côté serveur (seule son empreinte SHA-256 est conservée). En cas de perte,
> il faudra en générer une nouvelle et révoquer l'ancienne.

### Règles de sécurité

La clé API est un secret d'authentification personnel. Les règles suivantes sont
impératives :

- Ne **jamais** la committer dans un dépôt Git, même privé
- Ne **jamais** la partager par email ou messagerie non chiffrée
- Ne **jamais** la placer en dur dans le code source
- Ne **jamais** la publier dans un notebook Jupyter partagé

### Bonne pratique — stocker la clé hors du code

La méthode recommandée est d'utiliser une variable d'environnement :

```bash
# À ajouter dans ~/.bashrc ou ~/.zshrc
export UPPA_LLM_KEY="llmgw-votre_cle_ici"

# Pour un projet Python : utiliser un fichier .env exclu du contrôle de version
echo "UPPA_LLM_KEY=llmgw-votre_cle_ici" >> .env
echo ".env" >> .gitignore
```

En Python, la clé se lit simplement ainsi, sans jamais l'écrire dans le code :

```python
import os
api_key = os.environ["UPPA_LLM_KEY"]
```

---

## 2. Premier test — curl

Avant d'écrire un script Python complet, il est utile de vérifier que le service
répond et d'explorer les modèles disponibles. L'outil `curl` est idéal pour cela.

### Vérifier l'état du service

Cet endpoint ne requiert pas d'authentification et indique si le service est opérationnel,
quels modèles sont actuellement en mémoire GPU et combien de VRAM est disponible.

```bash
curl -s https://llm.eva.univ-pau.fr/health
```

Réponse attendue :
```json
{
  "status": "ok",
  "models_loaded": [],
  "vram_used_gb": 0.0,
  "vram_available_gb": 43.6
}
```

`/health` est une sonde de **liveness** : elle confirme seulement que le process
répond. Un service `ok` sans modèle chargé reste capable de servir une requête
(le modèle chargera à la demande).

### Vérifier la disponibilité — `/ready`

`GET /ready` (non authentifié) est une sonde de **readiness** distincte : elle
indique si la gateway peut effectivement **servir** au moins une requête
d'inférence maintenant. Utile pour un load balancer ou un script qui veut éviter
d'envoyer du trafic à une instance saturée.

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://llm.eva.univ-pau.fr/ready
```

- **`200`** — au moins un modèle est déjà chargé, **ou** il reste de la capacité
  pour en charger un (mode local) / au moins un nœud est online (mode cluster) :

  ```json
  {"status": "ready", "models_ready": ["qwen3.5-9b-q5_k_m"], "vram_available_gb": 12.4}
  ```

- **`503`** — aucun modèle prêt **et** aucune capacité (ou tous les nœuds
  offline). Le corps précise la raison (`no_model_ready_and_no_capacity` ou
  `all_nodes_offline`) sans divulguer d'infrastructure :

  ```json
  {"status": "not_ready", "models_ready": [], "vram_available_gb": 0.0,
   "reason": "no_model_ready_and_no_capacity"}
  ```

### Lister les modèles disponibles

```bash
curl -s https://llm.eva.univ-pau.fr/v1/models \
  -H "Authorization: Bearer $UPPA_LLM_KEY" | python3 -m json.tool
```

### Voir l'état de la queue VRAM

Cet endpoint est authentifié avec une clé API utilisateur normale. Il expose
uniquement l'état minimal de la queue d'admission, sans révéler les modèles
chargés, les chemins fichiers ou le détail VRAM.

```bash
curl -s https://llm.eva.univ-pau.fr/v1/capacity \
  -H "Authorization: Bearer $UPPA_LLM_KEY" | python3 -m json.tool
```

Réponse exemple :

```json
{
  "object": "capacity_queue",
  "mode": "local",
  "available": true,
  "enabled": true,
  "status": "idle",
  "waiters": 0,
  "max_waiters": 100,
  "timeout_seconds": 120,
  "retry_after_seconds": 10
}
```

`status` vaut `idle`, `waiting`, `full`, `disabled` ou `unavailable`.

### Première requête de génération

```bash
curl -s https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "messages": [
      {"role": "user", "content": "Explique le théorème de Bayes en 3 phrases."}
    ]
  }' | python3 -m json.tool
```

Réponse attendue :
```json
{
   "id": "chatcmpl-abc123",
   "object": "chat.completion",
   "model": "qwen3.5-9b-q5_k_m",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Le théorème de Bayes décrit comment mettre à jour..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 28,
    "completion_tokens": 87,
    "total_tokens": 115
  }
}
```

> **Première requête lente ?** C'est normal. Si le modèle n'est pas encore en mémoire,
> il lui faut 60 à 90 secondes pour se charger (modèle 70B) ou 10 à 20 secondes (8B).
> Les requêtes suivantes seront nettement plus rapides. Ce mécanisme est détaillé
> à la [section 10](#10-comportement-au-premier-appel).

---

## 3. Python — openai-python

La bibliothèque officielle `openai` est la méthode recommandée pour Python. Elle gère
automatiquement la sérialisation JSON, le streaming et la gestion de base des erreurs.
La reconfigurer pour pointer sur le cluster EVA ne demande qu'un seul changement.

### Installation

```bash
pip install openai
```

### Configuration du client

```python
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",  # seul changement par rapport à OpenAI
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,  # le modèle peut mettre jusqu'à 90s à se charger la première fois
)
```

### Requête simple

```python
response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[
        {"role": "system", "content": "Tu es un assistant de recherche scientifique."},
        {"role": "user",   "content": "Quelles sont les principales limites des LLMs actuels ?"}
    ],
)

print(response.choices[0].message.content)
print(f"\n--- Tokens utilisés : {response.usage.total_tokens} ---")
```

### Choisir le bon modèle

Le champ `model` détermine quel modèle traite la requête. Pour connaître les modèles
actuellement activés :

```python
models = client.models.list()
for m in models.data:
    print(m.id)
```

Le choix dépend du compromis qualité/latence souhaité :

```python
# Modèle principal — qualité maximale, temps de chargement ~90s
response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[{"role": "user", "content": "Analyse en détail ce texte..."}],
)

# Modèle léger — latence réduite, chargement ~15s, suffisant pour de nombreuses tâches
response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[{"role": "user", "content": "Résume en 2 phrases."}],
)
```

### Conversation multi-tours

Les LLMs sont des modèles *stateless* : ils ne mémorisent rien entre deux requêtes.
Pour maintenir un contexte conversationnel, il faut transmettre l'historique complet
à chaque appel.

```python
def chat(messages: list[dict], model: str = "qwen3.5-9b-q5_k_m") -> str:
    """Envoie une conversation et retourne la réponse du modèle."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
        temperature=0.7,
    )
    return response.choices[0].message.content


# Construction de la conversation
conversation = [
    {"role": "system", "content": "Tu es un expert en traitement du langage naturel."}
]

# Tour 1
conversation.append({"role": "user", "content": "Qu'est-ce qu'un transformer ?"})
reply = chat(conversation)
conversation.append({"role": "assistant", "content": reply})
print("Assistant :", reply)

# Tour 2 — le modèle voit l'intégralité de l'échange précédent
conversation.append({"role": "user", "content": "Et le mécanisme d'attention ?"})
reply = chat(conversation)
print("Assistant :", reply)
```

### Gestion robuste des erreurs

Dans un pipeline de traitement automatique, les erreurs passagères sont inévitables :
queue VRAM saturée ou expirée (503), limite de débit dépassée (429), perte réseau
transitoire. En fonctionnement normal, la gateway attend déjà le chargement ou la
libération VRAM avant de répondre ; un 503 signifie que l'attente bornée a expiré
ou que la queue est pleine.

```python
import time
from openai import OpenAI, APIStatusError, APITimeoutError, APIConnectionError

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
    max_retries=0,  # on gère les relances manuellement pour plus de contrôle
)


def query_with_retry(
    messages: list[dict],
    model: str = "qwen3.5-9b-q5_k_m",
    max_attempts: int = 3,
) -> str:
    """
    Envoie une requête avec relances automatiques.

    Gère spécifiquement :
    - 503 : queue VRAM pleine/expirée ou backend temporairement indisponible
    - 429 : la limite de débit est atteinte — attendre 60s
    - timeout réseau — attendre et réessayer
    """
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=2048,
            )
            return response.choices[0].message.content

        except APIStatusError as e:
            if e.status_code == 503:
                wait = int(e.response.headers.get("Retry-After", 30 * attempt))
                print(f"Service temporairement indisponible, attente {wait}s (tentative {attempt}/{max_attempts})…")
                time.sleep(wait)
            elif e.status_code == 429:
                print("Limite de débit atteinte, attente 60s…")
                time.sleep(60)
            elif e.status_code == 401:
                raise ValueError("Clé API invalide ou révoquée.") from e
            elif e.status_code == 404:
                raise ValueError(f"Modèle '{model}' inconnu — vérifier GET /v1/models.") from e
            elif e.status_code == 403:
                raise ValueError(f"Modèle '{model}' désactivé par l'administrateur.") from e
            else:
                raise

        except APITimeoutError:
            print(f"Timeout réseau (tentative {attempt}/{max_attempts})…")
            time.sleep(10 * attempt)

        except APIConnectionError:
            print(f"Serveur injoignable (tentative {attempt}/{max_attempts})…")
            time.sleep(5 * attempt)

    raise RuntimeError(f"Échec après {max_attempts} tentatives.")


# Utilisation
result = query_with_retry([{"role": "user", "content": "Résume ce paragraphe : …"}])
print(result)
```

---

## 4. Python — requêtes HTTP directes (httpx)

Si vous préférez ne pas dépendre de la bibliothèque `openai`, vous pouvez effectuer
des requêtes HTTP directement. La bibliothèque `httpx` est recommandée : elle supporte
aussi bien le mode synchrone qu'asynchrone, et offre une gestion du timeout plus fine.

```python
import httpx
import os

API_URL = "https://llm.eva.univ-pau.fr/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {os.environ['UPPA_LLM_KEY']}",
    "Content-Type": "application/json",
}


def ask(
    prompt: str,
    model: str = "qwen3.5-9b-q5_k_m",
    system: str = "Tu es un assistant utile.",
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(API_URL, json=payload, headers=HEADERS)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


print(ask("Quelle est la capitale de la France ?"))
```

---

## 5. Streaming

Le streaming permet de recevoir la réponse **token par token**, comme sur une interface
conversationnelle classique. Il est particulièrement utile pour les longues générations,
car l'utilisateur perçoit une réactivité immédiate plutôt qu'un long silence suivi d'un
bloc de texte.

### Python — openai-python

```python
# Affiche chaque token dès sa génération
stream = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[{"role": "user", "content": "Rédige une introduction sur les transformers."}],
    stream=True,
    max_tokens=1024,
)

for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)

print()  # retour à la ligne en fin de génération
```

### Streaming avec collecte du résultat complet

```python
def stream_and_collect(
    messages: list[dict],
    model: str = "qwen3.5-9b-q5_k_m",
) -> str:
    """Affiche les tokens au fil de la génération et retourne le texte complet."""
    full_text = ""
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="", flush=True)
        full_text += delta
    print()
    return full_text


text = stream_and_collect([
    {"role": "user", "content": "Liste 5 avantages du RAG (Retrieval-Augmented Generation)."}
])
# Le texte intégral est maintenant disponible dans `text`
```

### Streaming asynchrone (FastAPI / asyncio)

Pour les applications web ou tout code basé sur `asyncio` :

```python
import asyncio
from openai import AsyncOpenAI

async_client = AsyncOpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


async def stream_response(prompt: str, model: str = "qwen3.5-9b-q5_k_m"):
    async with async_client.beta.chat.completions.stream(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)
    print()


asyncio.run(stream_response("Explique le fine-tuning en 5 points."))
```

### curl — streaming SSE

```bash
curl -sN https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "messages": [{"role": "user", "content": "Compte jusqu'\''à 5 lentement."}],
    "stream": true
  }'

# Chaque ligne reçue ressemble à :
# data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"1"},...}]}
# data: {"id":"chatcmpl-...","choices":[{"delta":{"content":","},...}]}
# ...
# data: [DONE]
```

---

## 6. Paramètres de génération

### 6.1 Paramètres standard OpenAI

Ces paramètres sont identiques à ceux de l'API OpenAI et sont documentés dans
la référence officielle. Ils couvrent la grande majorité des besoins courants.

```python
response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[{"role": "user", "content": "Ton message ici"}],

    # ── Longueur de la réponse ────────────────────────────────────────────────
    max_tokens=2048,        # nombre max de tokens à générer (défaut : illimité)

    # ── Créativité / diversité ────────────────────────────────────────────────
    temperature=0.7,        # 0.0 = déterministe · 2.0 = très créatif (défaut : 1.0)
    top_p=0.9,              # nucleus sampling — alternative à temperature

    # ── Contrôle des répétitions ──────────────────────────────────────────────
    frequency_penalty=0.0,  # pénalise les tokens fréquents dans la sortie  (−2 à 2)
    presence_penalty=0.0,   # pénalise tout token déjà apparu dans la sortie (−2 à 2)

    # ── Conditions d'arrêt ────────────────────────────────────────────────────
    stop=["###", "\n\n"],   # séquences qui interrompent la génération

    # ── Mode de livraison ────────────────────────────────────────────────────
    stream=False,           # True pour recevoir les tokens au fil de la génération
)
```

#### Recommandations par cas d'usage

| Cas d'usage | temperature | top_p | max_tokens |
|---|---|---|---|
| Extraction / classification | 0.0–0.2 | — | 256 |
| Réponses factuelles | 0.3–0.5 | — | 1024 |
| Rédaction académique | 0.5–0.7 | 0.9 | 2048 |
| Génération créative | 0.8–1.2 | 0.95 | 4096 |
| Brainstorming | 1.0–1.5 | 0.95 | 2048 |

---

### 6.2 Paramètres avancés de sampling (llama.cpp)

La gateway agit comme un **proxy transparent** : tout paramètre natif de `llama.cpp`
peut être passé directement dans le corps de la requête `/v1/chat/completions`, aux
côtés des paramètres OpenAI standard. Aucune configuration particulière n'est nécessaire.

Avec le SDK `openai-python`, ces paramètres supplémentaires se transmettent via
`extra_body`. Le SDK les fusionne dans le corps JSON avant l'envoi ; `llama-server`
les reçoit et les applique sans distinction.

#### Contrôle du vocabulaire

| Paramètre | Défaut | Description |
|---|---|---|
| `top_k` | 40 | Limite le vocabulaire aux *K* tokens les plus probables à chaque étape |
| `min_p` | 0.05 | Exclut les tokens dont la probabilité est inférieure à 5 % de celle du token le plus probable |

#### Anti-répétition locale

Indispensables pour éviter que le modèle ne tourne en rond dans les générations longues.

| Paramètre | Défaut | Description |
|---|---|---|
| `repeat_last_n` | 64 | Fenêtre (en tokens) analysée pour détecter les répétitions |
| `repeat_penalty` | 1.0 | Intensité de la pénalité (1.1 = légère, 1.3 = forte) |
| `presence_penalty` | 0.0 | Pénalise tout token qui est déjà apparu, quelle que soit sa fréquence |
| `frequency_penalty` | 0.0 | Pénalise les tokens proportionnellement à leur fréquence d'apparition |

#### Sampler DRY — anti-répétition long terme

Conçu pour les générations de plusieurs milliers de mots, le sampler DRY détecte
et pénalise les séquences entières qui se répètent à grande distance, là où
`repeat_penalty` est insuffisant.

| Paramètre | Défaut | Description |
|---|---|---|
| `dry_multiplier` | 0.0 | Intensité (0 = désactivé ; 0.5 recommandé pour les textes longs) |
| `dry_base` | 1.75 | Facteur de base de la pénalité exponentielle |
| `dry_allowed_length` | 2 | Longueur minimale (en tokens) d'une séquence répétée pour être pénalisée |
| `dry_penalty_last_n` | -1 | Fenêtre de recherche (−1 = contexte entier) |

#### Mirostat — contrôle entropique adaptatif

Mirostat est une alternative à `temperature` + `top_p`. Plutôt que de fixer des
seuils de probabilité, il pilote directement l'*entropie* du texte généré, c'est-à-dire
son niveau de prévisibilité. Cela donne une cohérence stylistique plus stable sur
de longues séquences.

| Paramètre | Défaut | Description |
|---|---|---|
| `mirostat` | 0 | 0 = désactivé · 1 = v1 · **2 = v2 (recommandé)** |
| `mirostat_tau` | 5.0 | Entropie cible (valeurs basses → texte plus prévisible) |
| `mirostat_eta` | 0.1 | Taux d'adaptation — laisser à 0.1 dans la quasi-totalité des cas |

#### Samplers complémentaires

| Paramètre | Défaut | Description |
|---|---|---|
| `xtc_probability` | 0.0 | Probabilité d'élaguer les tokens très probables (0 = désactivé) |
| `xtc_threshold` | 0.1 | Seuil de probabilité pour l'élagage XTC |
| `typical_p` | 1.0 | Nucleus sampling « typique » (1.0 = désactivé) |
| `tfs_z` | 1.0 | Tail-free sampling (1.0 = désactivé) |

#### Contrôle d'exécution

| Paramètre | Défaut | Description |
|---|---|---|
| `seed` | aléatoire | Graine pour une génération reproductible (ex. : `42`) |
| `n_predict` | — | Alias de `max_tokens` |
| `ignore_eos` | false | Ignorer le token de fin de séquence (génération forcée) |
| `stop` | — | Séquences d'arrêt — pour les modèles Qwen/ChatML : `["<\|im_end\|>", "<\|endoftext\|>"]` |

#### Exemple — génération longue avec paramètres avancés (Python)

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=600.0,  # prévoir large pour les longues générations
)

response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[
        {
            "role": "system",
            "content": "Tu es un expert en prospective éducative. Rédige un texte académique exhaustif.",
        },
        {
            "role": "user",
            "content": "Rédige un chapitre complet sur : L'IA comme tuteur algorithmique universel.",
        },
    ],

    # ── Paramètres OpenAI standard ────────────────────────────────────────────
    max_tokens=4096,
    temperature=0.8,
    top_p=0.95,
    stop=["<|im_end|>", "<|im_start|>", "<|endoftext|>"],

    # ── Paramètres llama.cpp natifs — fusionnés dans le body par le SDK ────────
    extra_body={
        "top_k": 40,
        "min_p": 0.05,
        "repeat_last_n": 64,
        "repeat_penalty": 1.1,
        "dry_multiplier": 0.5,   # anti-répétition long terme activé
        "dry_base": 1.75,
        "dry_allowed_length": 2,
        "seed": 42,              # résultat reproductible
    },
)

print(response.choices[0].message.content)
```

> **Note :** les paramètres passés dans `extra_body` sont transmis tels quels par la gateway
> à `llama-server`, sans filtrage. Tout paramètre natif de `llama.cpp` est donc utilisable.

#### Exemple — curl avec paramètres avancés

Paramètres standard et avancés coexistent dans le même corps JSON, sans imbrication :

```bash
curl -s https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "messages": [{"role": "user", "content": "Rédige une introduction sur les LLMs."}],
    "max_tokens": 2048,
    "temperature": 0.8,
    "top_k": 40,
    "min_p": 0.05,
    "repeat_last_n": 64,
    "repeat_penalty": 1.1,
    "dry_multiplier": 0.5,
    "seed": 42
  }'
```

---

## 7. Endpoints natifs llama.cpp

En complément des routes compatibles OpenAI, la gateway expose les endpoints natifs
de `llama.cpp`. Ils bénéficient des mêmes garanties d'authentification, de rate
limiting et de gestion VRAM automatique que les routes standard.

### Completion native — `POST /completion` et `POST /v1/completion`

Contrairement à `/v1/chat/completions` qui attend un tableau `messages` et applique
automatiquement un template de conversation (ChatML, Llama, etc.), cet endpoint
accepte une chaîne brute dans le champ `prompt`. Il est utile pour :

- migrer des scripts `llama.cpp` existants sans les réécrire,
- gérer manuellement le formatage du prompt (ChatML, Alpaca, etc.),
- travailler avec des modèles sans template de conversation.

```bash
curl -s https://llm.eva.univ-pau.fr/completion \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "prompt": "<|im_start|>user\nQu'\''est-ce qu'\''un LLM ?<|im_end|>\n<|im_start|>assistant\n",
    "n_predict": 512,
    "temperature": 0.7,
    "repeat_penalty": 1.1,
    "stop": ["<|im_end|>", "<|endoftext|>"]
  }'
```

Réponse (format natif llama.cpp — différent du format OpenAI) :

```json
{
  "content": "Un LLM (Large Language Model) est un modèle de langage…",
  "stop": true,
  "tokens_predicted": 87,
  "tokens_evaluated": 42,
  "generation_settings": { "temperature": 0.7, "top_k": 40 }
}
```

> **Attention :** la réponse n'est pas au format OpenAI. Le texte généré se trouve
> dans le champ `content`, et non dans `choices[0].message.content`.

Python avec `httpx` (tous les paramètres avancés disponibles directement dans le body) :

```python
import httpx
import os

response = httpx.post(
    "https://llm.eva.univ-pau.fr/completion",
    headers={"Authorization": f"Bearer {os.environ['UPPA_LLM_KEY']}"},
    json={
        "model": "qwen3.5-9b-q5_k_m",
        "prompt": "La photosynthèse est",
        "n_predict": 256,
        "temperature": 0.5,
        "top_k": 40,
        "min_p": 0.05,
        "repeat_last_n": 64,
        "repeat_penalty": 1.1,
        "dry_multiplier": 0.5,
        "dry_base": 1.75,
        "dry_allowed_length": 2,
        "mirostat": 2,
        "mirostat_tau": 5.0,
        "mirostat_eta": 0.1,
        "seed": 42,
    },
    timeout=120.0,
)

data = response.json()
print(data["content"])
print(f"Tokens prompt : {data['tokens_evaluated']}  |  Tokens générés : {data['tokens_predicted']}")
```

Les deux chemins sont équivalents :
- `POST /completion` — chemin attendu par les scripts `llama.cpp` existants
- `POST /v1/completion` — alias préfixé `/v1/` pour les intégrations qui l'exigent

### Tokenisation — `POST /v1/tokenize`

Compte précisément le nombre de tokens d'un texte **selon le tokenizer exact du modèle**,
avant de l'envoyer. Cela permet d'éviter les erreurs de contexte dépassé, dont la limite
varie selon le modèle et la configuration.

```bash
curl -s https://llm.eva.univ-pau.fr/v1/tokenize \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "content": "Votre texte à tokeniser ici…"
  }'
# Réponse : {"tokens": [123, 456, 789, ...]}
```

Python — vérifier qu'un prompt tient dans le contexte avant de l'envoyer :

```python
import httpx
import os

def count_tokens(text: str, model: str = "qwen3.5-9b-q5_k_m") -> int:
    """Compte les tokens d'un texte selon le tokenizer exact du modèle cible."""
    r = httpx.post(
        "https://llm.eva.univ-pau.fr/v1/tokenize",
        headers={"Authorization": f"Bearer {os.environ['UPPA_LLM_KEY']}"},
        json={"model": model, "content": text},
        timeout=10.0,
    )
    return len(r.json()["tokens"])


MAX_CONTEXT = 32_768  # tokens — dépend du modèle et de la configuration serveur

prompt = open("mon_document_long.txt").read()
n = count_tokens(prompt)

if n > MAX_CONTEXT * 0.8:  # conserver 20 % pour la réponse
    print(f"Prompt trop long ({n} tokens) — le découper en segments plus courts.")
else:
    print(f"OK : {n} tokens  ({MAX_CONTEXT - n} disponibles pour la réponse)")
```

### Détokenisation — `POST /v1/detokenize`

Reconstruit du texte à partir d'une liste d'identifiants de tokens.

```bash
curl -s https://llm.eva.univ-pau.fr/v1/detokenize \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b-q5_k_m",
    "tokens": [9468, 17403, 374, 264]
  }'
# Réponse : {"content": "La France est un"}
```

### Récapitulatif des endpoints disponibles

| Endpoint | Entrée | Format de réponse | Usage recommandé |
|---|---|---|---|
| `POST /v1/chat/completions` | `messages` (array) | OpenAI standard | **Par défaut** — template appliqué automatiquement |
| `POST /v1/completions` | `prompt` (string) | OpenAI standard | Legacy text completion |
| `POST /completion` | `prompt` (string) | Natif llama.cpp | Scripts llama.cpp existants |
| `POST /v1/completion` | `prompt` (string) | Natif llama.cpp | Alias de `/completion` |
| `POST /v1/tokenize` | `content` (string) | `{"tokens": [...]}` | Compter les tokens avant envoi |
| `POST /v1/detokenize` | `tokens` (array) | `{"content": "…"}` | Reconstruire du texte depuis des IDs |
| `GET /v1/models` | — | OpenAI standard | Lister les modèles activés |
| `GET /health` | — | JSON | État du service (sans authentification) |

---

## 8. Intégration LangChain

LangChain est un framework Python très utilisé en recherche pour construire des
pipelines NLP complexes : chaînes de traitement, agents, RAG. L'intégration avec
le cluster EVA est immédiate — il suffit de renseigner l'URL de base et la clé.

### Requête simple

```bash
pip install langchain-openai langchain-core
```

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
import os

llm = ChatOpenAI(
    model="qwen3.5-9b-q5_k_m",
    openai_api_base="https://llm.eva.univ-pau.fr/v1",
    openai_api_key=os.environ["UPPA_LLM_KEY"],
    temperature=0.7,
    max_tokens=2048,
    request_timeout=120,
)

messages = [
    SystemMessage(content="Tu es un expert en machine learning."),
    HumanMessage(content="Explique le concept de surapprentissage."),
]
response = llm.invoke(messages)
print(response.content)
```

### Pipeline RAG (Retrieval-Augmented Generation)

Le RAG consiste à enrichir un prompt avec des extraits pertinents issus d'un corpus
de documents, ce qui améliore considérablement la précision des réponses sur un domaine
spécifique. L'exemple ci-dessous combine le LLM du cluster EVA avec des embeddings
calculés localement (sans appel à un service externe).

```bash
pip install langchain-community faiss-cpu sentence-transformers
```

```python
from langchain_openai import ChatOpenAI
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain_core.documents import Document
from langchain_community.embeddings import HuggingFaceEmbeddings

# LLM — servi par le cluster EVA
llm = ChatOpenAI(
    model="qwen3.5-9b-q5_k_m",
    openai_api_base="https://llm.eva.univ-pau.fr/v1",
    openai_api_key=os.environ["UPPA_LLM_KEY"],
    temperature=0.0,
)

# Documents à indexer
docs = [
    Document(page_content="Le L40S est un GPU Ada Lovelace 48 Go…", metadata={"source": "doc1"}),
    Document(page_content="llama.cpp permet l'inférence locale…",   metadata={"source": "doc2"}),
]

# Embeddings calculés localement avec un modèle HuggingFace
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

# Construction de l'index vectoriel
vectorstore = FAISS.from_documents(docs, embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

# Chaîne RAG complète
qa_chain = RetrievalQA.from_chain_type(
    llm=llm,
    retriever=retriever,
    return_source_documents=True,
)

result = qa_chain.invoke({"query": "Quelles sont les caractéristiques du L40S ?"})
print(result["result"])
for doc in result["source_documents"]:
    print(f"  Source : {doc.metadata['source']}")
```

---

## 9. JavaScript / Node.js

```bash
npm install openai
```

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  baseURL: 'https://llm.eva.univ-pau.fr/v1',
  apiKey: process.env.UPPA_LLM_KEY,
  timeout: 120_000,  // 120 secondes
});

// Lister les modèles disponibles
const models = await client.models.list();
console.log(models.data.map(m => m.id));

// Requête simple
const response = await client.chat.completions.create({
    model: 'qwen3.5-9b-q5_k_m',
  messages: [
    { role: 'user', content: "Qu'est-ce que la perplexité en NLP ?" }
  ],
  max_tokens: 1024,
});
console.log(response.choices[0].message.content);

// Streaming
const stream = client.chat.completions.stream({
    model: 'qwen3.5-9b-q5_k_m',
  messages: [{ role: 'user', content: 'Explique BERT en détail.' }],
  stream: true,
});

for await (const chunk of stream) {
  const delta = chunk.choices[0]?.delta?.content ?? '';
  process.stdout.write(delta);
}
console.log();
```

---

## 10. Comportement au premier appel

### Chargement à la demande

Les modèles ne sont **pas maintenus en VRAM en permanence**. Cette décision de conception
permet de réduire la consommation électrique d'environ 85 % pendant les périodes d'inactivité
(nuit, week-end), en faisant passer le GPU de ~350 W (inférence) à ~30 W (GPU libre).
Chaque modèle dispose de son propre cycle de chargement/déchargement indépendant.

```
Votre requête arrive (model: "qwen3.5-9b-q5_k_m")
        │
        ▼
Ce modèle est-il chargé en VRAM ?
        │                          │
       OUI                        NON ──► Vérification du budget VRAM
        │                                         │
        │                            Budget suffisant ?
        │                              OUI │   NON ──► Éviction LRU du modèle
        │                                  │              le moins récemment utilisé
        │                    Chargement en cours
        │                    (~60–90s pour 70B · ~15s pour 8B)
        │                    Les requêtes concurrentes attendent
        │                    et sont toutes débloquées simultanément
        ▼
  Réponse générée (~2–30s selon la longueur)
```

**Ce qu'il faut retenir en pratique :**

- La **première requête** vers un modèle après une période d'inactivité peut prendre
  60 à 120 secondes (70B) ou 10 à 20 secondes (8B).
- Les requêtes **suivantes** sont nettement plus rapides (2 à 10 s pour une réponse courte).
- Si le budget VRAM est saturé, le modèle le **moins récemment utilisé** est automatiquement
  déchargé pour libérer de la place (*éviction LRU*).
- Après **5 minutes sans requête**, chaque modèle est déchargé individuellement.
- **Aucune requête n'est perdue** : celles qui arrivent pendant le chargement sont mises
  en file d'attente et traitées dès que le modèle est prêt.

### Gérer ce délai dans votre code

```python
import time
from openai import OpenAI, APIStatusError
import httpx

# Option 1 — timeout long : le plus simple et le plus recommandé.
# Le client attend silencieusement le chargement du modèle.
client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=150.0,
)
response = client.chat.completions.create(
    model="qwen3.5-9b-q5_k_m",
    messages=[{"role": "user", "content": "Bonjour"}],
)

# Option 2 — interroger /health en amont pour anticiper l'état du service.
def get_service_status() -> dict:
    r = httpx.get("https://llm.eva.univ-pau.fr/health", timeout=5)
    return r.json()

status = get_service_status()
print("Modèles actuellement chargés :", status["models_loaded"])
print("VRAM disponible :", status["vram_available_gb"], "Go")
```

---

## 11. Codes d'erreur et solutions

### Vue d'ensemble

| Code HTTP | Type d'erreur | Cause probable | Solution |
|---|---|---|---|
| `200` | — | Succès | — |
| `400` | `invalid_request_error` | JSON malformé ou paramètre invalide | Vérifier le corps de la requête |
| `401` | `authentication_error` | Clé absente, invalide ou révoquée | Vérifier la clé ; contacter l'admin si révoquée |
| `403` | `permission_error` | Modèle désactivé par l'administrateur | Utiliser un modèle disponible via `GET /v1/models` |
| `404` | `not_found_error` | Identifiant de modèle inconnu | Vérifier l'ID du modèle via `GET /v1/models` |
| `429` | `rate_limit_error` | Limite de débit dépassée | Attendre 60 s ; consulter l'en-tête `Retry-After` |
| `503` | `server_error` | Queue VRAM pleine/expirée, modèle impossible à charger ou backend temporairement indisponible | Respecter `Retry-After` si présent, sinon attendre 30 à 90 s |
| `504` | `server_error` | Timeout de génération | Réduire `max_tokens` ou simplifier le prompt |
| `502` | `server_error` | Moteur d'inférence injoignable | Transitoire — réessayer dans 30 s |
| `500` | `server_error` | Erreur interne inattendue | Contacter l'admin avec l'heure et le contexte |

### Format des erreurs

Toutes les erreurs suivent le format OpenAI standard :

```json
{
  "error": {
    "message": "Modèle 'modele-inconnu' non trouvé dans le registre.",
    "type": "not_found_error",
    "code": "404"
  }
}
```

### Gestion des erreurs avec openai-python

```python
from openai import (
    OpenAI,
    AuthenticationError,    # 401
    PermissionDeniedError,  # 403 — modèle désactivé
    NotFoundError,          # 404 — modèle inconnu
    RateLimitError,         # 429
    APIStatusError,         # 4xx / 5xx génériques
    APITimeoutError,        # timeout réseau
    APIConnectionError,     # connexion impossible
)

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
    max_retries=0,
)

try:
    response = client.chat.completions.create(
        model="qwen3.5-9b-q5_k_m",
        messages=[{"role": "user", "content": "Bonjour"}],
    )
    print(response.choices[0].message.content)

except AuthenticationError:
    print("Erreur 401 : vérifiez votre clé API (variable UPPA_LLM_KEY).")
    print("Contactez l'admin si vous pensez qu'elle a été révoquée.")

except NotFoundError:
    print("Erreur 404 : identifiant de modèle inconnu.")
    print("Listez les modèles disponibles via GET /v1/models.")

except PermissionDeniedError:
    print("Erreur 403 : ce modèle est temporairement désactivé.")
    print("Essayez un autre modèle ou contactez l'admin.")

except RateLimitError as e:
    retry_after = int(e.response.headers.get("Retry-After", 60))
    print(f"Limite de débit atteinte — réessayer dans {retry_after} s.")
    time.sleep(retry_after)

except APIStatusError as e:
    if e.status_code == 503:
        print("Modèle en cours de chargement — réessayer dans 30 à 90 s.")
        time.sleep(60)
    elif e.status_code == 504:
        print("Timeout : le prompt est peut-être trop long. Essayez de réduire max_tokens.")
    else:
        print(f"Erreur {e.status_code} : {e.message}")

except APITimeoutError:
    print("Timeout réseau — vérifiez votre connexion au réseau UPPA.")

except APIConnectionError:
    print("Impossible de joindre llm.eva.univ-pau.fr — êtes-vous sur le réseau UPPA ?")
```

### Erreur 401 — clé invalide

Trois vérifications à effectuer dans l'ordre :

```bash
# 1. La variable d'environnement est-elle définie ?
echo $UPPA_LLM_KEY

# 2. Le mot-clé "Bearer" est-il présent dans le header ?
curl -H "Authorization: Bearer $UPPA_LLM_KEY" …
#                         ↑ obligatoire

# 3. Y a-t-il des espaces ou caractères invisibles dans la clé ?
echo -n "$UPPA_LLM_KEY" | cat -A
```

### Erreur 429 — limite de débit

La limite par défaut est de **20 requêtes par minute**. Dans une boucle de traitement,
espacer les appels de 3 secondes suffit généralement à rester en dessous du seuil.

```python
import time

prompts = ["Question 1", "Question 2", "Question 3", …]

results = []
for i, prompt in enumerate(prompts):
    try:
        r = client.chat.completions.create(
            model="qwen3.5-9b-q5_k_m",
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.choices[0].message.content)
    except RateLimitError:
        print(f"Rate limit sur la requête {i} — attente 60 s…")
        time.sleep(60)
        r = client.chat.completions.create(
            model="qwen3.5-9b-q5_k_m",
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.choices[0].message.content)

    time.sleep(3)  # ~20 req/min max
```

---

## 12. Limites et quotas

| Limite | Valeur par défaut | Notes |
|---|---|---|
| Requêtes par minute (RPM) | 20 | Ajustable par l'admin sur demande motivée |
| Tokens de contexte max | 32 768 par requête | Prompt + réponse combinés (dépend du modèle) |
| Slots parallèles — modèle 70B | 4 | Partagés entre tous les utilisateurs du même modèle |
| Slots parallèles — modèle 8B | 8 | Indépendants de ceux du 70B |
| Quota mensuel de tokens | Illimité | Configurable par l'admin ; appliqué sur une fenêtre glissante de 30 jours — dépassement → 429 avec `Retry-After` |

> Les slots parallèles sont propres à chaque modèle. Des requêtes simultanées vers le 70B
> et vers le 8B ne se bloquent pas mutuellement.

Si vous avez besoin de limites plus élevées pour un projet spécifique (annotation de corpus,
pipeline d'inférence à grande échelle, etc.), contactez l'admin en précisant le volume attendu.

### Estimer sa consommation de tokens

```python
# Règle approximative : 1 token ≈ 0,75 mot (français ou anglais)
# Un paragraphe de 200 mots représente environ 270 tokens.

# Estimation précise avec tiktoken (tokenizer OpenAI — approximation acceptable)
pip install tiktoken

import tiktoken
enc = tiktoken.get_encoding("cl100k_base")
text = "Votre texte ici…"
tokens = len(enc.encode(text))
print(f"Tokens estimés : {tokens}")

# Pour un comptage exact selon le tokenizer du modèle cible :
# utiliser POST /v1/tokenize (voir section 7)
```

---

## 13. Exemples complets par cas d'usage

### Annotation automatique d'un corpus

Ce script annote le sentiment d'un corpus de textes avec gestion du rate limit,
relances automatiques et sauvegarde progressive des résultats.

```python
"""
Annotation de sentiment sur un corpus de textes.
Gère la limite de débit (429) et sauvegarde au fil du traitement.
"""
import json
import time
import os
from pathlib import Path
from openai import OpenAI, RateLimitError

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=60.0,
)

SYSTEM_PROMPT = """Tu es un annotateur de sentiment. Pour chaque texte,
réponds UNIQUEMENT avec un JSON valide : {"sentiment": "positif"|"négatif"|"neutre", "score": 0.0-1.0}"""


def annotate_sentiment(text: str) -> dict:
    response = client.chat.completions.create(
        model="qwen3.5-9b-q5_k_m",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": text},
        ],
        max_tokens=64,
        temperature=0.0,  # déterministe : même texte → même annotation
    )
    return json.loads(response.choices[0].message.content)


texts = Path("corpus.txt").read_text().splitlines()
results = []

for i, text in enumerate(texts):
    print(f"[{i+1}/{len(texts)}] {text[:50]}…", end=" ")

    for attempt in range(3):
        try:
            result = annotate_sentiment(text)
            results.append({"text": text, **result})
            print(f"→ {result['sentiment']} ({result['score']:.2f})")
            break
        except RateLimitError:
            print("rate limit, attente 60 s…")
            time.sleep(60)
        except json.JSONDecodeError:
            print("réponse non-JSON, ignorée")
            results.append({"text": text, "sentiment": "erreur", "score": 0.0})
            break

    time.sleep(3)  # rester sous 20 req/min

with open("annotations.json", "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\nTerminé : {len(results)} textes annotés → annotations.json")
```

### Résumé de publications scientifiques

```python
"""
Résumé automatique d'abstracts scientifiques pour un public non-spécialiste.
"""
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


def summarize_abstract(abstract: str, language: str = "français") -> str:
    """Résume un abstract scientifique en 2–3 phrases accessibles."""
    prompt = f"""Résume cet abstract scientifique en {language} en 2 à 3 phrases
claires pour un public non-spécialiste. Mets en avant la contribution principale.

Abstract :
{abstract}"""

    response = client.chat.completions.create(
        model="qwen3.5-9b-q5_k_m",
        messages=[
            {"role": "system", "content": "Tu es un expert en vulgarisation scientifique."},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=256,
        temperature=0.5,
    )
    return response.choices[0].message.content


abstract = """
We present LLaMA, a collection of foundation language models ranging from 7B to 65B parameters.
We train our models on trillions of tokens, and show that it is possible to train state-of-the-art
models using publicly available datasets exclusively, without resorting to proprietary and
inaccessible datasets…
"""

print(summarize_abstract(abstract))
```

### Extraction d'entités nommées

```python
"""
Extraction d'entités nommées depuis des textes de recherche, avec sortie JSON structurée.
"""
import json
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


def extract_entities(text: str) -> dict:
    """Extrait les entités nommées d'un texte et les retourne en JSON."""
    prompt = f"""Extrais les entités nommées du texte suivant.
Réponds UNIQUEMENT avec un JSON valide structuré ainsi :
{{
  "personnes": ["…"],
  "organisations": ["…"],
  "lieux": ["…"],
  "dates": ["…"],
  "concepts_cles": ["…"]
}}

Texte : {text}"""

    response = client.chat.completions.create(
        model="qwen3.5-9b-q5_k_m",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.0,  # extraction → réponse déterministe
    )

    content = response.choices[0].message.content

    # Extraire le bloc JSON si le modèle a ajouté du texte autour
    start = content.find("{")
    end   = content.rfind("}") + 1
    if start >= 0 and end > start:
        content = content[start:end]

    return json.loads(content)


text = """
En 2023, Yann LeCun, directeur scientifique de Meta AI, a présenté ses travaux
sur les architectures Joint Embedding Predictive Architecture (JEPA) à l'université
de New York. Ces travaux s'opposent aux approches génératives popularisées par
OpenAI avec GPT-4.
"""

entities = extract_entities(text)
print(json.dumps(entities, ensure_ascii=False, indent=2))
# {
#   "personnes": ["Yann LeCun"],
#   "organisations": ["Meta AI", "OpenAI"],
#   "lieux": ["New York"],
#   "dates": ["2023"],
#   "concepts_cles": ["JEPA", "GPT-4", "architectures génératives"]
# }
```

### Q&A sur vos propres documents (RAG simple)

Ce patron convient aux corpus de taille modeste (quelques dizaines de documents). Pour
des corpus plus volumineux, préférer LangChain + FAISS (voir [section 8](#8-intégration-langchain)).

```python
"""
RAG sans framework externe : injection des documents dans le contexte du prompt.
"""
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


def simple_rag(
    question: str,
    documents: list[str],
    model: str = "qwen3.5-9b-q5_k_m",
) -> str:
    """Répond à une question en se basant exclusivement sur les documents fournis."""
    context = "\n\n---\n\n".join(documents)

    prompt = f"""Réponds à la question en te basant UNIQUEMENT sur les documents fournis.
Si la réponse ne s'y trouve pas, indique-le clairement plutôt que d'inventer.

Documents :
{context}

Question : {question}"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Tu es un assistant de recherche documentaire rigoureux."},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=1024,
        temperature=0.2,  # peu de créativité : on veut de la fidélité aux sources
    )
    return response.choices[0].message.content


docs = [
    "Le projet EVARuntime est une gateway d'inférence LLM développée pour l'UPPA…",
    "Le GPU L40S dispose de 48 Go de VRAM et d'une architecture Ada Lovelace…",
]

answer = simple_rag("Quelle est la quantité de VRAM du GPU utilisé ?", docs)
print(answer)
```

---

## Besoin d'aide ?

| Problème | Démarche recommandée |
|---|---|
| Demande de clé API | Contacter Mohamad El Akhal El Bouzidi ou Benjamin Mascret |
| Clé invalide / révoquée (401) | Vérifier la variable d'environnement ; contacter l'admin |
| Modèle inconnu (404) | Vérifier les IDs disponibles via `GET /v1/models` |
| Quota dépassé (429) | Espacer les requêtes ; demander une augmentation si nécessaire |
| Comportement inattendu | Vérifier les paramètres `temperature` et `system` |
| Intégration avec un outil spécifique | La gateway étant compatible OpenAI, la documentation officielle OpenAI s'applique dans la quasi-totalité des cas |
