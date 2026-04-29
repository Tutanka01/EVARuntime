# Référence API — Gateway Étudiante

Base URL : `https://llm-students.univ-pau.fr`

L'API est un **sous-ensemble compatible OpenAI**. Elle accepte les mêmes payloads
que l'API OpenAI mais sur un ensemble volontairement réduit d'endpoints et de
paramètres.

---

## Authentification

Toutes les routes (sauf `/health`) requièrent un token Bearer :

```http
Authorization: Bearer llmstu-<26 caractères>
```

Les clés ont la forme `llmstu-…` (préfixe distinct des clés admin `llmgw-…`).
Elles ont une expiration obligatoire. Une clé expirée ou révoquée retourne `401`.

---

## Rate limiting

Chaque requête est soumise à **quatre vérifications successives** :

| Vérification | Fenêtre | Limite par défaut | Header 429 |
|---|---|---|---|
| Burst | 10 s | 3 req | `X-RateLimit-Window: 10` |
| RPM | 60 s glissantes | 10 req/min | `X-RateLimit-Window: 60` |
| Tokens/heure | 60 min glissantes | 20 000 tokens | `Retry-After: 3600` |
| Tokens/jour | depuis minuit UTC | 100 000 tokens | `Retry-After: <s jusqu'à minuit>` |
| Streams concurrents | instantané | 1 | `Retry-After: 10` |

Tous les `429` incluent les headers suivants :

```
Retry-After: 42
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1714384200    (epoch Unix)
X-RateLimit-Window: 60
```

Ces valeurs peuvent être utilisées pour implémenter un retry exponentiel ou
afficher un message d'attente à l'utilisateur.

---

## GET /health

Non authentifié. Utilisé par les health checks nginx et systemd.

**Réponse 200 (service opérationnel) :**
```json
{"status": "ok", "db": "ok"}
```

**Réponse 503 (base de données inaccessible) :**
```json
{"status": "degraded", "db": "error"}
```

---

## GET /v1/models

Retourne la liste des modèles autorisés pour les étudiants.
Ce n'est pas la liste complète de la gateway admin — seulement le sous-ensemble
configuré dans `ALLOWED_MODELS`.

**Réponse 200 :**
```json
{
  "object": "list",
  "data": [
    {
      "id": "llama-3.1-8b-instruct",
      "object": "model",
      "created": 1704067200,
      "owned_by": "uppa-eva"
    },
    {
      "id": "qwen-9b",
      "object": "model",
      "created": 1704067200,
      "owned_by": "uppa-eva"
    }
  ]
}
```

---

## POST /v1/chat/completions

Seul endpoint d'inférence. Compatible avec les clients OpenAI standard.

### Paramètres acceptés

| Champ | Type | Limite | Notes |
|---|---|---|---|
| `model` | string | — | Doit être dans la liste `/v1/models` |
| `messages` | array | max 32 messages | Chaque message ≤ 8 KB, total ≤ 32 KB |
| `max_tokens` | int | max 2048 | Forcé à 2048 si supérieur |
| `stream` | bool | — | `true` ou `false` |
| `temperature` | float | [0.0, 2.0] | Clampé si hors bornes |
| `top_p` | float | [0.0, 1.0] | Clampé si hors bornes |
| `top_k` | int | [0, 200] | Clampé si hors bornes |
| `repeat_penalty` | float | [0.5, 2.0] | Clampé si hors bornes |
| `seed` | int | [-1, 2147483647] | |
| `stop` | string ou array | — | |
| `tools` | array | JSON ≤ 16 KB | |
| `tool_choice` | string ou object | — | |

**Tout autre champ est silencieusement supprimé avant l'appel upstream.**
Exemples de champs supprimés : `ignore_eos`, `cache_prompt`, `system_prompt`,
`mirostat*`, `dry_*`, `xtc_*`, `id_slot`, `samplers`, `logit_bias`.

Le champ `user` est **toujours écrasé** par l'identifiant interne de l'étudiant.

### Exemple — réponse non-streaming

```bash
curl https://llm-students.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer llmstu-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b-instruct",
    "messages": [
      {"role": "system", "content": "Tu es un assistant pédagogique."},
      {"role": "user",   "content": "Explique la dérivée d'\''une fonction."}
    ],
    "max_tokens": 512,
    "temperature": 0.7
  }'
```

**Réponse 200 :**
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1714383600,
  "model": "llama-3.1-8b-instruct",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "La dérivée d'une fonction..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 187,
    "total_tokens": 229
  }
}
```

### Exemple — streaming SSE

```bash
curl https://llm-students.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer llmstu-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Bonjour"}],
    "stream": true
  }'
```

Réponse `text/event-stream` :
```
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"Bon"},...}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"jour"},...}]}

data: [DONE]
```

### Exemple Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm-students.univ-pau.fr/v1",
    api_key="llmstu-...",
)

response = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Explique les réseaux de neurones."}],
    max_tokens=512,
)
print(response.choices[0].message.content)
```

---

## Codes d'erreur

Toutes les erreurs suivent le format OpenAI :

```json
{
  "error": {
    "message": "Description lisible de l'erreur.",
    "type": "invalid_request_error",
    "code": "400"
  }
}
```

| Code HTTP | `type` | Cause courante |
|---|---|---|
| `400` | `invalid_request_error` | JSON invalide, modèle non autorisé, messages hors limites, paramètre illégal |
| `401` | `authentication_error` | Clé absente, invalide, révoquée ou expirée |
| `413` | `invalid_request_error` | Corps > 64 KB (rejeté par nginx avant FastAPI) |
| `429` | `rate_limit_error` | Burst, RPM, tokens/h, tokens/jour ou concurrence dépassés |
| `503` | `server_error` | Gateway admin injoignable |
| `504` | `server_error` | Timeout upstream (>600 s pour un stream) |
| `500` | `server_error` | Erreur interne inattendue |
