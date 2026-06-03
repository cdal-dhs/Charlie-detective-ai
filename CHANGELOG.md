# Changelog Charlie AI — Detective.be

## [1.21.0] — 2026-06-03 (aide à la lecture multilingue pour Daniel + retry-draft endpoint)

### Ajouté
- **Aide à la lecture multilingue** : quand un mail client arrive en néerlandais, anglais, allemand, espagnol (ou toute autre langue ≠ FR), le brouillon généré est maintenant enrichi avec 4 blocs visuels pour aider Daniel à lire et répondre :
  1. **Email d'origine** (langue source, brut) — pour référence
  2. **Traduction FR** (pour que Daniel lise) — kimi-k2, temperature 0.1
  3. **Proposition de réponse** (toujours en français — langue de travail de Daniel)
  4. **Traduction de la proposition** dans la langue du client — pour copie-coller si Daniel souhaite répondre dans la langue source
- Si le mail est en français : aucun cadre, brouillon FR direct (comportement antérieur inchangé).
- Si une traduction échoue (LLM timeout) : garde-fou silencieux, le brouillon FR est conservé + une note ⚠️ en tête indique "traductions indisponibles".
- **Module `app/pipeline/translator.py`** : 2 fonctions `translate_to_fr()` et `translate_from_fr()`, garde-fous `try/except` + log warning (ne casse jamais le pipeline), troncature à 12K chars pour éviter timeout.
- **Module `app/pipeline/draft_renderer.py`** : `render_draft_with_translations()` — composition des 4 blocs avec séparateurs visuels.
- **`Language = str`** au lieu de `Literal["fr", "nl", "en"]` — toute langue BCP-47 est désormais supportée (néerlandais, anglais, allemand, espagnol, italien, portugais, etc.). Affichage humain via `language_label()`.
- **Endpoint `POST /api/drafts/{mail_id}/retry`** : force la régénération d'un brouillon (utilise le body complet, plus le body_preview tronqué 2K). Utile pour les mails classifiés `demande_client` dont le brouillon n'a pas été généré (cycle interrompu, exception silencieuse).
- **Bouton "Régénérer"** dans la conversation cockpit : redirige vers `/retry` au lieu de `/regenerate` (qui était un stub "feature planned V2").

### Changé
- **`_build_messages` (generator)** : la langue de réponse forcée est désormais **toujours le français** (langue de travail de Daniel). Avant : forçait la langue détectée du mail entrant. Logique de traduction sortie du LLM et placée dans le pipeline post-génération (plus déterministe, plus rapide, plus contrôlable).
- **`draft_generate` API** : utilise désormais `body` (complet) au lieu de `body_preview` (tronqué 2K). Le brouillon généré a plus de contexte, meilleure qualité.
- **`GenerationResult`** : nouveau champ `raw_draft` (proposition FR brute sans enrichissement) en plus de `draft` (texte final affiché enrichi avec traductions si ≠ FR).

---

## [1.20.10] — 2026-06-02 (fix recherche factuelle téléphone + court-circuit réponses Cerveau2 contradictoires)

### Fixé
- **Recherche factuelle par numéro de téléphone** : le numéro `0488/411192` n'était pas trouvé malgré sa présence en base. Trois bugs cumulés corrigés :
  1. `is_safe_sql()` rejetait les SQL avec `replace(...)` (fonction SQL standard) car le mot "replace" est dans `_DANGEROUS_SQL` (pour bloquer `REPLACE INTO`). Désormais `replace(` est ignoré avant le check.
  2. `ORDER BY received_at DESC` sur du texte RFC 2822 (`"Fri, 9 Jan 2026..."`) faisait un tri lexicographique (`W` > `F`), éjectant le bon résultat. Fix : `ORDER BY id DESC LIMIT 20`.
  3. Le SQL faisait `OR` entre le numéro et le mot "téléphone", polluant les résultats. Fix : si le meilleur keyword est un numéro (score ≥30), le WHERE ne garde que ce numéro.
- **Tri archives historiques** : `ORDER BY date DESC` dans les DB `boite1/2/3.sqlite` avait le même bug lexicographique. Fix : `LIMIT 200` par DB sans `ORDER BY`, tri global en Python avec `parsedate_to_datetime`, puis `[:limit]`.
- **vault_question allégée** : Cerveau2 recevait `"0488411192 téléphone"` ce qui diluait la recherche sémantique (dense search = implicit AND). Fix : si le meilleur keyword est un numéro, `vault_question = kws[0]` (numéro seul).
- **Faux négatif Cerveau2** : le LLM de synthèse Cerveau2 disait "pas trouvé" alors que le numéro était dans le `context`. Détection : si `_bad_vault` match mais que le numéro recherché est dans `vault_answer` → considérer que l'info est là.
- **Réponses contradictoires Cerveau2** : quand Cerveau2 contenait encore des négations malgré les probants en base, on affiche désormais une réponse propre (`"Voici ce que j'ai trouvé :"`) au lieu du texte contradictoire du LLM.
- **Déduplication des probants** : un même email existait dans `mail_processed` (sender réel) et `boite2.sqlite` (sender anonymisé), apparaissant en double. Fix : déduplication par `(subject.lower(), received_at)` sans `sender`.

### Ajouté
- **Note technique** : `docs/CERVEAU2_RECHERCHE_FACTUELLE.md` — patterns et pièges pour la recherche factuelle via Cerveau2 (réutilisable pour Second Cerveau Pro).

---

## [1.19.3] — 2026-05-30 (hotfix critique — bascule modèle chat Kimi K2 + garde post-LLM)

### Fixé
- **Bascule modèle chat : `gemma4:31b` → `openai/kimi-k2`** (Ollama Pro). `gemma4:31b` générait des réponses de refus systématiques du type "Tu n'as pas posé de question précise..." sur les requêtes factuelles (factures, archives). Kimi K2 est le modèle principal de l'agent, multilingue, et ne présente pas ce comportement de refus.
- **Garde `_BAD_RESPONSE` post-LLM-final** : les patterns de refus (`_BAD_VAULT`) sont désormais aussi filtrés sur la réponse brute du LLM final. Si le LLM final génère du garbage malgré tout, la réponse est court-circuitée et remplacée par une réponse de secours basée sur les données SQL/archives.
- **Timeout Cerveau2 identifié** : les logs de prod montrent que `query_vault` timeout après 15s sur les requêtes complexes. `vault_answer` est donc `None` et mon fix v1.19.2 ne s'appliquait pas. La cause racine était le modèle chat, pas le vault.

---

## [1.19.2] — 2026-05-30 (hotfix — purge vault_answer garbage du contexte LLM final)

### Fixé
- **Purge du `vault_answer` corrompu du contexte LLM final** : quand Cerveau2 répond du garbage (patterns `_BAD_VAULT` comme "tu n'as pas posé de question" ou réponse non pertinente sémantiquement), le code sautait correctement le bypass direct (ligne 1832) mais **injectait quand même le garbage dans le prompt du LLM final** (ligne 1888). Le LLM final, obéissant à la règle "Le SECOND CERVEAU est la SOURCE PRINCIPALE", reproduisait le garbage au lieu de synthétiser les résultats SQL/archives pertinents. Désormais, un `vault_answer` marqué `bad=True` ou `relevant=False` est **complètement retiré du contexte** du LLM final. Log explicite `charlie.vault_context_purged` pour le traçage.
- **Exemple corrigé** : "retrouve moi mes factures d'hôtel 2025 et 2026" — avant : réponse garbage "Tu n'as pas posé de question précise... Lampaert... Dusza...". Après : le LLM final reçoit uniquement les résultats SQL/archives des factures d'hôtel et génère une réponse propre.

---

## [1.19.1] — 2026-05-30 (hotfix — scoring mots-clés Charlie + masquage vault)

### Fixé
- **Scoring mots-clés corrigé** : `_build_keyword_sql` et `_archive_task` choisissaient des verbes d'action ("retrouve", score 8) à la place de noms concrets ("hotel", score 5). Nouvelle fonction `_extract_keywords()` avec scoring sémantique : **bonus +15** pour les noms concrets (hotel, facture, devis, contrat, rapport, vol, train, restaurant, parking, document, etc.) et **pénalité −15** pour les verbes d'action (retrouve, cherche, donne, montre, liste, affiche, envoie, etc.). Le SQL et les archives historiques ciblent désormais le bon objet de recherche.
- **Retrait de "facture/factures/devis" de STOP_WORDS** : ces mots étaient injustement exclus du scoring. Ils sont désormais dans `SEMANTIC_BOOST` et correctement utilisés comme mots-clés de recherche.
- **Filtre année dans `_build_keyword_sql`** : quand une année est détectée dans la question (ex: "2025"), le SQL ajoute automatiquement `processed_at >= 'YYYY-01-01' AND processed_at < 'YYYY+1-01-01'` pour restreindre la base courante.
- **Masquage tableau quand réponse vient du vault** : quand Cerveau2 répond directement (`vault_answer` utile) et que le SQL n'est pas le principal vecteur de réponse, le tableau SQL est masqué (`hide_rows=True`). Cela évite l'affichage de résultats non pertinents sous une réponse narrative correcte.
- **Dédoublonnage logique** : la logique d'extraction de mots-clés était dupliquée entre `_build_keyword_sql` et `_archive_task`. Désormais centralisée dans `_extract_keywords()` — un seul point de vérité.

---

## [1.19.0] — 2026-05-29 (release — résumé de dossier narratif + Ollama Cloud stable)

### Ajouté
- **Résumé de dossier narratif** : quand Daniel demande un résumé de dossier ("résume le dossier X"), Charlie assemble les contenus complets des emails (body, pas preview) et appelle le LLM avec un prompt ultra-ciblé pour produire **UN SEUL PARAGRAPHE FLUIDE ET NARRATIF**. Le LLM raconte l'histoire du dossier : client, type de demande, dates importantes, et montants financiers.
- **Masquage tableau SQL dans le chat** : nouveau flag `hide_rows` dans `CharlieResult`. Quand un résumé de dossier est généré, le template web n'affiche plus le tableau SQL brut sous la réponse — seul le paragraphe narratif est visible.

### Changé
- **Modèle chat : deepseek-v4-pro → gemma4:31b** (Ollama Cloud). deepseek-v4-pro ne savait pas synthétiser (retournait vide ou reproduisait des tableaux). gemma4:31b produit des résumés narratifs fluides.
- **Provider litellm : `ollama_chat/` → `openai/`**. `ollama_chat/` force litellm vers `localhost:11434` (Ollama local). `openai/` avec `api_base=https://ollama.com/v1` pointe vers Ollama **Cloud** (abonnement 20€/mois).
- **Fallback : OpenRouter/Claude → Ollama Cloud/glm-5.1**. Claude 3.5 Sonnet n'est plus disponible sur OpenRouter (404). Le fallback est désormais glm-5.1 sur Ollama Cloud, toujours inclus dans l'abonnement.

### Fixé
- **Corrections DB settings** : la table `app_settings` dans `agent_state.db` avait des vieilles valeurs (`llm_model_default`, `llm_model_classifier`, `llm_model_fallback`) qui prisaient sur `.env`. Mise à jour directe en DB + redémarrage container.
- **Secours anti-tableau** : si le LLM final échoue aussi, le secours ne produit jamais de tableau brut quand `is_dossier_summary` — message propre avec les sujets d'emails.

---

## [1.18.15] — 2026-05-29 (hotfix résumé de dossier — LLM Claude + masquage tableau)

### Fixé
- **LLM Claude pour les résumés de dossier** : le modèle deepseek-v4-pro ne savait pas synthétiser (vide ou tableaux). Désormais, les résumés de dossier utilisent **Claude 3.5 Sonnet via OpenRouter** (`llm_model_fallback`) qui excelle en synthèse narrative.
- **Prompt parfait pour le LLM** : instruction absolue "UN SEUL PARAGRAPHE FLUIDE ET NARRATIF. Pas de puces. Pas de tableaux. Pas de listes à puces." Le LLM reçoit les contenus complets des emails (body, 2500 chars chacun) et doit raconter l'histoire du dossier.
- **Masquage du tableau SQL dans le chat** : nouveau flag `hide_rows` dans `CharlieResult`. Quand un résumé de dossier est généré, le template web n'affiche plus le tableau SQL brut sous la réponse — seul le paragraphe narratif est visible.
- **Retry + garde anti-format** : 2 tentatives avec Claude + vérification que la réponse ne commence pas par une puce (`- `) et ne contient pas de tableau (`|`).
- **Contexte emails pour le LLM final** : si Claude échoue et qu'on passe au LLM final, celui-ci reçoit les contenus des emails (body) au lieu du tableau de métadonnées.

---

## [1.18.14] — 2026-05-29 (hotfix résumé de dossier — extraction Python intelligente, plus de LLM)

### Fixé
- **Remplacement total du bypass LLM par extraction Python** : le LLM deepseek-v4-pro ne savait pas synthétiser les emails (retournait vide ou reproduisait les tableaux). Désormais, `_build_dossier_summary_from_emails()` extrait automatiquement et déterministiquement :
  - Nom du client (patterns Achternaam/Voornaam, Nom/Prénom, Name/Naam)
  - Montants financiers (regex €, euro, EUR — filtre 10€ à 500K€, déduplication)
  - Dates importantes (des headers + dans le texte)
  - Type de demande (catégorie + mots-clés dans le body)
  → Formate un résumé structuré propre sans appeler de LLM.
- **Fallback LLM conservé mais secondaire** : si l'extraction Python ne trouve pas assez d'infos, on essaie encore une fois le LLM avec un prompt ultra-court. Si ça échoue aussi, retour direct d'un message propre avec les sujets d'emails.
- **Anti-tableau garanti** : quand `is_dossier_summary` est True, le code retourne TOUJOURS avant d'atteindre le LLM final qui reproduisait les tableaux `_sanitize_rows_for_prompt`.

---

## [1.18.13] — 2026-05-29 (hotfix résumé de dossier — body complet + retry + anti-tableau secours)

### Fixé
- **_build_keyword_sql : remonte `substr(body, 1, 3000)`** : la requête SQL par mot-clé remonte désormais le contenu complet du mail (tronqué à 3000 caractères) en plus du `body_preview`. Cela permet au bypass de résumé de dossier de voir les montants financiers et les détails cachés dans le corps complet.
- **Bypass résumé de dossier — retry + anti-BAD** : le bypass effectue désormais **2 tentatives** si le LLM retourne une réponse vide ou contenant "pas trouvé". Un garde `_BAD_RESPONSE` filtre les réponses inutiles avant de les retourner à Daniel.
- **Bypass — `body` prioritaire sur `body_preview`** : pour les emails de `mail_processed`, le bypass utilise la colonne `body` (complète, 3000 chars) au lieu du `body_preview` tronqué (~500 chars). Les archives historiques continuent d'utiliser leur `body_full` enrichi (v1.18.12).
- **Contexte LLM final — pas de tableau SQL quand `is_dossier_summary`** : si le bypass échoue et qu'on passe au LLM final, le contexte injecte les **contenus des emails** (body) au lieu du tableau de métadonnées `_sanitize_rows_for_prompt()`. Le LLM final a donc le texte des emails à synthétiser, pas des IDs et des statuts.
- **Secours anti-tableau pour résumé de dossier** : si même le LLM final échoue, le secours ne produit plus jamais de tableau brut quand `is_dossier_summary`. Il retourne un message propre avec la liste des sujets d'emails trouvés, sans dump technique.

---

## [1.18.12] — 2026-05-29 (hotfix body_full archives — 3000 chars au lieu du preview tronqué)

### Fixé
- **_search_historical_by_keyword() : body_full prioritaire** : les `body_preview` des DB historiques sont souvent incomplets ou contiennent uniquement les citations d'emails (ex: "\nEnvoyé de mon iPad\n> Le 7 févr..."). Le montant financier (200€ + 150€ + 1740€) était caché plus loin dans le `body_full` et invisible pour le LLM. Désormais, le `body_full` complet est utilisé systématiquement (tronqué à 3000 caractères) au lieu du `body_preview` partiel.

---

## [1.18.11] — 2026-05-29 (hotfix extraction dossier_id + bypass résumé LLM ciblé)

### Fixé
- **Extraction dossier_id — noms propres** : `_extract_dossier_id()` ne matchait que les codes ALL-CAPS (ADF) et `N°X`. Le pattern `_DOSSIER_RE` original utilisait `(?i:...)` qui ne fonctionnait pas comme attendu pour capturer les noms propres comme "Lampaert". Ajout d'un pattern explicite `_DOSSIER_NAME_RE = re.compile(r"[Dd][Oo][Ss]{2}[Ii][Ee][Rr]\s+([A-Z][a-zA-Z]+)")` pour capturer les noms propres après "dossier". Exclusion des faux positifs (client, général, monsieur, madame).
- **Bypass LLM ciblé pour les résumés de dossier** : quand `is_dossier_summary` est détecté ("résume", "synthèse", "infos", "détails" + un dossier_id identifié) ET qu'on a des emails pertinents, un **appel LLM spécifique et isolé** est fait avec un prompt ultra-ciblé contenant **uniquement** les body_preview des emails (pas de tableau SQL, pas de métadonnées brutes). Le prompt contient une instruction absolue : "SYNTHÉTISE le contenu des emails ci-dessus en UN SEUL PARAGRAPHE fluide et direct. Mentionne OBLIGATOIREMENT : nom du client, type de demande, dates importantes, et TOUS les montants financiers."
- **Suppression du contexte bruit** : dans ce bypass, le LLM ne reçoit PAS le tableau SQL de `_sanitize_rows_for_prompt()`, PAS la liste des archives historiques avec sujets/catégories, et PAS les notes Cerveau2 non pertinentes. Seuls les body_preview des emails du dossier sont injectés.

---

## [1.18.10] — 2026-05-29 (hotfix prompt + contexte archives — synthèse dossier)

### Fixé
- **Prompt LLM — règles anti-tableau + pro-synthèse** : ajout des règles 8, 9, 10 dans le prompt final :
  - Règle 8 : "Ne reproduis JAMAIS les tableaux de données bruts, les listes d'emails avec leurs métadonnées, ou les extraits techniques. Tu dois SYNTHÉTISER le contenu en langage naturel fluide."
  - Règle 9 : "Si Daniel demande un résumé de dossier, extrais et présente les informations clés : nom du client, type de demande, dates importantes, montants financiers. Un paragraphe clair et direct."
  - Règle 7 modifiée : "Daniel demande une SYNTHÈSE ou une INFO."
- **Contexte archives — mode synthèse vs mode liste** : quand `is_list_request` est faux (question normale ou résumé), le contexte injecté dans le prompt contient désormais le **body_preview** des emails historiques (jusqu'à 10 emails, 1500 caractères chacun, hard limit 8000 caractères total) au lieu d'une simple liste de sujets. Cela permet au LLM de voir le contenu (ex: proposition financière Lampaert avec les montants) et de le résumer.
- **Mode liste préservé** : quand `is_list_request` est vrai, le comportement ancien est conservé (liste des sujets avec dates/catégories).

---

## [1.18.9] — 2026-05-29 (hotfix keywords — normalisation accents + tri par pertinence)

### Fixé
- **Extraction mots-clés — normalisation des accents** : la liste de stop-words contenait "resume" (sans accent) mais pas "résume" (avec accent). Résultat : la question "Résume le dossier Lampaert" utilisait "Résume" comme mot-clé de recherche au lieu de "Lampaert". Désormais, les accents sont normalisés (`normalize("NFD")`) avant comparaison avec les stop-words.
- **Tri par pertinence** : au lieu de prendre le premier mot-clé trouvé dans la question (qui est souvent un verbe générique), les mots-clés sont maintenant triés par score de pertinence : +10 pour les noms propres (majuscule initiale) et +1 par caractère de longueur. "Lampaert" (nom propre, 8 caractères = score 18) bat "Résume" (verbe, 6 caractères = score 6).
- **Nouveaux stop-words** : ajout de "avec", "principaux", "principales", "important", "importants", "details", "detail", "information", "informations".
- **Même logique dans `_build_keyword_sql()`** : la recherche SQL sur `mail_processed` utilise le même algorithme de tri par pertinence.

---

## [1.18.8] — 2026-05-29 (hotfix archives — recherche body_full + fallback preview)

### Fixé
- **Archives historiques — recherche dans `body_full`** : `_search_historical_by_keyword()` ne cherchait que dans `subject`, `body_preview` et `sender`. Les réponses avec citations (ex: email de réponse à un formulaire) ont souvent un `body_preview` vide car ils commencent par des sauts de ligne ou des headers. Désormais, `body_full LIKE ?` est aussi inclus dans la clause WHERE.
- **Archives — fallback body_full quand preview vide** : quand un email historique est trouvé mais que son `body_preview` est vide ou < 50 caractères, les 800 premiers caractères de `body_full` sont extraits et retournés comme preview. Cela permet au LLM de voir le contenu des réponses avec proposition financière (ex: dossier Lampaert — offerte 200€ + 150€ + 1740€ + voorschot 1263.24€).

---

## [1.18.7] — 2026-05-29 (hotfix Charlie recherche factuelle + anti-hallucination)

### Fixé
- **Charlie — recherche par mot-clé SQL** : nouvelle fonction `_build_keyword_sql()` qui génère un `SELECT ... WHERE subject LIKE '%keyword%' OR body LIKE '%keyword%'` pour les questions factuelles spécifiques (ex: "résume le dossier Lampaert"). Déclenchée quand `_build_status_sql()` et `_build_count_sql()` retournent `None`, donc en complément du pipeline existant.
- **Charlie — recherche archives sans dossier_id** : `_archive_task()` cherchait uniquement quand un `dossier_id` était extrait (pattern `N°X`, hash, ALL-CAPS). Désormais, si aucun `dossier_id` n'est trouvé, les mots-clés significatifs de la question sont extraits et utilisés comme mot-clé de recherche dans les 3 DB historiques.
- **Charlie — guard anti-hallucination** : si après toutes les recherches (Cerveau2, SQL courant, archives, mémoire, corrections) **aucune source n'a de données**, l'appel au LLM final est court-circuité et Charlie retourne : "Je n'ai trouvé aucune information sur ce sujet dans les sources disponibles." Cela empêche le LLM d'inventer des réponses comme "Bien reçu, Daniel. J'..." quand le contexte est vide.
- **Liste stop-words** : 100+ mots vides français (verbes, adverbes, mots génériques) filtrés dans l'extraction de mots-clés pour éviter les requêtes SQL trop larges.

---

## [1.18.6] — 2026-05-29 (hotfix Cerveau2 ingestion)

### Fixé
- **Cerveau2 — mapping priorité** : `high` → `urgent`, `low` → `faible`. Cerveau2 rejetait tous les emails avec priorité `high` ou `low` avec une erreur 422 (validation FastAPI). C'était la cause racine des échecs d'ingestion des demandes clients urgentes.
- **Cerveau2 — `dossier_id` vide** : remplacé par `"GENERAL"` avant envoi. Cerveau2 rejette les dossier_id vides.
- **Cerveau2 — body trop long** : tronqué à 150 000 caractères avec mention `[... tronqué]`. Évite les payloads JSON massifs qui peuvent causer des timeouts ou rejets.
- **Cerveau2 — log du body d'erreur HTTP** : en cas d'échec HTTP (422, 500, etc.), le texte de la réponse Cerveau2 est logué (tronqué à 500 caractères) pour un diagnostic immédiat.
- **Cerveau2 — skip newsletter/phishing** : les pièces jointes des newsletters et phishing ne sont plus envoyées à Cerveau2 (bruit inutile). Le bloc `feed_correspondance` l'était déjà, mais pas les PJ.
- **Cerveau2 — timeout ingestion 15s → 120s** : Cerveau2 met 40-120s par email (indexation embeddings + fallback LLM). Le timeout de 15s provoquait des échecs systématiques. Passage à 120s avec retry 3x.

---

## [1.18.5] — 2026-05-28 (hotfix Drafts monitoring + telemetry)

### Fixé
- **Table `agent_telemetry` manquante dans migrations** : ajoutée dans `db_migrate.py`. Sur le VPS elle existait déjà (créée manuellement), mais les nouveaux setups plantaient.
- **Log visible des brouillons IMAP** : `_log_telemetry("draft_deposited")` ou `"draft_failed")` inséré à chaque brouillon. Visible dans le cockpit web → Audit Logs → Télémétrie poller.
- **Vérification post-dépôt Drafts** : `_verify_draft_present()` fait un SELECT + SEARCH SUBJECT "DEMANDE" dans Drafts après l'APPEND pour confirmer que le brouillon est indexé. Si non retrouvé, log `imap_draft.unverified` (warning) mais considéré comme succès (APPEND a réussi).
- **Re-sélection INBOX après Drafts** : quand `append_draft()` emprunte la connexion du poller, il re-sélectionne INBOX après la vérification pour ne pas casser le prochain `fetch`.

---

## [1.18.4] — 2026-05-28 (hotfix critique poller)

### Fixé
- **Poller traite les mails récents en priorité** : avec 7701 vieux mails sans flag, `uids[:MAX_PER_CYCLE]` traitait les 10 plus anciens à chaque cycle. Les nouveaux mails n'auraient jamais été vus avant ~64h. Correction : `uids[::-1][:MAX_PER_CYCLE]` pour traiter les UIDs les plus élevés (donc les plus récents) en premier.

---

## [1.18.3] — 2026-05-28 (hotfix critique Drafts IMAP)

### Fixé
- **IMAP Drafts — réutilisation connexion poller** : `append_draft()` accepte désormais un paramètre `imap_client` optionnel. Le poller passe sa connexion IMAP existante, éliminant la connexion secondaire qui était rejetée par Infomaniak (cause racine des 55 échecs `list_failed response=BAD` aujourd'hui).
- **Alerte monitoring Draft IMAP** : nouvelle alerte Resend `alert_imap_draft_failure()` envoyée à CDAL à chaque échec de dépôt Draft. Permet de monitorer que les brouillons de Daniel sont bien déposés.

---

## [1.18.2] — 2026-05-28

### Fixé
- **Date/heure du message original** ajoutée dans les brouillons de réponse (Resend email + IMAP Drafts). Auparavant, seuls l'expéditeur et le sujet étaient affichés — la date manquait absolument.

---

## [1.18.1] — 2026-05-28 (hotfix critique)

### Fixé
- **Poller saturation CPU** : le poller trouvait 7781 mails historiques sans flag `AgentProcessed`, saturant le VPS à 4604% CPU. Ajout d'un filtre date logiciel qui pose le flag et skip immédiatement tout mail avant le 20 mai 2026.
- **`MAX_PER_CYCLE` réduit à 10** (au lieu de 200) pour éviter de bloquer l'event loop asyncio.
- **Sleep 0.5s entre chaque mail** traité par le poller pour préserver la réactivité du cockpit web.
- **Script `deploy-to-vps.sh`** : ajout d'une vérification d'architecture (local arm64 vs VPS amd64) pour éviter le déploiement d'une image incompatible.

---

## [1.18.0] — 2026-05-28

### Changé
- **Suppression complète de `torch` / `sentence-transformers`** : passage de l'embedder local E5-large (CPU, 2GB+ RAM, JIT Triton) à un embedder API via `litellm` + `openai/text-embedding-3-small` (OpenRouter). L'image Docker passe de ~4GB à ~800MB.
- **Suppression du préchargement embedder au boot** : plus de task `embedder-preload` qui bloquait le démarrage web. L'embedder est maintenant stateless (appel API).
- **Dockerfile.base allégé** : suppression de `gcc`, `g++`, `libffi-dev`, `libssl-dev` et du `python -m compileall`. Seul `tesseract-ocr` reste (OCR pièces jointes).

### Ajouté
- **Dépendance `pytesseract>=0.3.10`** ajoutée explicitement dans `pyproject.toml` (déjà utilisé par `document_extract.py`, manquait dans les déps).
- **Variables d'environnement `EMBEDDING_API_BASE` et `EMBEDDING_API_KEY`** dans `app/config.py` et `.env.example`.

### Fixé
- **`writable_schema=ON` protégé par `try/finally`** dans `scripts/bootstrap_embeddings.py` pour éviter la corruption SQLite en cas d'exception.
- **`.env.example`** : `EMBEDDING_MODEL` aligné sur `openai/text-embedding-3-small` (au lieu de l'ancien E5 local).

---

## [1.17.4] — 2026-05-28

### Fixé
- **Ordre email Resend** : la proposition de réponse (brouillon Charlie) apparaît désormais **en haut** de l'email, et le message original du client **en dessous** — comme dans les brouillons IMAP. Daniel lit d'abord ce qu'il doit approuver, puis le contexte complet.
- **Sujet Resend** : correction faute de frappe `REPOSNE` → `REPONSE`.
- **Bandeau Resend** : correction faute de frappe `Assiatnt` → `Assistant`.

---

## [1.17.3] — 2026-05-28

### Fixé
- **Embedder préchargé au boot** : `SentenceTransformer` est chargé dans un thread séparé (`asyncio.to_thread`) au démarrage de l'agent, avant le poller. Évite le blocage de l'event loop asyncio pendant plusieurs minutes quand le premier `demande_client` déclenche le chargement du modèle.
- **Poller batch limité + yield** : maximum 200 emails traités par cycle de polling ; `await asyncio.sleep(0)` ajouté dans la boucle pour céder le contrôle à uvicorn/web server. Évite que le traitement d'un gros backlog (ex: suppression du SINCE) ne rende le cockpit web inaccessible.

---

## [1.17.2] — 2026-05-28

### Fixé
- **Poller IMAP — critère SINCE retiré** : le serveur IMAP Infomaniak rejette silencieusement le format de date RFC 3501 (`SINCE 01-May-2026`), provoquant un retour de 0 résultats et un arrêt total de détection des emails. Le critère `SINCE` est supprimé de la commande SEARCH ; l'idempotence est assurée par `UNKEYWORD AgentProcessed` + le check `_mail_exists` en base.
- **Sujet brouillon IMAP** (correction V1.17.1) : alignement définitif sur `DEMANDE D'Approbation - Reponse Demande Client : {subject}`.
- **Corps du brouillon IMAP** (correction V1.17.1) : la proposition de réponse apparaît en premier, suivie du message original du client en dessous (demande Daniel).

---

## [1.17.0] — 2026-05-27

### Ajouté
- **V2a — Livraison brouillons IMAP Drafts** : Charlie dépose les brouillons directement dans le dossier Drafts de la boîte source, avec le flag `\Draft`.
- **Script `scripts/manual_draft_deposit.py`** : dépose manuel un brouillon existant (`ai_draft` en base) dans les Drafts IMAP, sujet `PROPOSITION DE REPONSE EMAIL N° {id} / {subject}`.
- **Découverte auto du dossier Drafts** : `LIST` IMAP pour trouver `Drafts`, `Brouillons`, ou tout dossier contenant "draft" (compatibilité locale Infomaniak).

### Changé
- **UI cockpit conversation** : le bloc "Réponse proposée par Charlie" remonte en colonne droite, sous les boutons d'action `Approuver` / `Rejeter`. Numéro de mail `Email #{{ id }}` affiché en évidence verte dans le titre du bloc.
- **Corps du brouillon IMAP** : uniquement la réponse proposée + bandeau cockpit — suppression du message original du client intégré dans le corps.

### Fixé
- **Sujet brouillon IMAP** : `PROPOSITION REPONSE : ...` → `DEMANDE D'Approbation - Reponse Demande Client : ...` (spec V2a).
- **Fallback Resend conditionnel** : `notify_draft()` n'est appelé que si `append_draft()` échoue. Plus de doublon systématique email + Drafts.

---

## [1.16.13] — 2026-05-26

### Fixé
- **Charlie SQL statut** : `_build_status_sql()` génère automatiquement `SELECT ... WHERE status = 'pending'` pour les questions de demandes clients en attente (y compris avec fautes de frappe comme "deamdnes").
- **Prompt statut visible** : `_sanitize_rows_for_prompt()` expose désormais `status` et `priority` au LLM — plus de "aucun indicateur de statut" malgré des résultats SQL.
- **Garde-fous secours** : quand le LLM final dit "pas trouvé" malgré des rows en base, Charlie reconstruit la réponse directement à partir des résultats SQL.

### Changé
- **Prompt system Charlie** : règle 7b/7c ajoutée pour forcer l'inclusion de `status`/`priority` dans les SELECT de liste.

---

## [1.16.12] — 2026-05-26

### Fixé
- **Questions identitaires Cerveau2** : fallback direct `GET /notes/{path}` pour les fiches `04_entities/personnes/*.md` qui ne sont pas indexées dans sqlite-vec (Christophe, Sarah, Daniel).
- **Nuage de liaison familial** : `_resolve_links()` scanne désormais les clés relationnelles (`épouse`, `mari`, `conjoint`, `fille`, `fils`, `parent`, etc.) dans le frontmatter YAML.
- **dossier_id sur identités** : plus de filtrage par `dossier_id="CDAL"` sur les requêtes identitaires — les fiches entité ne portent pas de `dossier_id`.

---

## [1.16.11] — 2026-05-25

### Fixé
- **UX boutons feedback Charlie** : `hx-disabled-elt="find button[type=submit]"` empêche le double-clic ; le formulaire se remplace par le message de confirmation via `hx-target="this" hx-swap="outerHTML"`.
- **Bouton "À corriger"** : id stable `{feedback_id}-toggle` pour éviter les conflits HTMX.

---

## [1.16.10] — 2026-05-25

### Fixé
- **Classification IMAP** : mailbox_name correctement extrait dans `count_sql` pour les statistiques par marque.

---

## [1.16.0] — 2026-05-24

### Ajouté
- **Cockpit web v1.0** : inbox, conversation, chat AI Charlie, dashboard admin.
- **Bot Slack Charlie AI** : @mention + DM sur #detective.

### Changé
- **Génération SQL programmatique** pour les comptages d'emails (bypass LLM, +90% fiabilité).

---

*Format basé sur [Keep a Changelog](https://keepachangelog.com).*
