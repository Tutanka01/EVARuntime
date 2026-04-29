# Guide opérationnel — Gateway Étudiante

Ce document est le runbook de l'administrateur : surveillance quotidienne,
gestion des étudiants, procédures d'incident, fin de semestre.

---

## Commandes de base

Toutes les commandes CLI s'exécutent en tant que `llmstudent` en production :

```bash
sudo -u llmstudent /opt/llm-gateway-student/venv/bin/python cli.py <commande>
```

En local (dev) :

```bash
python cli.py <commande>
```

---

## Surveillance quotidienne

### Tableau de bord

```bash
python cli.py stats
```

Affiche en une commande :
- Requêtes et tokens du jour (avec répartition prompt/génération)
- Durée moyenne des requêtes
- Nombre d'étudiants actifs aujourd'hui vs inscrits
- Top modèles utilisés
- Cumul sur 7 jours

### Rapport d'utilisation

```bash
# Top consommateurs sur 7 jours
python cli.py usage-report --days 7

# Zoom sur un étudiant suspect
python cli.py usage-report --days 30 --user alice

# Activité du mois
python cli.py usage-report --days 30
```

### État du service

```bash
sudo systemctl status llm-gateway-student
sudo journalctl -u llm-gateway-student -f
curl -s https://llm-students.univ-pau.fr/health
# Attendu : {"status":"ok","db":"ok"}
```

---

## Gestion des étudiants

### Créer un étudiant

```bash
# Cas standard (quotas par défaut)
python cli.py add-student nom.prenom --email prenom.nom@etud.univ-pau.fr

# TER / PFE (quotas renforcés)
python cli.py add-student nom.prenom \
    --email prenom.nom@etud.univ-pau.fr \
    --rpm 20 \
    --daily-tokens 200000 \
    --hourly-tokens 50000 \
    --concurrent 2 \
    --notes "TER IA 2026"
```

### Lister les étudiants

```bash
python cli.py list-students
```

Colonnes affichées : ID, nom, email, actif, RPM, tokens/h, tokens/jour, streams,
nb clés actives, dernière requête, notes.

### Modifier les quotas d'un étudiant

```bash
# Augmenter uniquement le RPM
python cli.py set-quota alice --rpm 20

# Ajuster plusieurs quotas d'un coup
python cli.py set-quota alice --rpm 20 --daily-tokens 200000 --hourly-tokens 40000

# Désactiver le quota horaire (0 = off)
python cli.py set-quota alice --hourly-tokens 0
```

Les quotas modifiés sont actifs immédiatement (la prochaine requête lit la base).

### Suspendre / réactiver un étudiant

```bash
# Suspension immédiate (toutes les clés rejetées dès la prochaine requête)
python cli.py deactivate-student alice

# Réactivation
python cli.py activate-student alice
```

La suspension ne supprime pas les données. Elle permet de bloquer rapidement
sans perte d'historique.

### Supprimer un étudiant (RGPD)

```bash
python cli.py delete-student alice --yes
```

Supprime le compte, toutes ses clés API et tous ses logs d'usage (cascade FK).
**Irréversible.** À utiliser en fin de scolarité selon la politique DPO.

---

## Gestion des clés API

### Créer une clé

```bash
# Clé standard fin de semestre
python cli.py create-key alice --expires-at 2026-08-31T23:59:59+00:00

# Clé nommée (utile pour distinguer TP1 / TP2 / TER)
python cli.py create-key alice \
    --expires-at 2026-08-31T23:59:59+00:00 \
    --name TP-S2-2026
```

> La clé brute n'est affichée qu'à la création. Elle n'est pas stockée en clair.
> La copier immédiatement avant de fermer le terminal.

### Lister les clés

```bash
# Toutes les clés (tous les étudiants)
python cli.py list-keys

# Clés d'un étudiant
python cli.py list-keys --user alice
```

Code couleur : vert = valide > 30j, jaune = expire bientôt, rouge = expirée.

### Clés qui expirent bientôt

```bash
# Clés expirant dans les 30 prochains jours
python cli.py expiring-keys --days 30

# Alerte serrée (7 jours)
python cli.py expiring-keys --days 7
```

À intégrer dans une vérification hebdomadaire (cron ou script d'astreinte).

### Révoquer une clé

```bash
python cli.py revoke-key llmstu-abc12ef
```

La révocation est immédiate : la prochaine requête avec cette clé retournera 401.

---

## Procédures d'incident

### Clé API compromise (phishing, dépôt git public…)

1. **Révoquer immédiatement** :
   ```bash
   python cli.py revoke-key llmstu-<préfixe>
   ```

2. **Inspecter l'historique** dans les logs d'audit :
   ```bash
   sudo journalctl -u llm-gateway-student | grep '"key_prefix":"llmstu-<préfixe>'
   ```
   Ou dans le fichier d'audit :
   ```bash
   grep '"key_prefix":"llmstu-<préfixe>' /var/log/llm-gateway-student/audit.jsonl
   ```

3. **Évaluer l'impact** : tokens consommés, modèles utilisés, timestamps.

4. **Émettre une nouvelle clé** si l'étudiant légitime en a besoin :
   ```bash
   python cli.py create-key alice --expires-at 2026-08-31T23:59:59+00:00 --name remplacement
   ```

### Étudiant qui épuise les quotas GPU (abus)

1. Identifier via le rapport :
   ```bash
   python cli.py usage-report --days 1
   ```

2. Suspendre en urgence :
   ```bash
   python cli.py deactivate-student alice
   ```

3. Analyser l'usage, contacter l'étudiant, ajuster les quotas ou réactiver.

### Service indisponible (5xx)

```bash
sudo systemctl status llm-gateway-student
sudo journalctl -u llm-gateway-student --since "5 minutes ago"
curl -s https://llm-students.univ-pau.fr/health
```

Si `"db":"error"` dans `/health` : vérifier `/var/lib/llm-gateway-student/students.db`
(droits, espace disque, verrou SQLite).

Si la gateway admin est injoignable, la gw-étudiante renvoie 503 proprement
sans tentative de fallback. Vérifier la connectivité NIC admin → gw-admin.

### Révocation en masse (compromission de la base entière)

En cas de doute sur l'intégrité de la base étudiante :

```bash
# Suspendre TOUS les étudiants d'un coup
sqlite3 /var/lib/llm-gateway-student/students.db \
    "UPDATE users SET is_active = 0"

# Relancer proprement après investigation
sudo systemctl restart llm-gateway-student
```

---

## Procédure de fin de semestre

### 1. Rapport final d'usage

```bash
python cli.py usage-report --days 120   # adapter à la durée du semestre
```

Exporter si besoin :
```bash
python cli.py usage-report --days 120 2>&1 | tee rapport-S2-2026.txt
```

### 2. Vérifier les clés expirées

```bash
python cli.py list-keys | grep -E "expir|révoquée"
```

### 3. Supprimer les étudiants sortants (RGPD)

Selon la politique DPO (liste fournie par la scolarité) :

```bash
python cli.py delete-student nom.prenom --yes
```

### 4. Créer les clés pour le nouveau semestre

```bash
# Pour chaque nouvel étudiant de la liste
python cli.py add-student prenom.nom --email prenom.nom@etud.univ-pau.fr
python cli.py create-key prenom.nom \
    --expires-at 2027-01-31T23:59:59+00:00 \
    --name S1-2026-2027
```

---

## Monitoring recommandé

### Alertes à configurer

| Condition | Seuil suggéré | Action |
|---|---|---|
| Erreurs 5xx/minute | > 5 | Vérifier le service et la gw-admin |
| Erreurs 429/minute | > 50 | Identifier l'étudiant, quota atteint ou abus |
| Réponse `/health` | ≠ 200 | Redémarrage automatique via systemd |
| Espace disque `/var/lib/…` | > 80 % | Purger les vieux logs d'usage |
| Clé expirant dans 7j | > 0 | Notifier l'admin |

### SLO recommandés

- Disponibilité edge : **99 %** pendant les périodes de TP
- Erreurs 5xx hors maintenance : **< 1 %**
- Temps de révocation d'une clé : **< 5 minutes**
- Temps de réponse médian (hors génération) : **< 200 ms**

### Logs utiles

```bash
# Logs applicatifs (erreurs, démarrage, requêtes importantes)
sudo journalctl -u llm-gateway-student -f

# Logs d'audit (structurés JSON, sans contenu)
tail -f /var/log/llm-gateway-student/audit.jsonl | python3 -m json.tool

# Logs nginx (accès par IP)
sudo tail -f /var/log/nginx/access.log | grep llm-gateway-student
```

Format d'un log d'audit :
```json
{
  "ts": 1714383600.123,
  "request_id": "uuid-v4",
  "student_user_id": 42,
  "key_prefix": "llmstu-abc12",
  "client_ip_hash": "8f3a1b2c",
  "model": "llama-3.1-8b-instruct",
  "prompt_chars": 350,
  "prompt_tokens": 87,
  "completion_tokens": 214,
  "duration_ms": 1840,
  "status": 200
}
```

> Le contenu des messages et des réponses n'est **jamais** loggé.
> L'IP est remplacée par un HMAC à sel quotidien (non réversible sans le secret).
