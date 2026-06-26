# CONTEXT — Contexte business Detective.be

> Tout ce qu'un nouvel intervenant (humain ou IA) doit savoir sur le client et son métier avant d'écrire du code.

---

## Le client

**Daniel Hurchon** — détective privé belge, dirige seul un cabinet d'enquêtes privées exerçant sous **4 marques** :

| Marque | Domaine email | Langue par défaut | Public |
|---|---|---|---|
| Detective Belgique | `contact@detectivebelgique.be` | FR | clients francophones BE |
| Detective Belgium | `contact@detectivebelgium.com` | EN (mais multilingue) | clients internationaux + NL |
| DPDH Investigations | `info@dpdhuinvestigations.be` | FR | dossiers spécifiques |
| Detectives Belgique | `info@detectives-belgique.be` | FR (à confirmer) | clients francophones BE |

Daniel répond personnellement à tous les mails entrants. Pas de collaborateurs intermédiaires, pas d'assistant. Aujourd'hui, il utilise **Outlook ou Apple Mail** sur Mac (à confirmer côté Daniel).

---

## L'intégrateur

**CDAL** (`cdal@digitalhs.biz`, société Digital HS) — consultant/intégrateur IA, livre cette solution à Daniel. C'est lui que tu (Claude Code) assistes. CDAL valide les brouillons générés avant de les transférer à Daniel pendant toute la phase MVP.

---

## Les emails — typologie observée

Volume estimé : **~50 mails/jour** au total sur les 4 boîtes, mélangeant :

1. **Demandes clients** (cible prioritaire de l'agent)
   - Nouveau prospect qui décrit un soupçon (infidélité, fraude, recherche de personne, surveillance, etc.)
   - Client existant qui pose une question sur un dossier en cours
   - Demande de devis
   - Relance d'un échange précédent
2. **Factures / compta** : fournisseurs, OVH, Infomaniak, comptable, etc.
3. **Newsletters** : marketing B2B, lettres d'info pro
4. **Spam / phishing** : faux clients, tentatives d'arnaque
5. **Urgences** : situation client critique, deadline imminente
6. **Autre** : notifications systèmes (Stripe, LinkedIn, Google), confirmations automatiques, échanges internes

Au MVP, **seules les demandes clients sont traitées** (génération de brouillon). Tout le reste est tagué et passe à travers.

---

## Sensibilités métier

Le métier de détective privé impose des **garde-fous éditoriaux** que l'agent doit respecter dans ses brouillons :

- **Confidentialité** : rappels discrets sur la confidentialité du cabinet, jamais de divulgation d'autres dossiers
- **Pas d'engagement légal/contractuel par email** : devis ferme, prix, délais → toujours renvoyer vers un appel ou un rendez-vous
- **Pas de jugement** sur la situation décrite (ex : un mari qui suspecte sa femme — neutralité absolue)
- **Vouvoiement par défaut**, tutoiement très rare
- **Ton professionnel mais humain**, jamais obséquieux ni distant
- **Concision** : les mails de Daniel sont généralement courts, vont à l'essentiel
- **Toujours une porte de sortie vers un échange humain** : "n'hésitez pas à m'appeler", "je vous propose un rendez-vous"

Ces règles sont déjà encodées dans le placeholder `app/prompts/personality_daniel.txt` et seront raffinées par `scripts/extract_personality.py` à partir des 1200 paires Q/R.

---

## Multilingue

- **FR** : marché principal (Belgique francophone, France)
- **NL** : marché secondaire (Belgique néerlandophone — nombreux clients sur `detectivebelgium.com`)
- **EN** : clients internationaux

Règle absolue : **la réponse générée est TOUJOURS en français** (langue de travail de Daniel), quelle que soit la langue du mail entrant. Si le mail entrant est en NL/EN/DE/ES/autre, le brouillon est enrichi (v1.21.0) avec **4 blocs** : email d'origine + traduction FR + proposition FR + traduction dans la langue source (aide lecture multilingue — Daniel n'a plus à déchiffrer une langue qu'il ne maîtrise pas). La détection langue est faite par **`langdetect`** (toutes langues BCP-47 supportées) avant la génération — `fasttext` a été abandonné (ne build pas sur Mac ARM).

---

## Données disponibles

CDAL a déjà fait le travail d'**anonymisation** des mails historiques de Daniel :

- 3 fichiers SQLite (un par boîte mail)
- ~1200 paires `[mail entrant] → [réponse de Daniel]` au total
- Données pseudonymisées : noms, emails, adresses, numéros de dossier remplacés par des tokens
- Sert de base RAG pour le style + corpus de bootstrap pour le guide de personnalité

> Ces données sont sensibles malgré l'anonymisation : ne jamais les commiter dans git, ne jamais les uploader en clair vers un service tiers.

---

## Prod : VPS Hostinger KVM8

- 8 vCPU, 32 Go RAM, 400 Go NVMe — largement sous-utilisé
- CDAL l'exploite pour héberger plusieurs projets clients
- L'agent y vivra dans `/opt/DETECTIVE/` (Docker Compose + Traefik externe), healthcheck FastAPI sur `127.0.0.1:8765`
- Auto-restart via Docker, alertes système via Slack (`#detective`) + Resend + cron watchdog + Healthchecks.io (4 niveaux anti-crash silencieux)

---

## Canal Slack Boss ↔ Charlie

Outre le pipeline email, **Charlie** (l'agent IA) dispose d'un **canal Slack direct avec Daniel** sur `#detective` (workspace CDAL) via un Slack Bot interactif (`slack_bolt`) :

- **Usage** : Daniel peut interroger Charlie en direct (@mention ou DM), demander un résumé narratif de dossier, faire une recherche factuelle, ou valider/rejeter un brouillon sans passer par l'email
- **Identité** : Daniel = le Boss, Charlie = l'assistant IA (persona distinct, ton professionnel mais accessible)
- **Alertes système** (agent down, IMAP timeout, etc.) utilisent le **même canal** mais dans un thread séparé pour ne pas polluer la conversation métier
- **Note** : un module Telegram est conservé dans le code mais **inactif** (dépriorisé — Slack suffit)

## Vision long terme

À mesure que le MVP se stabilise, on étend :

1. **Bascule en V2** : brouillons directement dans Drafts IMAP de Daniel (Outlook/Apple Mail), plus de validation CDAL
2. **Extension factures** : extraction structurée + transfert comptable
3. **Dashboard web** : supervision unifiée, métriques qualité (taux d'acceptation, latence, etc.)
4. **Bot WhatsApp** : les clients posent leurs questions via WhatsApp, l'agent répond avec la même intelligence (RAG + style Daniel)
5. **Multi-sub-agents** : un orchestrateur qui dispatch chaque type de tâche au LLM le plus adapté (cheap pour la classif, premium pour la rédaction sensible)

---

## Contraintes implicites

- **Solide 24/7** : Daniel doit pouvoir compter sur l'agent sans maintenance permanente
- **Coût plafonné** : CDAL privilégie les abonnements à coût fixe (Ollama Pro 20€/mois) plutôt que le pay-per-token incontrôlé
- **Pas d'over-engineering** : MVP simple d'abord, V2 quand qualité prouvée. Pas de Docker, Kubernetes, microservices au MVP.
- **RGPD** : données anonymisées en stockage long terme (chantier V3 : suppression mails > 28 jours)
