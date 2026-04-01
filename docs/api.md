# Guide utilisateur — API Cluster EVA

Ce document s'adresse aux doctorants, enseignants et chercheurs qui souhaitent
utiliser le service d'inférence du cluster EVA depuis leurs scripts et applications.

L'API est **compatible avec le standard OpenAI** : tout code existant utilisant
`openai-python`, LangChain, LiteLLM ou `curl` fonctionne en changeant simplement
l'URL de base et la clé API.

---

## Table des matières

1. [Obtenir une clé API](#1-obtenir-une-clé-api)
2. [Premier test — curl](#2-premier-test--curl)
3. [Python — openai-python](#3-python--openai-python)
4. [Python — requêtes directes (httpx)](#4-python--requêtes-directes-httpx)
5. [Streaming](#5-streaming)
6. [Paramètres de génération](#6-paramètres-de-génération)
7. [Intégration LangChain](#7-intégration-langchain)
8. [JavaScript / Node.js](#8-javascript--nodejs)
9. [Comportement au premier appel](#9-comportement-au-premier-appel)
10. [Codes d'erreur et solutions](#10-codes-derreur-et-solutions)
11. [Limites et quotas](#11-limites-et-quotas)
12. [Exemples complets par cas d'usage](#12-exemples-complets-par-cas-dusage)

---

## 1. Obtenir une clé API

Contacter l'administrateur du service (DSI UPPA ou responsable du projet)
en indiquant votre nom, email institutionnel et l'usage prévu.

Vous recevrez une clé au format :
```
llmgw-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

> **Important :** Stockez cette clé en lieu sûr. Elle ne peut pas être récupérée
> après sa création. En cas de perte, demander une nouvelle clé et révoquer l'ancienne.

**Ne jamais :**
- Committer la clé dans un dépôt git
- La partager par email
- La mettre en dur dans le code source

**Bonne pratique — variable d'environnement :**
```bash
# Dans ~/.bashrc ou ~/.zshrc
export UPPA_LLM_KEY="llmgw-votre_cle_ici"

# Dans un projet Python, utiliser un fichier .env (ajouté dans .gitignore)
echo "UPPA_LLM_KEY=llmgw-votre_cle_ici" >> .env
echo ".env" >> .gitignore
```

---

## 2. Premier test — curl

```bash
# Vérifier que le service répond et voir les modèles disponibles
curl -s https://llm.eva.univ-pau.fr/health
# → {
#     "status": "ok",
#     "models_loaded": [],
#     "vram_used_gb": 0.0,
#     "vram_available_gb": 43.6
#   }

# Lister les modèles disponibles
curl -s https://llm.eva.univ-pau.fr/v1/models \
  -H "Authorization: Bearer $UPPA_LLM_KEY" | python3 -m json.tool

# Requête simple (le modèle peut mettre 60-90s à charger la première fois)
curl -s https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer $UPPA_LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b-instruct",
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
  "model": "llama-3.3-70b-instruct",
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

---

## 3. Python — openai-python

### Installation

```bash
pip install openai
```

### Configuration

```python
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    # Augmenter le timeout : le modèle peut mettre 60-90s à charger
    timeout=120.0,
)
```

### Requête simple

```python
response = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[
        {"role": "system", "content": "Tu es un assistant de recherche scientifique."},
        {"role": "user",   "content": "Quelles sont les principales limites des LLMs actuels ?"}
    ],
)

print(response.choices[0].message.content)
print(f"\n--- Tokens utilisés : {response.usage.total_tokens} ---")
```

### Choisir un modèle spécifique

Le champ `model` de la requête détermine quel modèle traite la demande.
Si plusieurs modèles sont activés, vous pouvez cibler le plus approprié :

```python
# Modèle principal : qualité maximale
response = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Analyse en détail ce texte..."}],
)

# Modèle léger : latence réduite, moins de VRAM
response = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Résume en 2 phrases."}],
)
```

Pour connaître les modèles disponibles :

```python
models = client.models.list()
for m in models.data:
    print(m.id)
```

### Conversation multi-tours

```python
def chat(messages: list[dict], model: str = "llama-3.3-70b-instruct") -> str:
    """Envoie une conversation et retourne la réponse."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
        temperature=0.7,
    )
    return response.choices[0].message.content


# Exemple de conversation
conversation = [
    {"role": "system", "content": "Tu es un expert en traitement du langage naturel."}
]

# Tour 1
conversation.append({"role": "user", "content": "Qu'est-ce qu'un transformer ?"})
reply = chat(conversation)
conversation.append({"role": "assistant", "content": reply})
print("Assistant :", reply)

# Tour 2
conversation.append({"role": "user", "content": "Et le mécanisme d'attention ?"})
reply = chat(conversation)
print("Assistant :", reply)
```

### Avec gestion des erreurs robuste

```python
import time
from openai import OpenAI, APIStatusError, APITimeoutError, APIConnectionError

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
    max_retries=0,  # On gère nous-mêmes les retries
)


def query_with_retry(messages: list[dict], model: str = "llama-3.3-70b-instruct",
                     max_attempts: int = 3) -> str:
    """
    Envoie une requête avec retry automatique.
    Attend le chargement du modèle si nécessaire (503).
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
                # Modèle en cours de chargement — attendre et réessayer
                wait = 30 * attempt
                print(f"Modèle en chargement, attente {wait}s (tentative {attempt}/{max_attempts})…")
                time.sleep(wait)
            elif e.status_code == 429:
                # Rate limit dépassé — attendre 60s
                print("Limite de débit atteinte, attente 60s…")
                time.sleep(60)
            elif e.status_code == 401:
                raise ValueError("Clé API invalide ou révoquée.") from e
            elif e.status_code == 404:
                raise ValueError(f"Modèle '{model}' inconnu ou désactivé.") from e
            elif e.status_code == 403:
                raise ValueError(f"Modèle '{model}' désactivé par l'administrateur.") from e
            else:
                raise

        except APITimeoutError:
            print(f"Timeout (tentative {attempt}/{max_attempts})…")
            time.sleep(10 * attempt)

        except APIConnectionError:
            print(f"Impossible de joindre le serveur (tentative {attempt}/{max_attempts})…")
            time.sleep(5 * attempt)

    raise RuntimeError(f"Échec après {max_attempts} tentatives.")


# Utilisation
result = query_with_retry([
    {"role": "user", "content": "Résume ce paragraphe : …"}
])
print(result)
```

---

## 4. Python — requêtes directes (httpx)

Si vous n'utilisez pas `openai-python` et préférez un client HTTP bas niveau :

```python
import httpx
import os

API_URL = "https://llm.eva.univ-pau.fr/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {os.environ['UPPA_LLM_KEY']}",
    "Content-Type": "application/json",
}


def ask(prompt: str, model: str = "llama-3.3-70b-instruct",
        system: str = "Tu es un assistant utile.") -> str:
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

Le streaming permet de recevoir la réponse **token par token**, comme sur ChatGPT.
C'est utile pour les longues générations ou pour une meilleure expérience utilisateur.

### Python — openai-python

```python
# Streaming simple — affiche chaque token dès sa génération
stream = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Rédige une introduction sur les transformers."}],
    stream=True,
    max_tokens=1024,
)

for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)

print()  # Saut de ligne final
```

### Python — streaming avec collecte du résultat complet

```python
def stream_and_collect(messages: list[dict], model: str = "llama-3.3-70b-instruct") -> str:
    """Stream vers stdout et retourne le texte complet."""
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
# Le texte complet est dans `text`
```

### Python — streaming async (pour applications FastAPI/asyncio)

```python
import asyncio
from openai import AsyncOpenAI

async_client = AsyncOpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


async def stream_response(prompt: str, model: str = "llama-3.3-70b-instruct"):
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
    "model": "llama-3.3-70b-instruct",
    "messages": [{"role": "user", "content": "Compte jusqu'\''à 5 lentement."}],
    "stream": true
  }'

# Chaque ligne ressemble à :
# data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"1"},...}]}
# data: {"id":"chatcmpl-...","choices":[{"delta":{"content":","},...}]}
# ...
# data: [DONE]
```

---

## 6. Paramètres de génération

Tous les paramètres standard OpenAI sont supportés :

```python
response = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Ton message ici"}],

    # ── Longueur ────────────────────────────────────────────────────────────
    max_tokens=2048,        # Nombre max de tokens à générer (défaut : illimité)

    # ── Créativité / diversité ──────────────────────────────────────────────
    temperature=0.7,        # 0.0 = déterministe, 2.0 = très créatif (défaut : 1.0)
    top_p=0.9,              # Nucleus sampling (alternative à temperature)

    # ── Répétitions ─────────────────────────────────────────────────────────
    frequency_penalty=0.0,  # Pénalise la répétition des tokens fréquents (-2 à 2)
    presence_penalty=0.0,   # Pénalise les tokens déjà apparus (-2 à 2)

    # ── Arrêt ────────────────────────────────────────────────────────────────
    stop=["###", "\n\n"],   # Séquences qui arrêtent la génération

    # ── Streaming ────────────────────────────────────────────────────────────
    stream=False,           # True pour le streaming token par token
)
```

### Recommandations par cas d'usage

| Cas d'usage | temperature | top_p | max_tokens |
|-------------|-------------|-------|------------|
| Extraction / classification | 0.0–0.2 | — | 256 |
| Réponses factuelles | 0.3–0.5 | — | 1024 |
| Rédaction académique | 0.5–0.7 | 0.9 | 2048 |
| Génération créative | 0.8–1.2 | 0.95 | 4096 |
| Brainstorming | 1.0–1.5 | 0.95 | 2048 |

---

## 7. Intégration LangChain

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
import os

llm = ChatOpenAI(
    model="llama-3.3-70b-instruct",
    openai_api_base="https://llm.eva.univ-pau.fr/v1",
    openai_api_key=os.environ["UPPA_LLM_KEY"],
    temperature=0.7,
    max_tokens=2048,
    request_timeout=120,
)

# Requête simple
messages = [
    SystemMessage(content="Tu es un expert en machine learning."),
    HumanMessage(content="Explique le concept de surapprentissage."),
]
response = llm.invoke(messages)
print(response.content)
```

### LangChain avec RAG (Retrieval-Augmented Generation)

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain_core.documents import Document

# LLM local via le gateway
llm = ChatOpenAI(
    model="llama-3.3-70b-instruct",
    openai_api_base="https://llm.eva.univ-pau.fr/v1",
    openai_api_key=os.environ["UPPA_LLM_KEY"],
    temperature=0.0,
)

# Exemple : indexer des documents
docs = [
    Document(page_content="Le L40S est un GPU Ada Lovelace 48GB…", metadata={"source": "doc1"}),
    Document(page_content="llama.cpp permet l'inférence locale…", metadata={"source": "doc2"}),
]

# Embeddings — utiliser un modèle local ou HuggingFace
from langchain_community.embeddings import HuggingFaceEmbeddings
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

vectorstore = FAISS.from_documents(docs, embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

qa_chain = RetrievalQA.from_chain_type(
    llm=llm,
    retriever=retriever,
    return_source_documents=True,
)

result = qa_chain.invoke({"query": "Quelles sont les caractéristiques du L40S ?"})
print(result["result"])
```

---

## 8. JavaScript / Node.js

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
  model: 'llama-3.3-70b-instruct',
  messages: [
    { role: 'user', content: 'Qu\'est-ce que la perplexité en NLP ?' }
  ],
  max_tokens: 1024,
});

console.log(response.choices[0].message.content);

// Streaming
const stream = client.chat.completions.stream({
  model: 'llama-3.3-70b-instruct',
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

## 9. Comportement au premier appel

Les modèles ne sont **pas chargés en permanence** (pour économiser l'électricité et
réduire le bruit des ventilateurs du serveur). Chaque modèle a son propre cycle
de chargement/déchargement indépendant.

```
Votre requête arrive (model: "llama-3.3-70b-instruct")
        │
        ▼
Ce modèle est-il chargé ?  ──non──→  Vérification du budget VRAM
        │                                      │
       oui                         Budget suffisant ?
        │                             oui │   non → éviction LRU du modèle
        │                                 ▼       le moins récemment utilisé
        │                   Chargement en cours (~60-90s pour 70B, ~15s pour 8B)
        │                              │
        │                   Toutes les requêtes en attente
        │                   pour ce modèle sont débloquées
        ▼                   ensemble dès qu'il est prêt
  Réponse immédiate
  (~2-30s selon la longueur)
```

**Conséquences pratiques :**

- La **première requête vers un modèle** après une période d'inactivité peut prendre 60 à 120 secondes (70B) ou 10 à 20 secondes (8B)
- Les requêtes **suivantes** vers ce modèle sont rapides (~2-10s pour une réponse courte)
- Si le budget VRAM est saturé, le modèle **le moins récemment utilisé** est automatiquement déchargé avant de charger le nouveau
- Après **5 minutes** sans requête, chaque modèle est déchargé individuellement

**Comment gérer ce délai dans votre code :**

```python
import time
from openai import OpenAI, APIStatusError
import httpx

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    # Timeout suffisant pour attendre le chargement du modèle
    timeout=150.0,
)

# Option 1 : timeout long (le plus simple)
# Le client attend automatiquement pendant le chargement.
response = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Bonjour"}],
)

# Option 2 : vérifier l'état via /health avant d'envoyer
def wait_for_any_model(max_wait: int = 30) -> dict:
    """Retourne l'état du gateway (modèles chargés, VRAM)."""
    r = httpx.get("https://llm.eva.univ-pau.fr/health", timeout=5)
    return r.json()

status = wait_for_any_model()
print("Modèles actuellement chargés :", status["models_loaded"])
print("VRAM disponible :", status["vram_available_gb"], "GB")
```

---

## 10. Codes d'erreur et solutions

### Vue d'ensemble

| Code HTTP | Type d'erreur | Cause | Solution |
|-----------|--------------|-------|----------|
| `200` | — | Succès | — |
| `400` | `invalid_request_error` | JSON malformé ou paramètre invalide | Vérifier le body de la requête |
| `401` | `authentication_error` | Clé absente, invalide ou révoquée | Vérifier la clé ; contacter l'admin si révoquée |
| `403` | `permission_error` | Modèle désactivé par l'administrateur | Utiliser un modèle disponible ; voir `GET /v1/models` |
| `404` | `not_found_error` | Modèle inconnu (non enregistré dans le registre) | Vérifier l'ID du modèle via `GET /v1/models` |
| `429` | `rate_limit_error` | Trop de requêtes (limite RPM dépassée) | Attendre 60s ; voir l'en-tête `Retry-After` |
| `503` | `server_error` | Modèle en cours de chargement ou VRAM insuffisante | Attendre 30-90s et réessayer |
| `504` | `server_error` | Timeout de génération (réponse trop longue) | Réduire `max_tokens` ou simplifier le prompt |
| `500` | `server_error` | Erreur interne inattendue | Contacter l'admin avec l'heure et le contexte |
| `502` | `server_error` | llama-server injoignable | Transitoire — réessayer dans 30s |

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
    APIStatusError,         # 4xx / 5xx
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
        model="llama-3.3-70b-instruct",
        messages=[{"role": "user", "content": "Bonjour"}],
    )
    print(response.choices[0].message.content)

except AuthenticationError:
    print("Erreur 401 : Vérifiez votre clé API UPPA_LLM_KEY.")
    print("Contacter l'admin si vous pensez qu'elle a été révoquée.")

except NotFoundError:
    print("Erreur 404 : Modèle inconnu.")
    print("Listez les modèles disponibles via GET /v1/models.")

except PermissionDeniedError:
    print("Erreur 403 : Ce modèle est temporairement désactivé.")
    print("Essayez un autre modèle ou contactez l'admin.")

except RateLimitError as e:
    retry_after = int(e.response.headers.get("Retry-After", 60))
    print(f"Limite de débit atteinte. Réessayer dans {retry_after}s.")
    time.sleep(retry_after)

except APIStatusError as e:
    if e.status_code == 503:
        print("Modèle en cours de chargement, réessayer dans 30-90s…")
        time.sleep(60)
    elif e.status_code == 504:
        print("Timeout : le prompt est peut-être trop long. Essayez max_tokens plus petit.")
    else:
        print(f"Erreur {e.status_code} : {e.message}")

except APITimeoutError:
    print("Timeout réseau. Vérifiez votre connexion au réseau UPPA.")

except APIConnectionError:
    print("Impossible de joindre llm.eva.univ-pau.fr. Êtes-vous sur le réseau UPPA ?")
```

### Erreur 404 — Modèle inconnu

```bash
# Symptôme
# {"error": {"message": "Modèle 'mon-modele' non trouvé dans le registre.", "type": "not_found_error"}}

# Solution : lister les modèles disponibles
curl -s https://llm.eva.univ-pau.fr/v1/models \
  -H "Authorization: Bearer $UPPA_LLM_KEY" | python3 -m json.tool
# → Liste les IDs de tous les modèles activés
```

### Erreur 401 — Authentification

```bash
# Vérifications :
# 1. La variable d'environnement est-elle définie ?
echo $UPPA_LLM_KEY

# 2. Le header est-il correct ? (Bearer, pas Basic ou autre)
curl -H "Authorization: Bearer $UPPA_LLM_KEY" ...
#                         ↑ ce mot est obligatoire

# 3. Y a-t-il des espaces ou caractères invisibles dans la clé ?
echo -n "$UPPA_LLM_KEY" | cat -A
```

### Erreur 429 — Rate limit

```python
# Symptôme : toutes vos requêtes en boucle déclenchent un 429
# Solution : espacer les requêtes

import time

prompts = ["Question 1", "Question 2", "Question 3", ...]  # longue liste

results = []
for i, prompt in enumerate(prompts):
    try:
        r = client.chat.completions.create(
            model="llama-3.3-70b-instruct",
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.choices[0].message.content)
    except RateLimitError:
        print(f"Rate limit sur prompt {i}, attente 60s…")
        time.sleep(60)
        r = client.chat.completions.create(
            model="llama-3.3-70b-instruct",
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.choices[0].message.content)

    # Espacer les requêtes pour ne pas dépasser la limite
    time.sleep(3)  # 3s entre chaque requête = ~20 req/min max
```

---

## 11. Limites et quotas

| Limite | Valeur par défaut | Notes |
|--------|-------------------|-------|
| Requêtes par minute (RPM) | 20 | Ajustable par l'admin sur demande |
| Tokens de contexte max | 32 768 par requête | Prompt + réponse (dépend du modèle) |
| Slots parallèles par modèle | 4 (70B) / 8 (8B) | Partagés entre tous les utilisateurs du même modèle |
| Quota mensuel tokens | Illimité | Configurable par l'admin |

> **Note :** Les slots parallèles sont indépendants par modèle. Requêtes vers le 70B
> et vers le 8B ne se bloquent pas mutuellement.

**Si vous avez besoin de limites plus élevées** (traitement de corpus, pipeline
d'annotation, etc.), contacter l'admin en expliquant le volume attendu.

### Estimer sa consommation de tokens

```python
# Règle approximative : 1 token ≈ 0.75 mot (français/anglais)
# Un paragraphe de 200 mots ≈ 270 tokens

# Compter précisément avec tiktoken (tokenizer OpenAI, compatible approximativement)
pip install tiktoken

import tiktoken
enc = tiktoken.get_encoding("cl100k_base")
text = "Votre texte ici..."
tokens = len(enc.encode(text))
print(f"Tokens estimés : {tokens}")
```

---

## 12. Exemples complets par cas d'usage

### Annotation d'un corpus de textes

```python
"""
Exemple : annoter automatiquement le sentiment de 100 avis.
Avec gestion de la limite de débit et sauvegarde des résultats.
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
réponds UNIQUEMENT avec un JSON : {"sentiment": "positif"|"négatif"|"neutre", "score": 0.0-1.0}"""


def annotate_sentiment(text: str) -> dict:
    response = client.chat.completions.create(
        model="llama-3.3-70b-instruct",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=64,
        temperature=0.0,  # Déterministe pour l'annotation
    )
    return json.loads(response.choices[0].message.content)


# Charger les textes
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
            print("rate limit, attente 60s…")
            time.sleep(60)
        except json.JSONDecodeError:
            print("réponse non-JSON, ignoré")
            results.append({"text": text, "sentiment": "erreur", "score": 0.0})
            break

    # Espacer pour ne pas dépasser 20 req/min
    time.sleep(3)

# Sauvegarder
with open("annotations.json", "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\nTerminé : {len(results)} textes annotés → annotations.json")
```

### Génération de résumés de publications

```python
"""
Exemple : résumer automatiquement des abstracts de publications scientifiques.
"""
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


def summarize_abstract(abstract: str, language: str = "français") -> str:
    """Résume un abstract scientifique pour un public non-spécialiste."""
    prompt = f"""Résume cet abstract scientifique en {language} en 2-3 phrases
claires pour un public non-spécialiste. Mets en avant la contribution principale.

Abstract :
{abstract}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-instruct",
        messages=[
            {"role": "system", "content": "Tu es un expert en vulgarisation scientifique."},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=256,
        temperature=0.5,
    )
    return response.choices[0].message.content


# Exemple
abstract = """
We present LLaMA, a collection of foundation language models ranging from 7B to 65B parameters.
We train our models on trillions of tokens, and show that it is possible to train state-of-the-art
models using publicly available datasets exclusively, without resorting to proprietary and
inaccessible datasets...
"""

summary = summarize_abstract(abstract)
print(summary)
```

### Extraction d'informations structurées

```python
"""
Exemple : extraire des entités nommées depuis des textes de recherche.
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
    """Extrait les entités nommées d'un texte."""
    prompt = f"""Extrais les entités nommées du texte suivant.
Réponds UNIQUEMENT avec un JSON valide structuré ainsi :
{{
  "personnes": ["..."],
  "organisations": ["..."],
  "lieux": ["..."],
  "dates": ["..."],
  "concepts_cles": ["..."]
}}

Texte : {text}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-instruct",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.0,
    )

    content = response.choices[0].message.content

    # Nettoyer le JSON si le modèle a ajouté du texte autour
    start = content.find("{")
    end = content.rfind("}") + 1
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

```python
"""
Exemple simple de RAG sans framework externe.
Charge des documents, découpe en chunks, répond aux questions.
"""
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key=os.environ["UPPA_LLM_KEY"],
    timeout=120.0,
)


def simple_rag(question: str, documents: list[str],
               model: str = "llama-3.3-70b-instruct") -> str:
    """
    RAG basique : injecte les documents dans le contexte.
    Pour des corpus plus grands, utiliser LangChain + FAISS.
    """
    context = "\n\n---\n\n".join(documents)

    prompt = f"""Réponds à la question en te basant UNIQUEMENT sur les documents fournis.
Si la réponse ne se trouve pas dans les documents, dis-le clairement.

Documents :
{context}

Question : {question}"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Tu es un assistant de recherche documentaire."},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=1024,
        temperature=0.2,
    )
    return response.choices[0].message.content


# Exemple
docs = [
    "Le projet EVARuntime est un gateway d'inférence LLM développé pour l'UPPA...",
    "Le GPU L40S dispose de 48GB de VRAM et d'une architecture Ada Lovelace...",
]

answer = simple_rag("Quelle est la quantité de VRAM du GPU utilisé ?", docs)
print(answer)
```

---

## Besoin d'aide ?

- **Problème d'accès** (clé invalide, accès révoqué) → Contacter l'administrateur
- **Modèle inconnu (404)** → Vérifier les IDs disponibles via `GET /v1/models`
- **Comportement inattendu du modèle** → Vérifier les paramètres `temperature` et `system`
- **Quota dépassé** → Contacter l'admin pour augmenter la limite
- **Intégration avec un outil spécifique** → Le gateway étant compatible OpenAI,
  la documentation officielle OpenAI est applicable dans la quasi-totalité des cas
