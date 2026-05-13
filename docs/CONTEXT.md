# CONTEXT — Contexte business Detective.be

> Tout ce qu'un nouvel intervenant (humain ou IA) doit savoir sur le client et son métier avant d'écrire du code.

---

## Le client

**Daniel Hurchon** — détective privé belge, dirige seul un cabinet d'enquêtes privées exerçant sous **3 marques** :

| Marque | Domaine email | Langue par défaut | Public |
|---|---|---|---|
| Detective Belgique | `contact@detectivebelgique.be` | FR | clients francophones BE |
| Detective Belgium | `contact@detectivebelgium.com` | EN (mais multilingue) | clients internationaux + NL |
| DPDH Investigations | `info@dpdhuinvestigations.be` | FR | dossiers spécifiques |

Daniel répond personnellement à tous les mails entrants. Pas de collaborateurs intermédiaires, pas d'assistant. Aujourd'hui, il utilise **Outlook ou Apple Mail** sur Mac (à confirmer côté Daniel).

---

## L'intégrateur

**Cyril Dal** (`cdal@digitalhs.biz`, société Digital HS) — consultant/intégrateur IA, livre cette solution à Daniel. C'est lui que tu (Claude Code) assistes. Cyril valide les brouillons générés avant de les transférer à Daniel pendant toute la phase MVP.

---

## Les emails — typologie observée

Volume estimé : **~50 mails/jour** au total sur les 3 boîtes, mélangeant :

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

Règle absolue : **l'agent répond TOUJOURS dans la langue du mail entrant**, jamais en traduction. La détection langue est faite par `fasttext` avant la génération.

---

## Données disponibles

Cyril a déjà fait le travail d'**anonymisation** des mails historiques de Daniel :

- 3 fichiers SQLite (un par boîte mail)
- ~1200 paires `[mail entrant] → [réponse de Daniel]` au total
- Données pseudonymisées : noms, emails, adresses, numéros de dossier remplacés par des tokens
- Sert de base RAG pour le style + corpus de bootstrap pour le guide de personnalité

> Ces données sont sensibles malgré l'anonymisation : ne jamais les commiter dans git, ne jamais les uploader en clair vers un service tiers.

---

## Prod : VPS Hostinger KVM8

- 8 vCPU, 32 Go RAM, 400 Go NVMe — largement sous-utilisé
- Cyril l'exploite pour héberger plusieurs projets clients
- L'agent y vivra dans `/opt/detective-agent/` sous user dédié `detective-agent`
- systemd unit, auto-restart, healthcheck local, alertes Telegram

---

## Canal Telegram Boss ↔ Charlie

Outre le pipeline email, **Charlie** (l'agent IA) dispose d'un **canal Telegram direct avec Daniel** :

- **Usage** : Daniel peut interroger Charlie en direct, demander un résumé de dossier, ou valider/rejeter un brouillon sans passer par l'email
- **Identité** : Daniel = le Boss, Charlie = l'assistant IA (persona distinct, ton professionnel mais accessible)
- **Phase test** : connecté au compte Telegram de **Cyril** pour itérer sans déranger Daniel
- **Phase prod** : migré sur le VPS Hostinger avec le bot dédié à Daniel
- **Alertes système** (agent down, IMAP timeout, etc.) utilisent le **même bot** mais dans un canal/thread séparé pour ne pas polluer la conversation métier

## Vision long terme

À mesure que le MVP se stabilise, on étend :

1. **Bascule en V2** : brouillons directement dans Drafts IMAP de Daniel (Outlook/Apple Mail), plus de validation Cyril
2. **Extension factures** : extraction structurée + transfert comptable
3. **Dashboard web** : supervision unifiée, métriques qualité (taux d'acceptation, latence, etc.)
4. **Bot WhatsApp** : les clients posent leurs questions via WhatsApp, l'agent répond avec la même intelligence (RAG + style Daniel)
5. **Multi-sub-agents** : un orchestrateur qui dispatch chaque type de tâche au LLM le plus adapté (cheap pour la classif, premium pour la rédaction sensible)

---

## Contraintes implicites

- **Solide 24/7** : Daniel doit pouvoir compter sur l'agent sans maintenance permanente
- **Coût plafonné** : Cyril privilégie les abonnements à coût fixe (Ollama Pro 20€/mois) plutôt que le pay-per-token incontrôlé
- **Pas d'over-engineering** : MVP simple d'abord, V2 quand qualité prouvée. Pas de Docker, Kubernetes, microservices au MVP.
- **RGPD** : données anonymisées en stockage long terme (chantier V3 : suppression mails > 28 jours)
