# Checklist Démonstration Daniel — Vendredi 22 Mai 2026

> Heure : 12h00 Bali (UTC+8) = 05h00 Bruxelles / 06h00 heure d'été CEST
> Version cible : v1.13.4+
> URL cockpit : https://detective.digitalhs.biz
> Cible : montrer que Charlie est un vrai "second cerveau" — pas juste un chatbot

---

## A. Pré-checks (à faire 30 min avant la démo)

- [ ] **Container actif** : `docker compose ps` sur VPS → `detective-agent` Up
- [ ] **Healthcheck vert** : `curl https://detective.digitalhs.biz/health` → 200
- [ ] **3 boîtes mail pollent** : dernier cycle dans les logs → `poller.cycle_empty` ou `poller.found`
- [ ] **Bot Slack Charlie connecté** : vérifier présence online dans le workspace Slack
- [ ] **Cerveau2 up** : `curl https://cerveau2-det.digitalhs.biz/health` → 200
- [ ] **Navigateur propre** : vider le cache/HTMX pour éviter les vieux JS (Ctrl+Shift+R)

---

## B. Scénario démo — Minute par minute (~15 min)

### 1. Connexion cockpit (1 min)
- [ ] Ouvrir https://detective.digitalhs.biz
- [ ] Login opérateur → accès inbox
- [ ] Pointer la version en bas de page : doit afficher **v1.13.4**

### 2. Inbox active (2 min)
- [ ] Montrer les 3 onglets de boîtes (D_FR, D_NL, D_PD)
- [ ] Filtrer par catégorie "demande_client" → montrer que les bons mails remontent
- [ ] Ouvrir une conversation → montrer la classification + la priorité + le brouillon AI si présent

### 3. Charlie Chat — Questions identitaires (3 min) ⭐ CRITIQUE
- [ ] **Question 1** : "Qui est l'épouse de CDAL ?"
  - Attendu : réponse instantanée "**Sarah**" (extraction directe vault, pas de "aucun résultat")
- [ ] **Question 2** : "Qui est la compagne de CDAL ?"
  - Attendu : même réponse "Sarah" (synonyme épouse/compagne)
- [ ] **Question 3** : "Quel est le nom de la femme de CDAL ?"
  - Attendu : même réponse "Sarah"
- [ ] Vérifier que la **section vault** s'affiche sous la bulle (document source visible)

### 4. Charlie Chat — Recherche métier (3 min)
- [ ] **Question 4** : "As-tu reçu des demandes de filature cette semaine ?"
  - Attendu : liste des emails `category=surveillance` + liens cliquables vers inbox
- [ ] **Question 5** : "Résume le dossier Dutry"
  - Attendu : synthèse narrative (pas un dump SQL) avec dates et étapes clés
- [ ] **Question 6** : "Combien de demandes client aujourd'hui ?"
  - Attendu : nombre exact via COUNT SQL

### 5. Charlie Chat — Mémoire S2/S3 (2 min)
- [ ] **Question 7** : "Retiens que le client Dupont prefère être contacté par email"
  - Attendu : "C'est noté dans ma mémoire, Daniel !"
- [ ] **Question 8** : "Souviens-toi du client Dupont"
  - Attendu : Charlie rappelle le fait sauvegardé
- [ ] Cliquer **✅ Bonne réponse** sur une réponse → doit afficher "Merci ! Charlie retient cette réponse."
- [ ] Cliquer **❌ À corriger** → formulaire visible, envoi possible

### 6. Slack Bot Charlie (2 min)
- [ ] Mentionner `@Charlie` dans `#detective` avec une question simple
  - Attendu : réponse en thread avec même qualité que le cockpit
- [ ] DM à Charlie avec "Qui est Sarah ?"
  - Attendu : réponse identitaire correcte

### 7. Pipeline email live (2 min) — si timing le permet
- [ ] Envoyer un vrai email test à une des 3 boîtes (depuis Gmail perso)
- [ ] Attendre le cycle poller (max 5 min)
- [ ] Rafraîchir l'inbox → le mail doit apparaître classé
- [ ] Si `demande_client` : vérifier que CDAL reçoit le brouillon Resend

---

## C. Garde-fous de sécurité à mentionner (1 min)

- [ ] **Anonymisation** : les vrais noms ne partent jamais dans le cloud LLM
- [ ] **Zone rouge** : si un document sensible est détecté, le LLM n'est pas appelé
- [ ] **Pas de données brutes** : Charlie ne montre jamais sender/body_preview en clair
- [ ] **Audit log** : toute question est loguée (traçabilité judiciaire)

---

## D. Fallbacks si un scénario plante

| Si ça plante | Plan B |
|---|---|
| Question identitaire échoue | Montrer le document dans la section vault + expliquer que l'extraction est en cours d'affinage |
| Latence >10s | Dire "Charlie réfléchit" + passer à la question suivante, revenir après |
| Slack bot down | Se concentrer sur le cockpit web, c'est le canal principal |
| Boîte mail bloquée | Montrer l'inbox avec les données déjà indexées (pas de polling live) |
| Cerveau2 down | Charlie répond quand même avec SQL + mémoire seuls (dégradation silencieuse) |

---

## E. Post-démo — Actions à noter

- [ ] Recueillir 3 retours terrain de Daniel sur la qualité des brouillons
- [ ] Identifier les questions qui ont échoué → tickets d'amélioration
- [ ] Décider : bascule vers Claude Sonnet 4 via OpenRouter ? (coût ~0.003€/req, fiabilité 99%)
- [ ] Planifier S4 : Telegram prod, backup B2, monitoring 1 semaine
