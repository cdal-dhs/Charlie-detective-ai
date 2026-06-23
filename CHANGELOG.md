# Changelog Charlie AI — Detective.be

## [1.25.7] — 2026-06-23 (brouillon ack pour les réponses client à Daniel — #606 Van Houtte)

### Contexte
#606 (Frédéric Van Houtte) = `Re: Mission ouvrier en maladie` : réponse du client à un échange récent avec Daniel (« Je vous ai répondu en vert sur votre mail » + citation du mail de Daniel du 16 juin sur la mission ouvrier en incapacité). C'est un **follow-up / accusé de réception**, pas un nouveau prospect. Le brouillon généré était le **qualifiant standard** (« Je comprends que vous souhaitez vérifier une incapacité de travail… pourriez-vous me transmettre : 1. Vos nom et prénom complets. 2. Votre adresse… ») — inadapté : Daniel a déjà envoyé sa mission/devis, le client répond, il ne faut pas redemander nom/prénom/GSM comme un nouveau prospect.

Le brouillon ack (`build_followup_ack_draft`, v1.25.1) existe déjà (« Merci pour ces compléments d'informations. Je les prends bien en compte et je vous reviens dès que possible sur la suite de votre dossier ») mais n'était pas déclenché : `_is_client_followup` (poller) et `_is_web_followup` (cockpit) requéraient que le sender ait **déjà un mail `demande_client` en DB dans les 30 derniers jours**. Or Frédéric n'avait qu'**un seul mail** en DB (#606 lui-même) — le mail initial qui a déclenché la mission a été traité hors-agent / autre boîte. L'historique DB manquait → follow-up non détecté → brouillon qualifiant.

`_is_reply_to_daniel` (v1.24.0, créé explicitement pour #606) détectait bien « Re: + body cite un mail de Daniel (préfixe `>` + signature cabinet) » mais ne faisait que **forcer la catégorie `demande_client`** (pour ne pas classer en facture/phishing) — il ne déclenchait pas le brouillon ack.

### Ajouté
- **Shortcut citation Daniel** dans `_is_client_followup` (poller) ET `_is_web_followup` (cockpit) : si `_is_reply_to_daniel(body, sender)` est vrai (Re: + citation d'un mail de Daniel avec signature cabinet), le mail est traité comme follow-up **sans nécessiter d'historique DB**. La citation d'un mail de Daniel est la preuve d'un échange existant — indépendante de l'historique DB (qui peut manquer si le mail initial a été traité hors-agent / autre boîte). → brouillon ack au lieu du qualifiant.
- **Cohérence poller ↔ cockpit** : le shortcut est dans les deux fonctions, donc la régénération cockpit (`POST /api/drafts/{id}/retry`) produit aussi le brouillon ack pour #606.

### Sécurité
Le brouillon ack est **safe** même si le client formule une nouvelle demande dans sa réponse : Daniel valide tous les brouillons en Drafts IMAP (V2a) avant envoi — il lit le mail original et adapte si besoin. Aucune demande n'est perdue. La règle d'or « faux positifs acceptables, faux négatifs intolérables » est respectée (un ack sur une réponse-avec-nouvelle-demande est un faux positif bénin que Daniel corrige ; un qualifiant sur un ack pur était un faux négatif qui polue l'inbox de Daniel avec des questions déjà répondues).

### Tests
- `tests/test_imap_poller_resilience.py` : +2 tests (`_is_client_followup` True sur citation Daniel sans historique DB #606 ; False sur citation sans signature cabinet — pas de shortcut).
- `tests/test_web_followup_shortcut.py` (3 tests) : `_is_web_followup` True sur citation Daniel sans historique DB, False sur citation sans signature, False sur nouvelle demande sans marqueurs.
- 110 tests au total, 0 régression (tests follow-up existants inchangés : leurs bodies ne citent pas Daniel).

## [1.25.6] — 2026-06-23 (demande de l'objectif final — #615 douane Kaiserslautern)

### Contexte
#615 (Andree Marie Scurbecq) = « faire une petite enquete au bureau de douane de Kaiserslautern » : demande d'enquête **sans objectif final précis** (prouver quoi ? vérifier quoi ?). Le brouillon qualifiant standard sautait directement aux tarifs sans demander l'objectif, or sans objectif on ne peut pas établir un devis. Le brouillon « demande floue » (v1.25.1, qui demande l'objectif + restitue les infos + tarifs + rappel tel) existait déjà mais n'était pas déclenché : la détection `_is_vague_request` pour `non_determine` se basait sur `len(body) < 200`, or le body de #615 = 542 chars (gonflé par les champs formulaire `Nom:/Prénom:/Téléphone:/Mentions légales:`). Le vrai message du client (~100 chars) était noyé.

### Ajouté
- **Module `app/pipeline/objective_check.py`** : verdict amont « objectif clair vs flou » basé sur le **message LIBRE du client** (body avant les champs formulaire, via `extract_free_message`), par un HYBRIDE :
  1. **Heuristique déterministe** (rapide, zéro LLM) : question de tarif explicite → clair ; objectif final évident (filature, infidélité, surveillance, recherche, dette, micros, incapacité, harcèlement, constat, fraude…) → clair ; message vide/lapidaire (< 60 chars) → flou ; sinon → incertain.
  2. **LLM gemma4 si incertain** : « le client a-t-il exprimé un objectif final précis et actionnable ? » → `OBJECTIF_CLAIR`/`OBJECTIF_FLOU`. Multilingue (NL/EN/DE/ES…).
  3. **Dégradation** : si le LLM échoue ou répond de façon inattendue → flou (règle d'or du projet : faux positifs acceptables — demander l'objectif inutilement —, faux négatifs intolérables — rater une demande floue et livrer un devis sans objectif).
- **Branchement generator** (`generate_draft`) : pour `non_determine` uniquement, appel à `assess_objective_clarity` avant `build_qualification_draft` ; le verdict est passé au builder via `objective_clear`. Les cas classés (infidélité, recherche, dette…) gardent leur logique existante (blast radius limité à `non_determine`).

### Changé
- `build_qualification_draft` + `_is_vague_request` (qualification_builder) : nouveau paramètre `objective_clear: bool | None = None`. Pour `non_determine`, le verdict amont override l'ancien critère `len(body) < 200`. `None` = legacy préservé (tests existants inchangés). Le brouillon flou existant (`_build_vague_request_draft`) est réutilisé tel quel : il demande déjà « pourriez-vous me préciser ce que vous souhaitez obtenir concrètement de notre intervention ? » + tarifs + rappel au téléphone fourni.
- L'heuristique exclut volontairement le terme générique « enquête » (trop large : « faire une petite enquête » ≠ objectif précis). Seuls les objectifs FINAUX déclenchent le shortcut déterministe.

### Tests
- `tests/test_objective_check.py` (14 tests) : `extract_free_message` (isole le message libre des champs formulaire, #615), heuristique (objectifs clairs filature/tarif/recherche, #615 vague → None, empty/lapidaire → flou), `assess_objective_clarity` (clair skip LLM, #615 LLM FLOU, LLM CLAIR, échec LLM → flou, empty → flou sans LLM).
- 168 tests au total, 0 régression (builder legacy avec `objective_clear=None` préservé).

## [1.25.5] — 2026-06-23 (détection newsletter durcie — #619 Arval via Eloqua)

### Contexte
#619 = newsletter marketing B2B Arval (`arval@info.arval.com`, sujet « Arval | Quelques conseils pour préparer vos vacances d'été », body contenant une URL Eloqua `elqTrackId`/`elqaid` + « Découvrez nos conseils » + « Monsieur Daniel Hurchon ») classée `demande_client` (priority high, status pending) à tort par le LLM. Le pré-filtre `is_newsletter` a raté pour 3 raisons : (1) matching accent-sensible (« découvrez » ≠ « decouvrez »), (2) sender `info.arval.com` absent de `NEWSLETTER_SENDERS`, (3) pas de détection des signatures URL Eloqua/Mailchimp. Le LLM a vu « Daniel Hurchon » + un « conseil » et a cru à une demande.

### Ajouté
- **Matching accent-insensible** (`_unaccent` via `unicodedata.NFKD` + encode ASCII ignore) sur le sujet ET le body ET les keywords : « découvrez » (body) matche désormais le keyword « decouvrez ».
- **Détection signatures URL plateformes marketing** (`NEWSLETTER_MARKETING_URLS` = `elqtrackid`/`elqaid`/`elq=` Eloqua, `mc_cid`/`mc_eid` Mailchimp, `xtrk=`/`trk_`) dans le body — robuste indépendamment du sender (rattrape les marketeurs qui envoient depuis un domaine d'envoi neutre).
- **Détection sous-domaines marketing** (`NEWSLETTER_DOMAINS` = `info.`/`news.`/`newsletter.`/`email.`/`marketing.`/`communications.`/`mailing.`/`campaign.`/`edm.`) via extraction du domaine de l'expéditeur (`email.utils.parseaddr`) : les vraies demandes clients ne viennent jamais de `info.arval.com`/`news.entreprise.com`. Attention : `info@brand.com` ne matche PAS (« info » est dans la partie locale, pas le domaine).

### Changé
- `is_newsletter` (app/pipeline/prefilter.py) réécrite avec `_unaccent` sur subject/body/keywords + check URLs marketing + check sous-domaines marketing. Ordre `quick_classify` inchangé : newsletter AVANT service_email (les newsletters peuvent matcher des mots-clés service).

### Tests
- `tests/test_prefilter_newsletter.py` (13 tests) : #619 Arval détecté + quick_classify newsletter, accent-insensibilité (subject + body), URL Eloqua/Mailchimp (sender neutre), sous-domaines marketing (news./email./marketing.), non-régressions (vraie demande client, `info@detectivebelgium.com` partie locale ≠ sous-domaine, `contact@brand.com`, sujet sans marqueurs).

## [1.25.4] — 2026-06-23 (sujets non-représentatifs #515 + tag NO_EMAIL_IN_THE_FORM pour forwarders WP)

### Contexte
#515 = formulaire WordPress avec sujet `[Privédetective België] Réinitialisation du mot de passe` (forwarder `wordpress@detectivebelgium.com`) : sujet lisible mais **totalement incohérent** avec la vraie demande (Hairemans Nathalie, suivi d'infidélité, dans le body). v1.25.3 ne gérait que les homoglyphes (détection déterministe). Deux manques : (1) les sujets non-représentatifs mais lisibles n'étaient pas reformulés, (2) les forwarders WP n'ont pas d'email client (vrai contact = téléphone, cf. Task #4) → Daniel/le brouillon doivent le savoir.

### Ajouté
- **Reformulation LLM des sujets non-représentatifs** (`app/pipeline/subject_fixer.py::fix_subject_llm`) : le prompt system couvre désormais (1) les homoglyphes illisibles ET (2) les sujets automatiques non-représentatifs (forwarders WP « Réinitialisation du mot de passe », « Contact form ») — le LLM reformule à partir du body pour refléter la demande réelle. Si le sujet est déjà représentatif, renvoie tel quel (no-op).
- **Tag `[NO_EMAIL_IN_THE_FORM]`** pour les forwarders WordPress (`is_wp_forwarder` = sender matchant `^(mail|wordpress|contact)@.*detective`, `tag_no_email` suffixe le sujet, idempotent) : ces formulaires n'ont pas d'email client → le vrai contact est le téléphone (champ Telefoonnummer). Signal visuel immédiat pour Daniel/CDAL et le brouillon. Déterministe, zéro LLM, zéro faux positif (l'agent Resend `agent@digitalhs.biz` ne matche pas).
- **Hook pipeline** (`app/workers/imap_poller.py`) : après la correction homoglyphe, application du tag `NO_EMAIL_IN_THE_FORM` si sender = forwarder WP. Les futurs formulaires WP seront tagués dès l'inbox.
- **Endpoint cockpit `POST /api/mails/{id}/fix-subject`** étendu : fetch le sender, reformulation LLM + tag WP (rétrocorrection #515 = reformulation + tag, même si le LLM ne propose rien le tag est appliqué sur l'original).

### Changé
- `fix_subject_llm` prompt élargi (homoglyphes + sujets non-représentatifs). L'auto-pipeline reste sur `is_subject_suspect` (homoglyphes only, détection déterministe fiable) — la reformulation non-représentative est déclenchée manuellement via le bouton cockpit (trop risquée en auto : faux positifs sur tous les sujets « Contact »).

### Tests
- `tests/test_subject_fixer.py` : +8 tests (`is_wp_forwarder` match/reject/case, `tag_no_email` add/idempotent/normal-sender/empty) → 20 tests au total.
- `tests/test_web_fix_subject.py` : réécrit avec sender dans le schéma + 5 tests (#614 homoglyphes sans tag, #515 reformulation+tag, #515 tag-only si LLM noop, noop sender normal + LLM None, 404).

## [1.25.3] — 2026-06-23 (correction LLM des sujets illisibles — homoglyphes itsme #614)

### Contexte
Le mail #614 (Serge M) a un sujet `іtѕⅿе-Bеvеіlіngѕmеldіng` — homoglyphes cyrilliques + chiffre romain `ⅿ` ressemblant à `itsme-Bevelingsmelding`. Ce sujet illisible pollue l'inbox et le sujet du brouillon V2a (`DEMANDE D'Approbation - ... : {sujet}`). Daniel a demandé que Charlie corrige ces sujets (le VPS est x86_64 ≠ Mac ARM, doc mis à jour).

### Ajouté
- **Module `app/pipeline/subject_fixer.py`** : détection déterministe des sujets suspects (`is_subject_suspect` = présence de confusables cyrillique U+0400–U+04FF / grec U+0370–U+03FF / chiffres romains U+2160–U+2188 ; les accents Latin `é è à ç` ne sont PAS des confusables) + correction LLM (`fix_subject_llm` : prompt court, gemma4:31b, max 120 tokens, nettoyage guillemets/préfixes « Sujet : »/première ligne, rejet si >200 chars). Dégradation silencieuse : si le LLM échoue ou ne propose rien de mieux, on conserve l'original (jamais de crash).
- **Hook pipeline** (`app/workers/imap_poller.py`) : après les skips (date avant 2026-06-01, system email) et AVANT `classify`, si le sujet est suspect → correction LLM → le sujet corrigé bénéficie à `classify`, `assign_priority`, `generate_draft` (sujet lisible du brouillon V2a) et la persistance. Coût LLM nul (forfait Ollama Pro). Les mails suspects (homoglyphes) sont rares → impact sur la cadence 5 min négligeable.
- **Endpoint cockpit `POST /api/mails/{mail_id}/fix-subject`** (`app/web/api.py`) : rétrocorrection des anciens mails (#614). UPDATE `subject` + audit log de l'original (forensic). Dégradation silencieuse (message d'info si le LLM ne propose rien). Bouton `✨ Corriger le sujet` ajouté dans `conversation.html` (HTMX, cible `#mail-subject`).

### Documentation
- **`CLAUDE.md` section Déploiement** réécrite : note explicite **VPS x86_64 ≠ Mac ARM** (cross-build `buildx --platform linux/amd64` obligatoire, `docker build` simple produit une image ARM inutilisable sur le VPS), distinction déploiement léger (code Python via volumes `app/`+`scripts/` ro + `docker compose restart`) vs rebuild image (seulement si `pyproject.toml`/`Dockerfile*`/packages système changent), correction du tag image (`detective-detective:latest` tiret, pas underscore), et `docker compose up -d` seul ne recharge pas le code Python → `restart` obligatoire.

### Tests
- `tests/test_subject_fixer.py` (12 tests) : détection suspect (cyrillique/grec/chiffre romain/accents FR exclus/empty) + `_clean` (guillemets, préfixe, première ligne) + `fix_subject_llm` mocké (succès, guillemets, no-improvement→None, échec LLM→None, empty→None, trop long→None, accents préservés).
- `tests/test_web_fix_subject.py` (3 tests) : endpoint UPDATE + audit log original + HTML nouveau sujet ; no-improvement conserve l'original + audit `subject_fix_noop` ; 404 si mail absent.

## [1.25.2] — 2026-06-23 (reclassify avant (re)génération + backfill --only-id robuste — #614)

### Contexte
Livraison de #614 (Serge M / demande d'extraction des conversations WhatsApp = piratage). Diagnostic : #614 était resté classé `phishing` en base. Le retry cockpit (`POST /api/drafts/{id}/retry`) régénérait le brouillon SANS reclassifier → `generate_draft` prenait la branche `else` (LLM) et produisait un brouillon hybride incomplet (annonçait des questions et des tarifs sans les lister) au lieu du brouillon déterministe `illegal_refusal` (qui contient cadrage légal + alternative + questions + tarifs chiffrés).

### Corrigé
- **Bug P0 — retry/generate sans reclassification** (`app/web/api.py::draft_generate`) : on reclassifie maintenant via `classify()` AVANT d'appeler `generate_draft`. Si la catégorie change, on update la DB (`category`, `status='pending'`, `priority='high'`) et on log `draft_generate.reclassified`. Garde ajoutée : si la catégorie finale n'est pas dans `draft_categories` (`demande_client`, `prise_contact`), on NE génère AUCUN brouillon (les phishing/spam/facture ne reçoivent pas de réponse) — on retourne un message clair au cockpit au lieu d'un brouillon LLM inadapté. Cf. #614.
- **Bug — backfill `--only-id` bloqué par `draft_generated`** (`scripts/backfill_reclassify.py::_fetch_candidates`) : en `--only-id`, on retirait déjà le filtre catégorie (v1.24.1) mais PAS le filtre `draft_generated=0 AND ai_draft IS NULL`. Conséquence : impossible de retraiter un mail déjà brouillonné (ex: remplacer un brouillon LLM inadapté). Désormais `--only-id` ne filtre PLUS que par `id` — CDAL sait quel mail il cible.

### Procédure #614 appliquée en prod
1. `backfill_reclassify --apply --only-id 614` (après reset du brouillon LLM) → reclassé `phishing→demande_client` + brouillon `illegal_refusal` déterministe généré (cadrage légal complet + alternative filature + 9 questions opérationnelles + tarifs 200/150/75/95 € + 2 détectives + signature Daniel).
2. `deliver_pending_drafts --apply --only-id 614` → déposé dans les Drafts IMAP de `detective_belgique`, `verified=True`.

### Tests
- `tests/test_web_draft_retry.py` (2) : retry reclassifie avant de générer (mail phishing → demande_client + brouillon déterministe) ; garde anti-brouillon sur catégorie non-demande (generate_draft jamais appelé, ancien brouillon conservé).
- `tests/test_backfill_reclassify.py` (5) : `--only-id` retourne un mail déjà brouillonné ; bulk garde le filtre `draft_generated=0` ; `_regenerate_draft` remplace le brouillon si reclassé demande_client, n'en génère pas sinon.
- **157 tests verts** (150 + 7).

### Point de vigilance
Le backfill et le retry dépendent de `classify()` (LLM gemma4:31b) — un faux négatif (classify laisse en phishing un vrai demande_client) reste possible, mais le hardening v1.24.0 `_has_strong_human_demand` (tarif + vocab enquête + signature) lève la majorité des cas. Règle d'or conservée : faux positifs acceptables, faux négatifs intolérables.

---

## [1.25.1] — 2026-06-23 (sujet de brouillon lisible + brouillon pour demande floue — #515)

### Contexte
Deux irritants remontés sur les brouillons `demande_client` livrés dans les Drafts IMAP (V2a) :

1. **Sujet de brouillon illisible** (#515) : les formulaires WordPress relaient le mail du client avec un sujet template sans rapport avec la vraie demande (« Réinitialisation du mot de passe », « Nouveau Message De Détective privé Belgique - Prenons contact », « Contactformulier », « Uw bericht »…), expédié par un forwarder (`wordpress@`/`contactform@`/`no-reply@`/`mail@`). Le sujet du brouillon IMAP (`DEMANDE D'Approbation - Reponse Demande Client : {sujet}`) devenait alors absurde et illisible pour Daniel dans sa boîte.
2. **Demande floue** (#515 Nathalie / #615 douane) : un client raconte sa situation sans formuler de demande opérationnelle claire (pas de cible/horaires/lieu) ni poser de question de tarif. Le brouillon qualifiant standard alignait alors une batterie de questions opérationnelles (cible, adresse de départ, horaires, véhicule…) décalées et inadaptées tant que la demande n'est pas clarifiée.

### Ajouté
- **`suggested_subject_for_draft()`** (`app/pipeline/qualification_builder.py`) : détecte un sujet « absurde » (sujet matchant un template WP **ou** expéditeur = forwarder) et retourne un libellé lisible `"{cas_label} — {Prénom NOM}"` (ou juste le libellé si aucun nom n'est extrait du body). Retourne `None` si le sujet original est pertinent (on le garde). Le résultat est propagé via `GenerationResult.suggested_subject` (`app/pipeline/generator.py`) jusqu'à la livraison IMAP (`app/delivery/imap_draft.py`) qui l'utilise à la place du sujet original quand il est défini.
- **Détection des demandes floues** (`_is_vague_request()`) : la dette a sa propre logique (jamais floue) ; une question de tarif explicite désactive la détection (le client sait ce qu'il veut → brouillon standard) ; `non_determine` lapidaire (< 200 chars, sans tarif) = flou ; cas classé = flou si **aucune** info opérationnelle extraite (questions d'index ≥ 3 dans `_CASE_QUESTION_SPECS`).
- **Brouillon de clarification** (`_build_vague_request_draft()`) : accuse réception, restitue les infos déjà reçues (nom, prénom, GSM… via `_format_received_info`), demande poliment ce que le client souhaite obtenir concrètement, donne les tarifs (transparence), propose un échange téléphonique au numéro fourni le cas échéant (Task #4 partielle : vrai contact = téléphone pour les formulaires WP), et signe au nom de Daniel. **Pas de questions opérationnelles** numérotées.

### Changé
- `app/pipeline/qualification_builder.py` : `build_qualification_draft()` gagne une branche « demande floue » (après le refus hors-légalité v1.24.1, avant la dette/standard) qui court-circuite vers le brouillon de clarification.
- `app/pipeline/generator.py` : `GenerationResult` gagne le champ `suggested_subject` ; la branche `demande_client` calcule et logge le `suggested_subject`.
- `app/delivery/imap_draft.py` : le `Subject` du brouillon IMAP utilise `gen.suggested_subject or incoming.subject`.
- `_TARIFF_QUESTION_PATTERNS` : pattern élargi pour matcher « quel est votre tarif » / « quel est le tarif » (trop restrictif auparavant).

### Tests
- `tests/test_qualification_builder.py` : 13 nouveaux tests (suggested_subject ×4, _is_vague_request ×6, _build_vague_request_draft ×2, build_qualification_draft flou ×1). Les 3 tests existants du brouillon standard ont été enrichis d'une question de tarif au body (sans quoi le body lapidaire est désormais correctement détecté comme flou — le changement de comportement est voulu). **150 tests verts** (137 + 13).

### Point de vigilance
La détection floue repose sur l'extraction d'infos opérationnelles (`_extract_case_info` + `_extract_client_info`). Un cas classé avec une info op extraite à tort (faux positif d'extraction) ne sera **pas** détecté comme flou et recevra le brouillon standard. Règle d'or conservée : faux positifs acceptables, faux négatifs intolérables.

---

## [1.25.0] — 2026-06-23 (bascule des modèles LLM — gemma4:31b principal + glm-5.2:cloud fallback)

### Contexte
La documentation (CLAUDE.md, README, HANDOVER) était **massivement désynchronisée** avec la prod depuis la v1.21.1 : elle affirmait que `kimi-k2.6:cloud` était le modèle principal, que `gemma4:31b` était « obsolète », et que `claude-sonnet-4` était « 404 sur OpenRouter ». En réalité, la prod tournait déjà sur `gemma4:31b` (default/classifier/qualifier) avec `claude-sonnet-4` en fallback via OpenRouter. CDAL a confirmé la cible :

- **`gemma4:31b`** (Ollama Pro Cloud, `openai/gemma4:31b` + `api_base=https://ollama.com/v1`) devient le modèle **principal sur toutes les tâches** : génération de brouillons (default), classifier, case classifier (qualifier), **et le chat Charlie** (cockpit + Slack Bot — bascule depuis kimi-k2.6:cloud). Modèle **non-reasoning** (réponse dans `message.content`), multimodal, ~256K context. Existe sur https://ollama.com/library/gemma4 — ce n'est PAS un modèle obsolète.
- **`glm-5.2:cloud`** (Ollama Pro Cloud, `openai/glm-5.2:cloud`) devient le **fallback** unique (remplace `claude-sonnet-4` via OpenRouter et `glm-5.1:cloud`). Reasoning model (Z.ai, ~756B params, ~976K context, thinking effort High/Max) — réponse dans `reasoning_content`, le wrapper `complete()` du routeur l'extrait automatiquement quand `content` est vide. `_clean_reasoning()` (30+ patterns) reste utile pour filtrer les traces de raisonnement de ce fallback.
- **`kimi-k2.6:cloud`** n'est plus utilisé nulle part.

Cette entrée annule la « découverte latérale » de v1.24.1 qui recommandait de « basculer case_classifier/translator de gemma4:31b (obsolète) vers kimi-k2.6:cloud » — cette recommandation était **fausse**, gemma4:31b est le modèle voulu.

### Changé — `app/config.py` (défauts)
- `llm_model_default` : `openai/kimi-k2.6:cloud` → `openai/gemma4:31b`
- `llm_model_classifier` : `openai/kimi-k2.6:cloud` → `openai/gemma4:31b`
- `llm_model_chat` : `openai/kimi-k2.6:cloud` → `openai/gemma4:31b`
- `llm_model_fallback` : `openai/glm-5.1:cloud` → `openai/glm-5.2:cloud`
- `llm_model_qualifier` : `openai/gemma4:31b` (inchangé — déjà correct)

### Changé — `.env` (local CDAL, rsyncé vers `.env.production` au deploy)
- `LLM_MODEL_FALLBACK` : `openrouter/anthropic/claude-sonnet-4` → `openai/glm-5.2:cloud`
- Ajout `LLM_MODEL_CHAT=openai/gemma4:31b` (bascule chat kimi→gemma4 explicite)
- Commentaire « Fallback : OpenRouter » → « Fallback : glm-5.2:cloud via Ollama Pro ». La clé `OPENROUTER_API_KEY` est conservée (utilisée uniquement pour les embeddings `text-embedding-3-small`).

### Changé — `.env.example`
- Section LLM alignée : `LLM_MODEL_DEFAULT`/`CLASSIFIER`/`CHAT`/`QUALIFIER` = `openai/gemma4:31b`, `LLM_MODEL_FALLBACK` = `openai/glm-5.2:cloud`. Commentaires reformulés (provider Ollama Cloud, JAMAIS `ollama_chat/`). Retrait des mentions « Kimi K2.6 », « GLM 5.1 », « Claude Sonnet 4 404 ».

### Inchangé — `app/llm/router.py`
- Le wrapper `complete()` prend déjà `content` d'abord, puis `reasoning_content` si vide : compatible gemma4:31b non-reasoning (content direct) ET glm-5.2:cloud reasoning (reasoning_content). `_clean_reasoning()` est inoffensif sur gemma4:31b (aucun pattern de trace à filtrer) et utile pour le fallback glm-5.2:cloud. Aucun changement de code nécessaire.

### Documentation (23 edits via sub-agent)
- **CLAUDE.md** (§3 stack, §6 garde-fous, §7 état courant) : principal = `gemma4:31b`, chat = `gemma4:31b` (bascule v1.25.0), fallback = `glm-5.2:cloud`. Retrait de « Claude Sonnet 4 est 404 sur OpenRouter ». Reformulation des garde-fous LLM (glm-5.2:cloud reasoning fallback + gemma4:31b non-reasoning principal, `ollama_chat/` toujours interdit). **Suppression du « Point de vigilance #10 »** (gemma4:31b n'est pas un bug à corriger).
- **README.md** (architecture, stack, statut, version) : idem. Version → 1.25.0.
- **HANDOVER.md** (header, §2 fichiers clés, §3, §4 stack, §8 règle 8, §9 points vigilance #2/#3/#4, suppression #10) : idem. Mentions historiques kimi dans les post-mortems conservées (faits passés datés).
- `docs/ROADMAP.md` : aucune correction nécessaire (pas de mention modèle courant obsolète).

### Tests
- **137/137 tests verts** (aucune régression — les tests mockent `complete()`).
- **Smoke test LLM** : `openai/gemma4:31b` répond `pong` (~1s, latence faible). `openai/glm-5.2:cloud` répond `pong` (mais nécessite `max_tokens` ≥ ~100 — voir point de vigilance ci-dessous).

### Corrigé — `max_tokens` du fallback reasoning (anti crash silencieux)
`glm-5.2:cloud` est un **reasoning model** : il produit d'abord une trace de raisonnement (`reasoning_content`) puis la réponse finale (`content`). Si l'appel est fait avec un `max_tokens` trop faible, **tous les tokens sont consommés par le raisonnement** et `content` reste vide → réponse finale vide après `_clean_reasoning()`. Le fallback du router `complete()` réutilise le même `max_tokens` que l'appel principal, donc le risque dépend des valeurs passées par chaque appelant. Audit fait :

- **`app/pipeline/classifier.py:394`** — `max_tokens=15` → **200**. Le classifier attend 1 mot (la catégorie) ; gemma4:31b (principal) répond en 1 mot et s'arrête (max_tokens = plafond, pas cible). Mais le fallback glm-5.2:cloud ne pouvait **jamais** répondre en 15 tokens → mail classé `"autre"` silencieusement = faux négatif (intolérable). 200 laisse la place au raisonnement + à la catégorie.
- **`app/pipeline/case_classifier.py:171`** — `max_tokens=300` → **500**. Le case_classifier renvoie un JSON `{case_type, confidence, reason}` ; 300 tokens laissait trop peu de place au raisonnement glm-5.2:cloud avant le JSON. 500 donne la marge. (Dégradation gracieuse déjà en place via try/except → `non_determine`.)
- Appels déjà sûrs (inchangés) : `generator.py` (2500), `translator.py` (3000), `charlie.py` (500-1000), `classifier` déterministe.

### Point de vigilance restant — chat Charlie en fallback
Le chat Charlie (`app/charlie.py:1275`, `max_tokens=500`) appelle gemma4:31b (principal, non-reasoning → pas de problème). Le fallback glm-5.2:cloud ne s'active qu'en cas de panne gemma4 : à 500 tokens, le raisonnement glm-5.2 peut laisser ~100-200 tokens pour la réponse — acceptable pour la plupart des questions, mais une question complexe pourrait voir sa réponse tronquée. Le chat a un try/except (dégradation gracieuse). Surveillance prod conseillée lors d'une panne gemma4 — hors-scope v1.25.0.

### Déploiement
- Le rsync `.env` → `.env.production` au déploiement propagera `LLM_MODEL_FALLBACK=glm-5.2:cloud` et `LLM_MODEL_CHAT=gemma4:31b` en prod. `docker compose restart` (dev mount `./app:ro`) — pas de rebuild Docker nécessaire (aucune dépendance modifiée).
- `app_settings` prod : déjà vide (pas de purge nécessaire — les défauts config.py + `.env.production` priment).

## [1.24.2] — 2026-06-23 (RAG mis en pause — l'approche déterministe le remplace)

### Contexte
Le RAG (retrieval sur ~2042 paires Q/R historiques via `sqlite-vec`) est **mis en pause** par défaut. La nouvelle approche de génération de brouillons — brouillon qualifiant **déterministe** par code (`qualification_builder.py`, v1.22.7+) avec questions structurées par cas de figure + récupération des informations déjà fournies par le client + few-shot Daniel (v1.22.4) — est **plus fiable** que le RAG et le remplace pour la génération des brouillons `demande_client` / `prise_contact`.

Ce constat est confirmé par deux faits :
1. Le RAG était de toute façon **cassé sur les 3 boîtes** depuis le 2026-05-28 (point de vigilance #1 du HANDOVER : `boite1` = table `pairs` vide, `boite2`/`boite3` = table inexistante). Le bootstrap n'a jamais été ré-exécuté après la bascule embedder local → OpenRouter (v1.18.0). Tous les brouillons générés depuis avaient RAG=0.
2. Pour les `demande_client` / `prise_contact`, le résultat du RAG (`pairs`) **n'était de toute façon pas utilisé** : la branche `build_qualification_draft` (déterministe) ignore `pairs`. Le RAG n'était exploité que dans la branche `else` (catégories hors `draft_categories`), qui ne correspond pas aux brouillons clients.

Mettre le RAG en pause supprime donc un appel embedding inutile (coût + latence) sans aucune perte de qualité sur les brouillons clients.

### Changé — `app/config.py`
- Nouveau setting `rag_enabled: bool = False` (env `RAG_ENABLED`). Désactivé par défaut. Réactivable via `RAG_ENABLED=true` — utile uniquement si on re-bootstrappe `pairs_vec` et qu'on décide de réinjecter des exemples historiques dans la branche `else` du générateur.

### Changé — `app/pipeline/rag.py`
- `retrieve()` court-circuite immédiatement (retourne `[]`) si `rag_enabled=False`, **avant** tout appel à l'API embedding. Log `rag.disabled_skip`. Le reste du code (embed, `_connect`, query sqlite-vec) est conservé intact pour réactivation ultérieure. La dégradation silencieuse d'origine (table manquante / API échoue) reste en place.

### Inchangé — `app/pipeline/generator.py`
- L'appel `retrieve()` reste en place ; il retourne désormais `[]` sans IO ni appel API. Le log `generator.retrieved rag=0` reflète l'état. Aucune branche du générateur ne régressait (la branche `demande_client`/`prise_contact` n'utilisait pas `pairs`).

### Config — `.env.example`
- Section `# --- RAG ---` mise à jour : commentaire explicatif + `RAG_ENABLED=false`.

### Documentation
- `CLAUDE.md` (§3 stack, §7 état courant) : RAG marqué « en pause (v1.24.2) — remplacé par l'approche déterministe ».
- `README.md` (architecture, stack, statut) : idem.
- `HANDOVER.md` (fichiers clés, point de vigilance #1) : point #1 reformulé — le RAG n'est plus un bug à corriger en urgence mais une fonctionnalité mise en pause par choix, réactivable.
- `docs/ROADMAP.md` : item RAG marqué en pause.

### Tests
- **137/137 tests verts** (aucune régression — les tests mockaient déjà `retrieve → []`).

### À venir
- Si on souhaite réactiver le RAG un jour : `python -m scripts.bootstrap_embeddings` (re-indexer `pairs_vec` sur les 3 boîtes) + `RAG_ENABLED=true` + vérifier que la branche `else` du générateur en tire bénéfice. Hors-scope tant que l'approche déterministe donne satisfaction.

## [1.24.1] — 2026-06-22 (brouillon hors-légalité — refus poli + alternative légale)

### Contexte
Suite au meeting Daniel du 2026-06-22, le mail #614 (Serge M) est une demande **mixte** : prouver l'infidélité de son épouse (légal — filature/surveillance) **+** « faire sortir toutes les conversations WhatsApp » du téléphone de son épuse (illégal en Belgique — accès non autorisé aux communications privées = atteinte à la vie privée + accès frauduleux à un système informatique). Le brouillon qualifiant infidélité standard est inadapté : il proposerait une filature sans adresser la demande de piratage, laissant croire qu'on pourrait le faire.

Daniel demande : « pour la demande hors-légalité, préparer une réponse polie qu'il y a des lois et que nous sommes tenus de les respecter ».

### Ajouté — `app/pipeline/qualification_builder.py`
- **`_ILLEGAL_REQUEST_PATTERNS`** — 11 regex (FR + NL + EN) détectant les demandes d'accès non autorisé : extraction de conversations/messages, piratage de téléphone/compte/WhatsApp/messagerie, accès aux communications privées d'une personne, logiciel espion / mise sur écoute / installation cachée, relevés téléphoniques/bancaires, géolocalisation sans consentement, obtention de mot de passe.
- **`_detect_illegal_request(body)`** — renvoie `(match, extrait)`. Match sur le body de #614 (« faire sortir toutes les conversations »).
- **`_LEGAL_ALTERNATIVE`** — mapping cas → alternative légale proposée (filature discrète, surveillance, enquête de passé, détection micros, etc.).
- **`_build_illegal_refusal_draft(...)`** — brouillon de refus poli : (1) accusé réception + reconnaissance de la situation, (2) refus ferme et transparent sur le cadre légal belge (infractions pénales, détectives agréés tenus de respecter la loi), (3) alternative légale selon le cas sous-jacent, (4) infos reçues + questions de collecte manquantes + tarifs (transparence), (5) signature Daniel. Ton identique au brouillon qualifiant standard.
- **`build_qualification_draft`** — court-circuite le brouillon qualifiant standard si `_detect_illegal_request` matche. Log `qualification.illegal_request_detected`.

### Changé — `scripts/backfill_reclassify.py`
- `--only-id N` ne filtre **plus** par catégorie (avant : `category IN (autre, facture, rappel, urgent)` empêchait de retraiter un phishing mal classé comme #614). Avec un ID précis, on cible le mail sans filtrage de catégorie — on garde uniquement le filtre « pas encore de brouillon généré ». Permet de remonter #614 (phishing → demande_client) après le hardening v1.24.0.

### Reclassement appliqué en prod (post-déploiement v1.24.0)
- **#515** (Nathalie Hairemans, detective_belgium) — reclassé `facture` → `demande_client`, brouillon généré (10556 chars), livré dans `Drafts` de detective_belgium.
- **#606** (Frédéric Van Houtte, detective_belgique) — reclassé `facture` → `demande_client`, brouillon généré (4653 chars, case `incapacite_travail`), livré dans `Drafts` de detective_belgique.
- **#614** (Serge M) — en attente du déploiement v1.24.1 (le brouillon sera le refus poli + alternative légale).

### Tests — `tests/test_illegal_request.py`
- 14 nouveaux tests : détection positive (faire sortir, pirater WhatsApp, accéder téléphone, logiciel espion, sur écoute, mot de passe, relevés, NL hackeren, EN hack into) + négatifs (filature légitime, surveillance légale, question tarif simple) + brouillon #614 contient « infractions pénales » + alternative légale + pas de reformulation du piratage + signature Daniel + demande légitime ne déclenche pas le refus.
- **137/137 tests verts** sur la suite complète (aucune régression).

### Découvertes latérales
- `case_classifier` et `translator` tournent encore sur `openai/gemma4:31b` (modèle **obsolète** selon CLAUDE.md §3). À corriger dans une prochaine version (basculer sur kimi-k2.6:cloud). Hors-scope v1.24.1.

## [1.24.0] — 2026-06-22 (hardening détection — zéro client raté sur 3 patterns pièges)

### Contexte
Suite au meeting Daniel du 2026-06-22, trois clients réels ont été ratés par le classifier (0 brouillon généré, Daniel n'a pas eu de proposition). Tous partagent le même défaut : le classifier se fiait au **sujet** alors que le **body** contenait une vraie demande client. v1.24.0 inverse la priorité — le body l'emporte sur le sujet — via 3 règles déterministes.

| Mail | Client | Sujet trompeur | Classé | Vraie demande |
|---|---|---|---|---|
| #515 | Nathalie Hairemans | `[Privédetective België] Réinitialisation du mot de passe` (template WP mal configuré) | `facture` | Jalousie / sa nicht (formulaire WordPress) |
| #606 | Frédéric Van Houtte | `Re: Mission ouvrier en maladie` | `facture` | Follow-up avec coordonnées (TVA, GSM, employé) |
| #614 | Serge M | `іtѕⅿе-Bеvеіlіgіngѕmеldіng` (homoglyphes itsme) | `phishing` | Infidélité / filature Congo-WhatsApp |

### Ajouté — `app/pipeline/classifier.py`
3 fonctions de détection déterministe + 1 exception sécurisée au « jamais remonter depuis phishing » :

1. **`_is_wp_contact_form(body)`** — détecte les formulaires de contact WordPress (detectivebelgium.com NL : `Achternaam/Voornaam/Telefoonnummer` ; detectivebelgique.be FR : `Nom/Prénom/Téléphone`). Ces mails arrivent via un expéditeur technique (`mail@`/`wordpress@`/`contact@detective*`) avec un sujet parfois trompeur, mais le body structuré en champs est la signature fiable. Force `demande_client` depuis **toute catégorie** (y compris phishing/spam/newsletter) — un formulaire WP ne peut pas être un phishing.

2. **`_is_reply_to_daniel(body, sender)`** — détecte les réponses client à un mail de Daniel : `Re:` + body cite un mail de Daniel (préfixe `>` + signature `Daniel Hurchon` / `Chaussée Bara 213` / `Autorisation ministérielle` / `GSM 0471/31.81.20`) + expéditeur humain (pas un forwarder `mail@/wordpress@/contact@detective*` ni no-reply). Force `demande_client` depuis toute catégorie. Corrige le piège « Re: + citation d'un devis avec mots devis/facture/HTVA ».

3. **`_has_strong_human_demand(body)`** — exception sécurisée au « jamais remonter depuis phishing » : si le body a une demande humaine **forte** (prénom signé en fin de body + vocabulaire métier enquête + question de tarif) **sans** marqueur de phishing actif (`cliquez ici`, `votre compte a été suspendu`, `vérifiez votre identité`…), on autorise la remontée depuis phishing/spam/newsletter. Permet de rattraper #614 (sujet itsme leurre mais body demande directe de Serge M). Les URL (`https://`) sont volontairement **exclues** des marqueurs phishing : un client peut légitimement mentionner un profil Facebook.

4. **`_enforce_recall_over_precision` réécrite** — ordre de priorité clair (formulaire WP → réponse à Daniel → demande forte → heuristique humaine classique), chaque override loggué avec sa `rule` pour traçabilité.

### Tests — `tests/test_classifier_hardening.py`
- 17 nouveaux tests backportant les 3 cas réels (#515 NL, #606 citation Daniel, #614 demande forte) + anti-régression (vrai phishing itsme reste phishing, formulaire sans prénom/tarif ne remonte pas, expéditeur service ne déclenche pas reply_to_daniel, formulaire FR #615).
- **36/36 tests verts** sur le module hardening.
- **123/123 tests verts** sur la suite complète (aucune régression).

### Note opérationnelle
- Les 3 clients ratés (#515, #606, #614) doivent être **reclassés manuellement** après déploiement et leurs brouillons régénérés via le cockpit (`POST /api/drafts/{id}/retry`). Voir task #6.
- Découvertes latérales : (a) les formulaires WP ne demandent **jamais l'email** du client — le vrai contact = téléphone (`Telefoonnummer`) ; le brouillon devra dire « je vous appelle au 04xx » plutôt que répondre par email au forwarder. (b) `detective_belgium` n'a reçu aucun mail traité depuis le 11 juin (11 jours de silence) — le poller fonctionne (cycles OK), c'est un flux business bas, donc chaque mail compte double. (c) Bug RAG (point de vigilance #1 HANDOVER) toujours ouvert sur les 3 boîtes.

## [1.23.0] — 2026-06-18 (version stable — brouillons multilingues + messages originaux)

### Contexte
Bascule en version mineure 1.23.0 pour marquer la stabilisation du rendu des brouillons après les fixes 1.22.20 et 1.22.21.

### Changé
- Mise à jour de la source de vérité de version : `app/_version.py` → `VERSION = "1.23.0"`.
- Synchronisation des références version dans `README.md` et `HANDOVER.md`.

## [1.22.21] — 2026-06-18 (brouillons FR : message original manquant)

### Contexte
Daniel constate sur le mail #615 (FR) que la régénération cockpit ne met pas le message original du client sous la proposition. La v1.22.20 avait déplacé la gestion de l'original dans `draft_renderer.py`, mais `generate_draft()` ne l'appelait que pour les langues étrangères. Les brouillons FR ressortaient donc sans le message original.

### Changé
- **`app/pipeline/generator.py`** :
  - `render_draft_with_translations()` est désormais appelée **pour toutes les langues**, y compris le FR.
  - Pour le FR : la proposition est enrichie du message original du client en dessous.
  - Pour les autres langues : 4 blocs multilingues + message original en dessous (inchangé).
- **`tests/test_generator_draft.py`** :
  - Nouveau test garantissant qu'un `demande_client` en FR inclut le bloc `MESSAGE ORIGINAL DU CLIENT` sous la proposition.

### Tests
- **106/106 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.20] — 2026-06-18 (rendu brouillons multilingues + message original complet)

### Contexte
CDAL remonte deux problèmes de rendu des brouillons signalés par Daniel :
- Les emails arrivant dans une langue autre que le FR (NL/EN/DE/...) ne suivaient plus la structure du mail #503 (original → traduction FR → proposition FR → proposition traduite dans la langue source).
- Le message original complet du client n'était plus systématiquement positionné sous la proposition, ce qui gênait la relecture de Daniel.

### Changé
- **`app/pipeline/draft_renderer.py`** :
  - Pour les emails en FR : la proposition FR est désormais suivie du message original du client en dessous.
  - Pour les emails en langue étrangère : restauration de la structure 4 blocs (#503) avec le message original complet ajouté en pied.
- **`app/delivery/imap_draft.py`** :
  - Suppression du bloc redondant "=== MESSAGE ORIGINAL DU CLIENT ===" dans `_build_draft_body()` : l'original est désormais géré exclusivement par `draft_renderer.py`.
- **`tests/test_draft_renderer.py`**, **`tests/test_imap_draft.py`** :
  - Nouveaux tests couvrant le rendu FR, le rendu NL 4 blocs + original, et l'absence de duplication par la couche IMAP.

### Tests
- **105/105 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.19] — 2026-06-18 (follow-up : prénom dans thread cité + cockpit retry)

### Contexte
Suite au fix du mail #603, CDAL demande d'améliorer le brouillon de follow-up :
- Le #603 avait un body très court et le prénom "Sophie" se trouvait dans le thread cité ; le brouillon s'ouvrait donc sur "Bonjour,".
- L'endpoint cockpit `POST /drafts/{id}/retry` ne prenait pas en compte la détection follow-up et régénérait le brouillon qualifiant standard au lieu du brouillon court.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - `build_followup_ack_draft()` cherche le prénom dans 3 sources : signature du mail actuel, infos client extraites du body entier, et salutations `Bonjour <Prénom>,` dans le thread cité.
- **`app/web/api.py`** :
  - Nouvelle fonction `_is_web_followup()` détecte les réponses client depuis le cockpit (sujet `Re:`, marqueurs body).
  - `draft_generate()` passe `is_followup_response` à `generate_draft()` pour que les retry depuis l'interface respectent la nouvelle logique.

### Tests
- **101/101 tests verts** avec `venv/bin/python -m pytest -q`.
- Ajout de `test_build_followup_ack_draft_extracts_first_name_from_quoted_thread`.

## [1.22.18] — 2026-06-18 (réponses client : brouillon de remerciement, pas requalification — mail #586)

### Contexte
CDAL constate que quand un client répond à un mail de Daniel (ex. mail #586 : "Voici...", "En réponse à..."), Charlie envoyait la proposition standard de qualification avec toutes les questions. Or le client a probablement déjà reçu cette proposition lors de son premier contact. Si le même expéditeur a déjà envoyé un mail `demande_client` dans les 30 derniers jours, Charlie doit désormais envoyer un brouillon court de remerciement : *"Merci pour ces compléments d'informations, je vous reviens dès que possible..."*.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - Nouvelle fonction `build_followup_ack_draft()` : génère un brouillon court professionnel de remerciement + promesse de recontact, sans bloc tarifaire ni questions.
- **`app/workers/imap_poller.py`** :
  - `_is_client_followup()` : détecte une réponse client via `In-Reply-To`/`References`, sujet `Re:` ou marqueurs body (`voici`, `ci-joint`, `en réponse à`, `comme demandé`, etc.).
  - Vérifie ensuite si l'expéditeur a un mail `demande_client` datant de moins de 30 jours (parsing RFC 2822 de `received_at`).
  - Passe le flag `is_followup_response` à `generate_draft()`.
- **`app/pipeline/generator.py`** :
  - `generate_draft()` accepte `is_followup_response` et route vers `build_followup_ack_draft()` au lieu du brouillon qualifiant standard.

### Tests
- **100/100 tests verts** avec `venv/bin/python -m pytest -q`.
- Ajout de `test_build_followup_ack_draft` et 4 tests sur `_is_client_followup`.

## [1.22.17] — 2026-06-18 (correction classification newsletter corporate — mail #593)

### Contexte
Mail #593 provenant de `greetje.daneel@bauermediaoutdoor.com` (Google Ads / Bauer Media) : un message de mise à jour de politique/publicitaire a été classifié à tort en `demande_client`, générant un brouillon qualifiant inapproprié. Le mot "devis" présent dans un sujet de type `Re:` a faussement déclenché le rappel "toute demande de devis = demande_client".

### Changé
- **`app/pipeline/classifier.py`** :
  - `_looks_like_human_question()` : liste `service_senders` enrichie de `ads-google`, `googleads`, `google-ads`, `bauermedia`, `outdoor.com`.
  - Ajout de marqueurs corporate/out-of-office dans le body (`dear customer`, `dear advertiser`, `dear partner`, `the google ads team`, `1600 amphitheatre parkway`, `privacy-enhancing technologies`, `platform program policies`, `eu user consent policy`, `transparency and consent framework`).
  - Rejets explicites des sujets `Re: devis/facture/provision/avenant/contrat/commande/offre/bon de commande` qui n'indiquent pas un humain.
- **`app/workers/imap_poller.py`** :
  - `_is_verified_demande_client()` : détection renforcée des bodies corporate/out-of-office (Google Ads Team, Google LLC, privacy policy, TCF, etc.) avant de valider une `demande_client`.
- **`app/prompts/classifier_prompt.txt`** :
  - Ajout de l'exemple 13 (Bauer Media / Google Ads policy update) → `newsletter`.
  - Précision : le mot "devis" dans un email automatique de fournisseur ne transforme pas le robot en prospect.

### Tests
- **95/95 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.16] — 2026-06-18 (faux positifs email/nom/véhicule/horaires — mail #592)

### Contexte
Mail #592 envoyé depuis le formulaire web : le `sender` stocké est `mail@detectivebelgique.be` et le body commence par "je suis avec un avocat...". Le brouillon qualifiant affichait donc :
- **Votre email : mail@detectivebelgique.be** (email interne du cabinet) ;
- **Vos nom et prénom : avec un avocat Prodeo maître** (faux positif sur "je suis avec...") ;
- **Véhicule / adresse de départ / horaires** parasites car le body est un seul bloc sans ponctuation.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - `_NOM_COMPLET_PATTERN` : supprimé `re.IGNORECASE` et alternances explicites `[Mm]on nom...|[Jj]e suis` pour exiger que chaque mot du nom commence par une majuscule. Élimine "je suis avec un avocat...".
  - `_is_internal_email()` : nouvelle fonction qui ignore les emails internes (`detectivebelgique.be`, `detectivebelgium.com`, `dpdhuinvestigations.be`, `digitalhs.biz`, `no-reply*`) comme fallback email client.
  - `_VEHICULE_PATTERN` : s'arrête sur les transitions `travaille`, `et cette`, `pour le prouver`, `car` pour ne pas avaler les horaires/lieu.
  - `_ADRESSE_DEPART_PATTERN` : s'arrête sur `possédant`, `avec`, `travaille`, `et`, `car` pour ne pas avaler le véhicule/horaires.
  - `_HORAIRE_PATTERN` : capture une indication temporelle optionnelle (`semaine du 18 juin`) + `travaille/horaire/créneau/travail` + heure, avec contexte restreint.

### Tests
- **95/95 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.15] — 2026-06-18 (extraction qualifiante renforcée — mails #598 et #601)

### Contexte
Suite à la livraison v1.22.14, deux mails réels ont montré des cas d'extraction encore perfectibles :
- **Mail #598** (filature, réponse depuis un formulaire web) : l'adresse cible, la relation (épouse/madame), l'absence de véhicule et les habitudes n'étaient pas extraits correctement.
- **Mail #601** (incapacité de travail, body non structuré) : le nom du prospect, son adresse, l'employeur et la personne concernée (Segers Grégory) étaient parasités par des faux positifs.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - `_INFO_STOP` / `_INFO_STOP_ADDRESS` : arrêt sur `gsm`, `rue`, `avenue`, `boulevard` pour éviter que le nom/adresse ne débordent sur les champs suivants.
  - `_NOM_COMPLET_PATTERN` : limite aux espaces horizontaux et à 2-5 mots pour ne pas absorber l'adresse postale.
  - `_ADRESSE_BE_PATTERN` : tolère des compléments entre le numéro et le code postal (ex. `(Bierset), Grace-Hollogne`) et s'arrête proprement en fin de ville.
  - `_CLIENT_INFO_LABELS["heure_contact"]` / `["profil"]` : exigent désormais un séparateur explicite (`:`, `-`, `=`, `?`) pour éviter les faux positifs dans le body libre.
  - `infidelite_filature` :
    - Détection de la relation `épouse / conjointe / madame` même sans nom propre.
    - Extraction de l'adresse cible via `Coordonnées de madame`, `Elle habite`, etc.
    - Détection explicite de l'absence de véhicule (`pas de voiture`, `pas de véhicule`).
    - Nettoyage des habitudes pour capturer toute la phrase (jusqu'au point) sans empiéter sur le paragraphe suivant.
  - `incapacite_travail` :
    - Extraction du nom/prénom et de l'adresse connue de la personne concernée via `_NOM_CIBLE_PATTERN` et la 2ème adresse postale.
    - Nouveau `_EMPLOYEUR_PATTERN` pour capturer l'employeur/lieu de travail avec son adresse.
    - Nouveau `_LIEU_SUSPECT_PATTERN` plus strict (maîtresse, domicile conjugal, adresse connue) évitant d'accrocher `je ne sais pas l'adresse`.
  - `_format_received_info()` : affiche désormais la personne concernée et son adresse pour `incapacite_travail`, et capitalise aussi le `nom_complet`.
  - `_clean_snippet()` : supprime un label `Adresse :` résiduel.

### Tests
- **95/95 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.14] — 2026-06-18 (brouillons qualifiants intelligents pour tous les cas)

### Contexte
Mail #601 (Sophie) : le prospect avait déjà transmis son nom, son adresse, son GSM, le nom de la cible, l'adresse de départ, les horaires, les habitudes et le véhicule. Le brouillon deterministe redemandait pourtant la totalité de la liste. CDAL demande que **chaque cas de figure** détecte les informations déjà fournies et ne redemande que les éléments manquants.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - `_extract_client_info()` : extraction robuste des coordonnées client (nom, prénom, GSM, email, adresse, heure de contact, profil) même sans séparateur explicite (ex. `gsm 0491502786`), fallback adresse postale belge, extraction nom complet (`mon nom est Bassem Sophie`), fallback prénom depuis salutation du thread cité.
  - `_extract_case_info()` : extraction spécifique au cas de figure :
    - `infidelite_filature` : nom/prénom cible, adresse de départ, horaires, habitudes, véhicule, photo.
    - `recherche_personne` : nom/prénom recherché, date de naissance/âge, région/pays.
    - `incapacite_travail` : certificat/arrêt, horaire, lieu/employeur suspecté.
    - `securite_passé_violences` & `contre_espionnage_micros` : contexte spécifique.
  - `_strip_quoted_thread()` & `_body_without_signature()` : suppression du thread cité et de la zone de signature pour éviter les faux positifs (ex. nom du signataire pris pour la cible).
  - `_build_standard_draft()` : pour tous les cas hors `recuperation_dette`, le brouillon affiche désormais les éléments reçus, filtre les questions déjà répondues et adapte le closing si le dossier est déjà complet.
  - `_CASE_QUESTION_SPECS` : mapping question → clés d'info pour filtrage déterministe.
  - `_clean_snippet()` : nettoyage des extraits de phrase sans couper au premier retour à la ligne.
  - Post-traitement des habitudes : priorité aux indices forts (maîtresse, dort) avant les indices généraux (samedi, dimanche).
- **`tests/test_qualification_builder.py`** :
  - Ajout de `test_build_draft_for_sophie_601_filters_answered_questions` avec le vrai body du mail #601.
  - Vérifie que seule la photo reste à demander et que les éléments déjà fournis ne sont pas redemandés.

### Tests
- **95/95 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.13] — 2026-06-16 (brouillon dette : ne pas redemander ce qu'on a déjà)

### Contexte
CDAL corrige : si le prospect a déjà transmis nom, email, GSM, etc., Charlie ne doit pas les redemander — il doit les lister comme reçus et ne demander que ce qui manque.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - Ajout de `_extract_client_info(body, sender)` avec regex sur labels classiques (Nom, Prénom, Téléphone/GSM, Email, Adresse, Heure de contact, Profil).
  - Ajout de `_format_received_info()` qui capitalise nom/prénom et formate la section "Voici les éléments que nous avons bien reçus de votre part :".
  - `_build_dette_draft()` :
    - Affiche les informations client déjà connues.
    - Sépare clairement "Concernant la créance", "Concernant la personne concernée" et "De votre côté, pour finaliser le dossier" (adresse manquante).
    - Ne redemande jamais le GSM, l'email ou le nom/prénom s'ils sont déjà présents.
    - Closing corrigé : "Bien à vous," + prénom sur sa propre ligne.
- **`tests/test_qualification_builder_dette.py`** : assertions mises à jour pour valider la section "déjà reçus", l'absence de redemande et le closing.
- **`app/_version.py`** : bump `1.22.12` → `1.22.13`.

### Tests
- **94/94 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.12] — 2026-06-16 (fix brouillon dette)

### Contexte
Déploiement de v1.22.11 puis test local du cas `recuperation_dette` : le brouillon mélangeait les questions de base génériques avec les questions spécifiques dette, produisant une liste incohérente et des doublons.

### Fixé
- **`app/pipeline/qualification_builder.py`** :
  - Isolation des questions dette : `recuperation_dette` utilise uniquement `_CASE_QUESTIONS["recuperation_dette"]`.
  - Ajout explicite d'une demande de coordonnées client (nom, prénom, adresse, GSM) dans `_build_dette_draft()`.
  - Suppression du doublon "reconnaissance de dette" en ne listant que `questions[1:]` (la première question est traitée dans l'intro).
- **`tests/test_qualification_builder_dette.py`** : assertions renforcées pour vérifier l'absence des questions génériques et la présence des questions dette.
- **`app/_version.py`** : bump `1.22.11` → `1.22.12`.

### Tests
- **94/94 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.11] — 2026-06-16 (cas récupération de dette)

### Contexte
CDAL partage une vraie demande client de récupération de dette et la réponse idéale de Daniel. Besoin : ajouter un nouveau cas de figure `recuperation_dette` avec un brouillon adapté.

### Ajouté
- **`app/pipeline/case_classifier.py`** :
  - Nouveau cas `recuperation_dette` dans `CASE_TYPES`, le prompt JSON, `_case_to_label()` et le fallback keyword (`dette`, `argent`, `doit`, `créance`, `recouvrement`, `reconnaissance de dette`, etc.).
- **`app/pipeline/qualification_builder.py`** :
  - `_CASE_QUESTIONS["recuperation_dette"]` : 6 questions sur documents, identité, adresse, contacts, employeur, biens.
  - `_CASE_LABELS["recuperation_dette"]` : "une récupération de dette ou de créance".
  - `_rephrase_need()` : intro "Nous accusons bonne réception de votre demande concernant une personne de votre entourage qui vous doit une somme importante d'argent."
  - `_build_dette_draft()` : structure proche du modèle Daniel (intro, question document, liste à puces, closing légal, "Bien à vous").
- **`tests/test_case_classifier_dette.py`** : 2 tests fallback.
- **`tests/test_qualification_builder_dette.py`** : 1 test structure.
- **`app/_version.py`** : bump `1.22.10` → `1.22.11`.

### Tests
- **94/94 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.10] — 2026-06-16 (polish brouillon déterministe)

### Contexte
Premier test du simulateur en prod (v1.22.9). CDAL demande 3 ajustements de wording pour que le brouillon sonne davantage comme écrit par Daniel.

### Changé
- **`app/pipeline/qualification_builder.py`** :
  - Intro questions : "Afin de préparer votre dossier dans les meilleures conditions, et pouvoir vous donner une estimation de devis fiable, pourriez-vous me transmettre les éléments suivants :"
  - Relais final : "Dès réception de ces éléments, je reprendrai contact avec vous pour finaliser le devis et convenir d'un échange téléphonique sur ce nouveau dossier."
- **`tests/test_qualification_builder.py`** : assertions mises à jour.
- **`app/_version.py`** : bump `1.22.9` → `1.22.10`.

### Tests
- **91/91 tests verts** avec `venv/bin/python -m pytest -q`.

## [1.22.9] — 2026-06-16 (Simulateur de brouillon super-admin)

### Contexte
CDAL veut pouvoir tester les brouillons directement depuis le cockpit, sans envoyer de vrai email, pour itérer sur la qualité et éduquer Charlie.

### Ajouté
- **`app/web/admin.py`** :
  - `GET /admin/draft-simulator` : page super-admin avec formulaire (boîte source, catégorie, sujet, corps).
  - `POST /admin/api/draft-simulator/run` : génère le brouillon en appelant `generate_draft()` directement. RAG et Cerveau2 sont mockés pour un test rapide. Le classifier de cas est appelé en vrai. Log d'audit `draft_simulator_run`.
- **`app/web/templates/admin/draft_simulator.html`** : interface HTMX avec textarea, sélecteur de boîte/catégorie, spinner et affichage du brouillon généré.
- **`app/web/templates/base.html`** : entrée de menu "🧪 Simulateur brouillon" réservée super-admin.
- **`tests/test_admin_draft_simulator.py`** : 3 tests (page admin OK, génération OK, rejet anonyme).

### Changé
- **`tests/test_case_classifier.py`** : refactor ruff clean (lignes longues, variables non utilisées).
- **`app/web/admin.py`** : corrections E501 sur des chaînes HTML préexistantes.
- **`app/_version.py`** : bump `1.22.8` → `1.22.9`.

### Tests
- **91/91 tests verts** avec `venv/bin/python -m pytest -q`.
- Test local cockpit simulé non effectué (nécessite `.env` complet + DB initialisée).

## [1.22.8] — 2026-06-16 (fix qualification déterministe — bug brouillon #582)

### Contexte
Le brouillon #582 généré en production par la v1.22.7 a obtenu une note de **0/10** : aucune salutation, aucune question obligatoire (nom, prénom, GSM, adresse…), ton robotique, tarifs et relais Daniel absents. Les tests en local avec `gemma4:31b`, `kimi-k2.6:cloud` et `glm-5.1:cloud` ont montré qu'aucun modèle disponible ne suit de façon fiable une consigne « liste impérativement les questions manquantes sous forme numérotée ».

### Fixé
- **`app/pipeline/qualification_builder.py`** : nouveau builder déterministe qui construit le brouillon qualifiant par code :
  - Salutation personnalisée avec extraction du prénom du signataire depuis la fin du mail.
  - Reformulation du besoin contextualisée (ex. "surveillance concernant l'agissement d'un collaborateur").
  - 6 questions de base obligatoires + 3 questions spécifiques au cas détecté.
  - Bloc tarifaire configurable (ouverture de dossier, rapport, heures jour/nuit).
  - Rappel systématique de la règle des 2 détectives pour filatures/surveillance mobile.
  - Relais Daniel pour finalisation du devis et appel de clôture.
  - Signature `Daniel Hurchon / {marque} / GSM 0471/31.81.20 / contact@detectivebelgique.be`.
- **`app/pipeline/generator.py`** : branche `demande_client`/`prise_contact` (`DRAFT_CATEGORIES`) sur `build_qualification_draft()` au lieu du LLM brut. Le flux LLM few-shot est conservé pour les autres catégories.
- **`app/pipeline/case_classifier.py`** : corrections ruff (prompt JSON multiligne + fallback keyword formaté). **Fix v1.22.8a** : le fallback keyword utilise maintenant les mots entiers (`\b...\b`) et s'applique au mail original (sujet + body), pas à la réponse LLM — corrige les faux positifs `incapacite_travail` dus au mot "travail" dans "lieu de travail".
- **`app/pipeline/qualification_builder.py` + `app/pipeline/generator.py`** : corrections ruff (E501, F541, I001, UP017).

### Ajouté (testability sans envoyer d'email)
- **`scripts/test_draft_qualification.py`** : script de test local qui appelle `generate_draft()` directement sans IMAP. RAG et Cerveau2 sont mockés, le classifier est appelé en vrai. 5 cas prédéfinis + possibilité de passer `--subject` / `--body`.
- **`tests/test_qualification_builder.py`** : tests unitaires du builder déterministe (extraction prénom, questions par cas, tarifs, signature).

### Supprimé
- Scripts temporaires de debug : `scripts/test_draft_local.py`, `scripts/test_draft_deterministic.py`, `scripts/test_draft_simple.py`.

### Tests
- **88/88 tests verts** avec `venv/bin/python -m pytest -q`.
- Test local `scripts/test_draft_qualification.py --case filature_collaborateur` : brouillon correct de 9 questions, cas `infidelite_filature`, salutation "Bonjour Christophe,".

## [1.22.7] — 2026-06-16 (qualification prospect dans les brouillons)

### Contexte
Les brouillons de réponse doivent mieux qualifier les prospects dès le premier contact. Daniel a besoin de toutes les informations clés pour faire un appel de clôture avec un devis solide. Un fichier de consignes métier a été fourni par CDAL.

### Ajouté
- **`app/prompts/prospect_qualification.md`** : directive complète de qualification client — règles générales, formulaire de base technique, 5 cas de figures avec questions spécifiques, séquence type et transparence tarifaire.
- **`app/pipeline/case_classifier.py`** : module dédié qui détecte le cas de figure principal du mail entrant (`incapacite_travail`, `infidelite_filature`, `recherche_personne`, `securite_passé_violences`, `contre_espionnage_micros`, `non_determine`) avec confiance et raison. Modèle configurable via `LLM_MODEL_QUALIFIER` (défaut `openai/gemma4:31b`).
- **`app/pipeline/generator.py`** : intègre la directive de qualification et le cas détecté dans le system prompt ; max_tokens passé à 2500 pour accueillir les questions complètes.
- **`app/workers/imap_poller.py`** : les brouillons sont maintenant générés pour toutes les catégories listées dans `DRAFT_CATEGORIES` (`demande_client,prise_contact` par défaut), pas seulement `demande_client`.
- **`app/config.py`** : nouveaux paramètres `llm_model_qualifier`, tarifs ajustables (`dossier_opening_fee`, `report_fee`, `hourly_rate_day`, `hourly_rate_night_weekend`) et `draft_categories`.
- **`app/settings_store.py`** : `get_llm_model_qualifier()` pour lecture runtime DB/env.
- **`.env.example`** : variables `LLM_MODEL_QUALIFIER`, tarifs et `DRAFT_CATEGORIES` documentées.

### Tests
- `tests/test_case_classifier.py` : 7 tests couvrant l'extraction JSON, le fallback keyword et les erreurs LLM.
- **82/82 tests verts**.

## [1.22.6] — 2026-06-16 (fix UI Copier + logs Actions Daniel)

### Contexte
Suite à la demande de review et aux constats VPS :
- Le bouton **Copier** de la réponse proposée par Charlie sur `/app/conversations/{id}` ne fonctionnait pas (Alpine.js `@click` inline non fiable dans le contexte HTMX).
- Les mails `demande_client` de `detective_belgium` arrivent correctement `pending`/`high` en base ; ils passent ensuite `approved` suite aux actions de Daniel (`user_id=2`) dans le cockpit. Besoin de visibilité sur ces actions.

### Fixé / Ajouté
- **`app/web/templates/app/conversation.html`** : remplacement du bouton Copier Alpine par un listener vanilla JS délégué (`js-copy-draft`), compatible HTTPS + localhost, avec fallback `document.execCommand('copy')` si `navigator.clipboard` n'est pas disponible.
- **`app/web/admin.py`**: nouvelle requête `daniel_actions` récupérant les 50 dernières actions de `user_id=2` (`draft_approve`, `draft_reject`, `status_update`, `manual_draft`, `draft_save`) depuis `audit_logs`.
- **`app/web/templates/admin/audit.html`** : nouvelle section "👤 Dernières actions de Daniel" dans la page Logs, avec date, action, ressource et détails.
- **`app/_version.py`** : bump `1.22.5` → `1.22.6`.

## [1.22.5] — 2026-06-16 (fix robustesse mémoire + tests Cerveau2)

### Contexte
Suite à la relecture du projet, **16 tests étaient rouges** et 2 bugs de robustesse ont été identifiés :
- `query_vault` retourne un tuple `(notes, answer)` depuis plusieurs versions, mais les tests mockaient encore une liste unique.
- `charlie_memory.query_memory` / `query_corrections` / `save_memory` / `save_feedback` / `get_good_memories` plantaient avec `OperationalError: no such table` si la base n'était pas initialisée (cas test `db_path=/dev/null` ou démarrage partiel).
- `_is_vault_relevant` référençait `_VAULT_KEYWORDS` non défini → `NameError` si appelée.
- `_extract_dossier_id` ne capturait pas "affaire XYZ123".

### Fixé
- **`app/charlie_memory.py`** : toutes les fonctions publiques (`query_memory`, `query_corrections`, `save_memory`, `save_feedback`, `get_good_memories`) appellent désormais `init_memory_table()` en début de traitement, avec capture `sqlite3.OperationalError` et dégradation silencieuse (`return []` ou `-1`) si la base est inaccessible. Cela évite les crashs en cas de DB partiellement initialisée.
- **`app/charlie.py`** :
  - Définition de `_VAULT_KEYWORDS` (mots-clés métier normalisés) pour que `_is_vault_relevant()` fonctionne si elle est appelée.
  - Ajout du pattern `_AFFAIRE_RE` dans `_extract_dossier_id()` pour capturer "affaire XYZ123".
- **`tests/test_cerveau_client.py`** : adaptation au tuple `(notes, answer)` retourné par `query_vault` ; `test_ok_response_returns_notes` passe `context_only=False` pour tester la réponse LLM.
- **`tests/test_charlie_vault.py`** : adaptation des mocks au tuple ; correction de `test_ask_charlie_calls_vault_when_no_sql` qui utilisait "Bonjour" (intercepté par `_general_response`) ; renommage de `test_ask_charlie_no_vault_for_pure_sql` en `test_ask_charlie_vault_called_even_for_count_sql` car le vault est maintenant systématiquement requêté.
- **`tests/test_cerveau_feed.py`** : date du mock passée au 15 juin 2026 pour ne plus être skipée par le filtre `process_since_date=2026-06-01`.

### Bilan tests
- **75/75 tests verts** avec `venv/bin/python -m pytest -q`.
- Note : la commande `pytest` globale est liée à Python 3.9 sur ce Mac ; il faut utiliser le venv Python 3.14.

## [1.22.4] — 2026-06-15 (fix few-shot loading — le LLM voit ENFIN le VRAI Daniel)

### Contexte — BUG LATENT CRITIQUE

Suite au feed du `human_draft` de Daniel pour le mail #561 (Soldermann — correction 1990 chars, capturée 2026-06-15T07:10 UTC), le test du loader `_load_daniel_fewshot()` retourne **0 candidat** alors que 2 mails correspondent aux critères (mail #561 + mail #83 du 2026-05-22). Root cause : le filtre SQL `WHERE date(received_at) >= ?` ne parse PAS le format RFC 2822 — la colonne est stockée en `Sat, 13 Jun 2026 05:41:38 +0000`, format que la fonction SQLite `date()` ne sait pas interpréter. **Conséquence** : depuis la v1.22.0 (livraison du few-shot learning), le system prompt a TOUJOURS été injecté avec un bloc few-shot VIDE. Le LLM n'a JAMAIS vu le vrai style Daniel. Les brouillons générés étaient du "Daniel simulé" basé uniquement sur `personality_daniel.txt`, jamais sur les corrections validées.

### Fixé
- **`app/pipeline/generator.py::_load_daniel_fewshot()` — v1.22.4** : le filtre temporel est maintenant fait EN PYTHON (regex RFC 2822) après récupération d'un panel de 200 candidats en SQL. Pattern : on prend large côté SQL (`body>200 AND (hd>100 OR status=sent)`), on trie par human_draft DESC puis received_at DESC, on parse la date avec `_RFC2822 = re.compile(r'[A-Za-z]{3},\s+(\d+)\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})')`, on garde les N plus récents dans la fenêtre 30 jours. Pattern réutilisé de `scripts/cleanup_old_drafts.py` (même parsing).
- **Re-test live sur VPS** (data réelle) : 2 candidats récupérés au lieu de 0.
  - **#561** (Soldermann, 2026-06-13) : `human_draft` 1990 chars — la correction Daniel CORRECTEMENT chargée.
  - **#83** (Wastiau, 2026-05-22) : `human_draft` 997 chars.
- **Effet immédiat** : la prochaine génération de brouillon (nouveau mail `demande_client` ou retry manuel via `POST /api/drafts/{id}/retry`) injectera dans le system prompt ces 2 vrais exemples signés Daniel. Charlie imitera le ton (formel, structuré par paragraphes, mention "On vous téléphonera...", "Bien cordialement, Daniel Hurchon"), la structure (intro + estimation × N scénarios + infos pour convention + paiement), et le niveau de détail (prix HTVA, mention "kilométrage à calculer", "provision 60 %").

### Bilan déploiement
- Test live avant deploy : `Candidates from SQL: 2 / Top 5 (after Python date filter): #561 + #83`. Le fix est validé.

### Anti-régression
- Pattern RFC 2822 parsing mutualisé — `_parse_received_at()` interne à `_load_daniel_fewshot()` (pas encore extrait en helper partagé, à factoriser si d'autres modules en ont besoin).
- Si un nouveau mail ne se parse pas, il est simplement ignoré (pas de crash), warn log `generator.fewshot_load_failed` reste en filet de sécurité.
- Le panel SQL reste borné à 200 lignes → pas de risque OOM si la table grossit à 10K+.

## [1.22.2] — 2026-06-10 (livraison IMAP des brouillons backfillés)

### Contexte — Suites du hotfix v1.22.1

Le backfill v1.22.1 a régénéré **76 brouillons** (mail #504 + 27 du run `--limit 50` + 48 du run `--apply` complet) pour des mails historiques reclassifiés `demande_client`. **Mais** le poller IMAP ne re-livre pas les brouillons existants — sa condition `if category == "demande_client" and is_new` (imap_poller.py:1298) ne se déclenche que pour les nouveaux mails vus pour la première fois. Conséquence : **76 brouillons en base, 0 dans les Drafts IMAP de Daniel**.

### Ajouté
- **`scripts/deliver_pending_drafts.py`** — script one-shot qui :
  1. Ajoute la colonne `delivered_at` (idempotent via `PRAGMA table_info`).
  2. Lit `mail_processed WHERE category='demande_client' AND draft_generated=1 AND delivered_at IS NULL`.
  3. Pour chaque mail, appelle `append_draft()` pour déposer en IMAP Drafts de la boîte source (flag `\Draft`, sujet `DEMANDE D'Approbation - Reponse Demande Client : ...`).
  4. Marque `delivered_at` pour idempotence (pas de redépot).
- **Flags** : `--apply` (défaut dry-run), `--limit N`, `--only-id 504`.
- **Logs structurés** : `deliver.start`, `deliver.candidates`, `deliver.ok`, `deliver.failed`, `deliver.done`.

### Fixé
- **CRLF dans les sujets** (v1.22.2 hotfix) : les invitations Google Calendar et convocations A.G. ont des `\r\n` dans le sujet (`Updated invitation: ... @ Wed 3 Jun 2026 5pm - 5:45pm\r\n (WITA) ...`), interdit par RFC 5322. `_sanitize_subject()` remplace CRLF/CR/LF par espace avant de construire le header MIME. **14 mails re-livrés** (LUC.BUCHEL/Nationale Loterij, upfdpb/UPDPB x4, arshan@qrosh.be NL, cdal@digitalhs.biz x3 invitations).
- **`raw_draft` n'existe pas** en table `mail_processed` (schéma fixé avant v1.22.0). Le SELECT se base sur `ai_draft` uniquement.
- **`message_id` n'existe pas non plus** en table. Fallback sur `imap_uid` pour `IncomingMail.message_id`.

### Bilan déploiement
- **Run 1** : 152 candidats, 138 livrés, 14 échecs (CRLF sujet).
- **Run 2** (post-fix sanitize) : 14 candidats, 14 livrés, 0 échec.
- **Total livré** : **153/154** (1 mail filtré en amont = brouillon vide + sujet vide, correctement ignoré).
- **Daniel a maintenant 153 brouillons en attente d'approbation** dans ses 3 boîtes `Drafts` (detective_belgique, detective_belgium, dpdh_investigations).

### Procédure d'activation post-deploy
```bash
# 1. Deployer la nouvelle image
bash scripts/deploy-to-vps.sh

# 2. Dry-run pour validation
docker exec detective-app python -m scripts.deliver_pending_drafts --limit 10

# 3. Apply (livraison réelle)
docker exec detective-app python -m scripts.deliver_pending_drafts --apply
```

### Anti-régression
- Marquage `delivered_at` empêche le redépot lors de runs multiples.
- Un brouillon déjà délivré (présence dans `delivered_at`) n'est jamais re-traité.
- Si `append_draft` échoue, le mail reste `delivered_at IS NULL` et sera retenté au prochain run.
- Sujet sanitizé systématiquement — protection contre CRLF/CR/LF dans tous les cas.

## [1.22.1] — 2026-06-10 (durcissement classifier — ZÉRO client raté)

### Contexte — BUG P0 MÉTIER

Daniel a signalé le 2026-06-10 qu'un mail client (id #504, zabougafz@gmail.com) était mal catégorisé : `Re: demande d'un détective pour une personne` avec body `c'est combien le tarif exacte svp` était classé `facture` au lieu de `demande_client`. Conséquence : **0 brouillon généré**, Daniel n'a pas eu de proposition pour ce client. C'est symptomatique d'un biais du LLM classifier qui se laisse abuser par :
- Un `Re:` (suggère une réponse passée, pas une nouvelle demande)
- Une citation d'un devis/devis dans le corps (suggère une facture)

**Décision CDAL (2026-06-10)** : **RÈGLE D'OR ABSOLUE** = on ne rate AUCUN `demande_client`. Faux positifs acceptables (Daniel les rejette à la lecture), faux négatifs intolérables (on perd un client). C'est non-négociable pour le business.

### Ajouté
- **`app/pipeline/classifier.py::classify()`** — post-traitement `_enforce_recall_over_precision()` qui force `demande_client` quand le LLM hésite (entre `autre`/`facture`/`rappel`/`urgent` → `demande_client`). L'heuristique `_looks_like_human_question()` détecte les indices humains forts : questions tarif/devis, salutations multilingues (FR/NL/EN), vocabulaire enquête (filature/surveillance/infidélité), sender non-service. **N'override JAMAIS** depuis `phishing`/`spam`/`newsletter` (sécurité).
- **Nouveau module de tests** `tests/test_classifier_hardening.py` — 19 tests couvrent : cas #504 (replay), cas Breyne NL, sender service, newsletter, 2FA Infomaniak, body trop court, override depuis chaque catégorie source, intégration classify() avec LLM mocké. **19/19 verts**.
- **`scripts/backfill_reclassify.py`** — script one-shot qui re-classe les ~150 mails du backlog (catégorie `autre`/`facture`/`rappel`/`urgent` avec `draft_generated=0`). Flags : `--apply` (défaut dry-run), `--limit N`, `--only-id 504`. Pour chaque mail reclassifié en `demande_client` : update `category`, `status='pending'`, `priority='high'`, regénère le brouillon via `generate_draft()`. Log `backfill.demande_client_found` pour traçabilité.
- **`app/prompts/classifier_prompt.txt`** — règle d'or remontée en haut du prompt (v1.22.1) + 2 few-shots ajoutés : cas #504 (replay explicite) + cas NL "Vraagje over offerte".

### Inchangé
- Le flow poller→classifier→generator reste identique, seul le classifier gagne un post-traitement.
- Le prompt v1.22.0 (personnalité Daniel) n'est pas touché.
- La température du classifier reste 0.0 (déterministe), le coût par appel reste ~15 tokens.

### Anti-régression
- Le test `test_recall_override_newsletter_kept` et `test_recall_override_phishing_kept` ancrent le comportement : **on ne remonte JAMAIS** depuis `newsletter` ou `phishing`. Le risque d'over-correction est contenu.
- Si Daniel trouve trop de **faux positifs** (des mails classés `demande_client` à tort) : ajuster la liste `_HUMAN_QUESTION_SIGNALS` dans `app/pipeline/classifier.py` (réduire les matches ambigus) ou durcir la garde "1 hit OU 2 hints" en "1 hit ET 1 hint" (plus strict).

### Note opérationnelle
- **Bump 1.22.0 → 1.22.1** (patch) documente un fix critique P0.
- **Action immédiate après deploy** : `python -m scripts.backfill_reclassify --only-id 504 --apply` (traite le mail qui a déclenché le hotfix). Puis `python -m scripts.backfill_reclassify --limit 50 --apply` pour traiter 50 mails prioritaires. Le reste peut tourner en background.
- **Action manuelle #504** : le brouillon généré par backfill atterrit dans `mail_processed.ai_draft`. Pour qu'il arrive dans la Drafts IMAP de Daniel, ouvrir le mail dans le cockpit → bouton "Régénérer" → l'endpoint `POST /api/drafts/{id}/generate` fait l'APPEND IMAP.
- **Déploiement** : `bash scripts/deploy-to-vps.sh` (pre-flight + build local + push image + healthcheck).

## [1.22.0] — 2026-06-05 (refonte qualité LLM — Charlie colle à Daniel)

### Contexte
Daniel demande d'améliorer la qualité des réponses proposées par Charlie. Constat sur 2 mails récents (#477, #478) : les brouillons Charlie étaient **trop génériques**, manquaient de **personnalisation** (noms client/mandant), ne **posaient pas de questions** pour faire avancer le dossier, donnaient peu de **méthodologie concrète**, et surtout ne poussaient **PAS vers le RDV téléphonique/visio** — qui est le levier de closing #1 de Daniel. Objectif v1.22.0 : faire produire par Charlie un brouillon que Daniel n'a qu'à cliquer "Envoyer" sans réécriture, et qui pousse naturellement vers l'échange direct.

### Ajouté
- **`app/prompts/personality_daniel.txt` — réécrit complètement** (~3× plus long, 124 lignes vs 51). Nouvelles sections : OBJECTIF, CTA RDV téléphonique/visio (closing), PERSONNALISATION (noms client + mandant), VOCABULAIRE PROFESSIONNEL (ordre de mission, agenda des opérations, réactivation dossier, etc.), STRUCTURE ATTENDUE (paragraphes thématiques aérés), ANTIPATTERNS (pas de pavé, pas de remerciement creux, annoncer la suite), PATTERNS BON/MAUVAIS (2 exemples courts calibrés sur les vrais mails #477 et #478 de Daniel).
- **Few-shot in-context learning dynamique** : nouvelle fonction `_load_daniel_fewshot()` dans `app/pipeline/generator.py`. À chaque génération, on injecte dans le system prompt les **3-4 dernières vraies réponses validées par Daniel** (priorité aux `human_draft` = corrections explicites, fallback `status=sent`). Sélection : 30 derniers jours, body > 200 chars, tri par date desc. Le LLM voit **le vrai Daniel écrire à de vrais clients**, pas un style approximatif.
- **`rag_top_k` 5 → 10** (`app/config.py`) : double le nombre de cas historiques Q/R injectés en contexte. Coût tokens +1K, négligeable.
- **`cerveau2_limit` 3 → 8** (`app/config.py`) : plus de notes du second cerveau Vault dans le contexte. Coût tokens +1K, négligeable.
- **`max_tokens` 1500 → 2000** (`app/pipeline/generator.py`) : permet les réponses Daniel-like 15-30 lignes sans troncature.

### Inchangé
- Le modèle LLM reste `kimi-k2.6:cloud` (32K context window — on a la marge).
- Le flow de génération (RAG + vault + prompt) reste identique, juste avec plus de contexte.
- La température 0.4 reste OK pour le peu de variabilité nécessaire.

### Note opérationnelle
**Bump mineur 1.21.9 → 1.22.0** documente une refonte qualité (semver mineur car pas de breaking change d'API). **Action immédiate** : sur les 3-5 prochains mails entrants, Daniel valide si la qualité est OK. Si une régression (trop formel, trop rigide, mauvaise structure), tweaker `personality_daniel.txt` (priorité : c'est un fichier de prompt, facile à faire évoluer). **Coût LLM** : ~+3K tokens par mail, sur 50 mails/jour ça fait +150K tokens/jour = ~$0.30/jour (kimi-k2.6:cloud à $2/M tokens output, $0.50/M input). Négligeable.

### Anti-régression
Si Daniel trouve que Charlie devient **trop formel** ou **trop rigide** après ce changement : c'est probablement l'effet "patterns BON/MAUVAIS" qui force trop le LLM. Solution : retirer la section PATTERNS COURTS du prompt, garder juste les sections +VOCABULAIRE et +CTA RDV. Itérer.

---

## [1.21.9] — 2026-06-05 (fix P0 — brouillons IMAP ne se déposaient plus sur detective_belgique)

### Contexte — BUG P0 PRODUCTION
Depuis au moins 10 jours (et donc depuis le 29/05, premier impact client visible), la boîte `detective_belgique` d'Infomaniak **refuse toutes les commandes LIST avec pattern** : `Error in IMAP command LIST: Invalid pattern (0.001 + 0.000 secs).` — y compris `LIST "" "*"`. Conséquence : `_find_drafts_folder()` retournait `None`, le brouillon n'était jamais déposé dans Drafts, et le fallback Resend prenait le relais (qui jusqu'à v1.21.8 envoyait à CDAL au lieu de Daniel). **Daniel n'a donc reçu aucun brouillon depuis 8 jours** sur la boîte `detective_belgique` (la plus active). Cause probable : restriction de sécurité Infomaniak sur cette boîte spécifique (trop de dossiers ? quota LIST dépassé ?).

### Diagnostic clé
Test direct via Docker exec sur le container Charlie : `SELECT Brouillons` retourne **OK** sur cette boîte. Donc le dossier existe bien, c'est **uniquement** la commande `LIST` qui est bloquée. Le dossier peut être sélectionné directement par son nom, sans avoir besoin de le lister d'abord.

### Fix
- **`app/delivery/imap_draft.py` — `_find_drafts_folder()`** réécrite : au lieu de faire `LIST "" "*"` puis matcher les noms, on tente directement `SELECT` sur chaque nom candidat (`Brouillons`, `Drafts`, `INBOX.Brouillons`, `INBOX.Drafts`, `Draft`, `Brouillon`, etc.). Le premier qui répond `OK` est retenu. **On revient à `SELECT INBOX` à la fin** pour ne pas perturber le poller qui s'attend à ce que la mailbox sélectionnée soit INBOX.
- **LIST conservé en fallback ultime** : si tous les SELECT probes échouent, on tente LIST quand même, au cas où d'autres boîtes Infomaniak acceptent encore le pattern. Ça ne change rien pour ces boîtes (qui marchaient déjà).
- **Couvre toutes les variantes** (FR/NL/EN, avec/sans INBOX préfixe) — déduplication avec `seen: set[str]` pour éviter les probes redondants.

### Inchangé
- Le reste du pipeline d'APPEND (`append_draft()`, `_verify_draft_present()`) reste identique. Seul `_find_drafts_folder` change.
- Le fix v1.21.8 (fallback Resend → Daniel to + CDAL cc) reste valide : si pour une raison X le nouveau code échoue quand même, le fallback va à Daniel.

### Note opérationnelle
**Bump 1.21.8 → 1.21.9** documente un fix critique P0. **Action immédiate après deploy** : dans le cockpit, aller sur les mails #480 et #481 (status=pending), cliquer "Régénérer" → le brouillon devrait se déposer **directement dans la Drafts IMAP de Daniel** cette fois, plus de fallback Resend. Vérifier aussi que les brouillons du 29/05 au 05/06 (6 mails listés) ont bien atterri dans la Drafts — sinon les re-régénérer. Daniel peut aussi vérifier sa Drafts IMAP directement via webmail Infomaniak (https://mail.infomaniak.com).

---

## [1.21.8] — 2026-06-05 (fix critique — fallback Resend va enfin à Daniel)

### Contexte — BUG P0 PRODUCTION
**Daniel n'a reçu AUCUN brouillon de Charlie depuis le 29/05/2026** (capture : sa boîte "Brouillons" est vide depuis cette date, ~8 jours de panne silencieuse côté client). Cause : le fallback Resend (utilisé quand l'APPEND IMAP échoue — « Connexion IMAP secondaire rejetée par Infomaniak ») envoyait à `cdal@digitalhs.biz` au lieu de Daniel. Conséquence : CDAL recevait l'alerte + la proposition en fallback, mais Daniel voyait rien. **Le client attend 8 jours pour rien** pendant qu'on a l'impression côté CDAL que tout va bien. C'est le pattern « zéro crash silencieux » porté sur la livraison : un échec **métier** non alerté au bon destinataire.

### Fix
- **Nouvelles variables de config** (`app/config.py`) : `draft_recipient_to` (Daniel par défaut `contact@detectivebelgique.be`) et `draft_recipient_cc` (CDAL par défaut `cdal@digitalhs.biz`). L'ancienne `draft_recipient=cdal@digitalhs.biz` est conservée pour les alertes système (Resend `alert_imap_draft_failure`).
- **`app/delivery/resend_notifier.py`** : `payload` Resend utilise maintenant `to=[settings.draft_recipient_to]` + `cc=[settings.draft_recipient_cc]`. Log `resend.sent` mentionne les 2 destinataires.
- **`.env.example` + `.env.production`** sur VPS : ajout des 2 nouvelles variables avec valeurs par défaut Daniel/CDAL.

### À investiguer en parallèle (cause primaire)
Le fallback ne devrait **jamais** se déclencher. Le vrai problème = « connexion IMAP secondaire rejetée par Infomaniak » dans le message d'alerte. Charlie ouvre 2 connexions IMAP simultanées (polling + APPEND Drafts), Infomaniak rejette la 2e. Fix à creuser : sérialiser les opérations IMAP, ou réutiliser la connexion du poller pour l'APPEND. **Hors-scope de ce hotfix** (à traiter proprement en v1.22.0 pour éviter une régression de stabilité du poller). En attendant : Daniel reçoit le brouillon en fallback Resend dans sa boîte, donc plus de panne visible client.

### Note opérationnelle
Bump 1.21.7 → 1.21.8 documente un fix critique. **Action immédiate** : aller dans la Drafts de Daniel sur les 3 boîtes (Infomaniak webmail) et rejouer manuellement les 6-8 brouillons manqués depuis le 29/05 (Charlie les a en base, ils sont régénérables via `POST /api/drafts/{id}/retry` du cockpit, ou via le bouton « Régénérer » sur la conversation). Liste à extraire : `SELECT id, mailbox_name, subject FROM mail_processed WHERE draft_generated=1 AND created_at >= '2026-05-29' AND status IN ('agent_attempted')` — devrait lister ~6-10 mails.

---

## [1.21.7] — 2026-06-05 (audit log systématique par cycle de polling)

### Contexte
CDAL : « je veux une trace dans /audit que le poller a eu lieu même si pas d'email retiré : il nous faut du log de qualité pour moi et le client ». Jusqu'ici `/audit` ne montrait que les events **métier** (login, brouillon créé, etc.). Le poller, lui, loggait dans `agent_telemetry` (section « Cycles poller 24h » du template), pas dans `audit_logs`. Conséquence : pour Daniel ou CDAL, l'absence d'event audit = impossible de distinguer « pas de mail reçu » (silence normal) de « Charlie est down » (incident). Avec ce changement, **chaque fin de cycle de polling écrit 1 ligne dans `audit_logs`** (cycles vides inclus), avec `action=poller.cycle`, `resource_type=mailbox`, `resource_id=<nom_boîte>`, et un suffixe `ok` / `empty` pour distinguer d'un coup d'œil.

### Ajouté
- **`_log_audit()` dans `app/workers/imap_poller.py`** (juste après `_log_telemetry`) : insère une ligne dans `audit_logs` avec `action='poller.cycle'`, `resource_type='mailbox'`, `resource_id=<mailbox_name>`, `details='<cycle_result> | <details>'`, `user_agent='charlie-poller'`, `user_id=NULL`, `ip_address=NULL`, `created_at=now()`. **Best-effort** : si l'INSERT échoue (DB lock, schema manquant), on log un warning `poller.audit_log_failed` et on continue. Le poller ne doit JAMAIS crasher pour une raison d'audit.
- **Appel à la fin de chaque cycle** (dans `_process_mailbox`, juste après l'appel `_log_telemetry` existant) : passe `cycle_result='ok'` si ≥ 1 mail traité, `cycle_result='empty'` si 0 mail. Les cycles en erreur (catch plus haut avec `raise`) ne sont volontairement pas audités ici — ils passent par un autre canal (alerte Resend + Slack via `_maybe_alert_poller_failure`).

### Inchangé
- Le poller n'écrit toujours pas dans `audit_logs` quand le cycle se termine en **erreur IMAP** (le `except Exception: raise` court-circuite avant l'audit). C'est volontaire : les erreurs sont déjà tracées via `poller.mail_error` (log structuré) + alertes Resend/Slack après 5 échecs consécutifs.
- La section « 🔄 Cycles poller (24h) » du template `admin/audit.html` continue d'afficher la **télémétrie** (`agent_telemetry`) — c'est une vue différente, plus granulaire (chaque event est listé). La nouvelle ligne `audit_logs` apparaît dans la **table principale** en bas, avec un tri `created_at DESC` standard.

### Note opérationnelle
Bump mineur 1.21.6 → 1.21.7 documente l'ajout d'audit. Aucun changement de logique métier : on ajoute une traçabilité. Charge DB supplémentaire : 1 INSERT par cycle = 12 INSERT/heure (3 boîtes × 4 cycles/heure = 12). Négligeable. Volume `audit_logs` attendu : ~350 lignes/jour → ~12K/mois → rotation à voir dans 6 mois.

---

## [1.21.6] — 2026-06-05 (visuel inbox — badge brouillon sur la colonne Boîte)

### Contexte
CDAL demande un signal visuel fort dans l'inbox list : **l'identifiant de boîte (D_FR / D_NL / D_PD) doit devenir très visible quand Charlie a généré un brouillon de réponse**. Aujourd'hui c'est juste du texte gris 12px sur fond noir — facile à rater. Daniel doit pouvoir scanner sa liste de mails en un coup d'œil et voir d'un coup d'œil "ce mail a une proposition de réponse prête".

### Ajouté
- **Pilule émeraude + ✉️ sur la 1ère colonne de l'inbox** quand `mail_processed.ai_draft IS NOT NULL AND length(ai_draft) > 0`. Style : `inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold ring-1 ring-emerald-500/40` + `border-l-4 border-l-emerald-400` sur la cellule. Tooltip natif : `title="Proposition de réponse générée par Charlie"`. Cohérent avec le fond `bg-green-900/40` des hot-rows (les deux utilisent la palette émeraude) mais **plus compact** (juste la colonne Boîte, pas la ligne entière).
- **`ai_draft` dans la query `_fetch_mails`** (`app/web/app_routes.py`) : ajouté aux 2 SELECT (hot + other) et à la liste `cols`. **Coût perf** : 1 colonne TEXT de plus, négligeable (≤200 lignes dans l'inbox, mode `pragma journal_mode=WAL`).
- **Variable Jinja `has_draft`** dans `inbox_rows.html` : détecte `ai_draft is not none and |length > 0` (le `|length` évite les cas où `ai_draft=""` en base).

### Inchangé
- Le rendu sans brouillon (texte gris normal) reste exactement comme avant. Aucun mail sans brouillon ne reçoit le badge.
- Le mode édition détaillée (`/app/conversation/{id}`) n'est pas touché : le badge est **uniquement dans la liste inbox** comme demandé.
- Le changement de catégorie/priorité en mode list (`htmx-trigger="change"`) reste fonctionnel — la 1ère colonne n'est pas dans un `<form>`, aucun conflit.
- Le 1er mail de Daniel (#474, mail de test classé `autre`) **n'a PAS reçu** de brouillon → il s'affiche en gris normal. Daniel a manuellement overridé en `demande_client` + `high` ; le badge apparaîtra dès que le brouillon sera régénéré (cockpit "Régénérer" ou endpoint `/api/drafts/{id}/retry`).

### Note opérationnelle
Bump mineur 1.21.5 → 1.21.6 documente l'ajout visuel. Aucun changement de logique backend. Si tu revois un badge apparaître sur un mail sans brouillon, vérifier que `ai_draft` n'est pas `""` ou `null` en base (probablement une ancienne migration). Query de vérif rapide : `SELECT id, mailbox_name, subject, length(ai_draft) FROM mail_processed WHERE ai_draft IS NOT NULL AND length(ai_draft) > 0 AND draft_generated = 0 LIMIT 10;` — devrait retourner 0 ligne en prod.

---

## [1.21.5] — 2026-06-04 (zéro crash silencieux — alertes multi-canaux + heartbeat)

### Contexte
CDAL : « il faut s'assurer que plus jamais de crash sans être prévenu : c'est impossible et interdit ». Le hotfix v1.21.3 alertait déjà par email Resend après 5 crashes/boîte, mais **un seul canal = un seul SPOF**. Si Resend tombe ou si CDAL ne lit pas ses emails pendant 3 jours, on rate l'alerte. Et si Charlie crash COMPLÈTEMENT (OOM, kill -9), aucune alerte in-app ne s'exécute. **v1.21.5 ajoute 2 niveaux de redondance** (Slack + heartbeat au démarrage) et prépare le terrain pour le watchdog externe (cron VPS) qui sera ajouté demain.

### Ajouté
- **Alerte Slack en parallèle de Resend** : nouvelle fonction `_send_slack_crash_alert()` dans `app/alerts.py`. Appelée automatiquement depuis `alert_poller_persistent_failure()` après l'envoi Resend réussi. **Best-effort** : si Slack est down, on log un warning et on continue. Canal = Webhook Slack (déjà câblé, pas de setup additionnel côté Slack). Format message : `:rotating_light: Poller IMAP — échecs consécutifs (N erreurs sur mailbox) + dernière erreur + UIDs + action`.
- **`notify_startup(version)` dans `app/alerts.py`** : notification Slack au démarrage réussi de l'agent (`:white_check_mark: Charlie AI démarré — v1.21.5`). **Couvre le cas "Charlie a redémarré après un crash"** : si CDAL voit passer 2 startups en 5 min sur Slack, il sait qu'il y a un problème. Best-effort.
- **`notify_shutdown(reason)` dans `app/alerts.py`** : notification Slack à l'arrêt propre (`:wave: Charlie AI arrêté — raison : stop_requested`). Permet de distinguer arrêt intentionnel vs crash. Best-effort.
- **Appel dans `app/main.py`** : `notify_startup(VERSION)` après l'init complète, `notify_shutdown(...)` après `stop_event.wait()`. Les deux sont wrappés en `try/except` pour ne pas bloquer le démarrage/arrêt.
- **Log `agent.startup_notify_failed` / `agent.shutdown_notify_failed`** : trace claire si la notif Slack a planté (utile pour debug).

### Préparé pour demain (non déployé, TODO S+1)
- **Watchdog externe** : cron VPS qui curl `/health` toutes les 60s et alerte si 3 checks consécutifs échouent. Couvre OOM, kill -9, deadlock asyncio, disque plein. Alerte même si Charlie est totalement HS.
- **Uptime checker externe** : exposer `/healthz` via Traefik + inscription à Healthchecks.io (gratuit, < 5 min de setup). Vérifie de l'extérieur, pas depuis le VPS lui-même.
- **Cleanup auto disk + images Docker** : cron hebdo `/usr/local/bin/detective-docker-clean.sh` (analogue à `magicreator-docker-clean.sh` qui existe déjà).

### Inchangé (toujours actif)
- v1.21.3 : alerte email Resend à `cdal@digitalhs.biz` si ≥5 crashes/boîte (anti-spam 1h/boîte)
- v1.21.3 : compteur `consecutive_errors` dans `HealthState`
- v1.21.3 : 19 tests de résilience verts

### Note opérationnelle
- Bump 1.21.4 → 1.21.5 (mineur) documente l'ajout des alertes Slack.
- Le webhook Slack `SLACK_WEBHOOK_URL` est déjà configuré en prod (.env.production). Pas de changement de secrets.
- Si tu reçois trop de notifications Slack au démarrage (ex: restart toutes les 5 min), c'est qu'il y a un crashloop → aller voir les logs VPS `docker logs --tail=100 detective-agent`.

---

## [1.21.4] — 2026-06-04 (filtre date 1er juin 2026 + doc patterns réutilisables)

### Contexte
Après le hotfix v1.21.3 (poller IMAP cassé depuis ~26h), Daniel rappelle qu'il veut **uniquement les mails du 1er juin 2026 à aujourd'hui** (4 juin). Le code hardcodait `datetime(2026, 5, 20)` comme date limite, donc les mails de fin mai étaient skippés. Décision CDAL : passer à **1er juin 2026 strict**.

En parallèle, CDAL demande de **documenter les patterns réutilisables** depuis ce hotfix, car il développe en parallèle le produit **Second Cerveau Pro** (`SECONDCERVEAU-PRO/`) et l'instance `CDAL2/`. Tout client Second Cerveau Pro qui scrape IMAP doit intégrer ces patches.

### Changé
- **`app/workers/imap_poller.py`** : date limite passée de `datetime(2026, 5, 20)` à `datetime(2026, 6, 1)`. Le log `poller.date_skipped` utilise maintenant `reason="before_2026-06-01"`.
- **`.env.example`** : `PROCESS_SINCE_DATE=2026-06-01` (aligné sur la décision).
- **`app/config.py`** : commentaires mis à jour (exemples pointent sur `2026-06-01`).

### À faire manuellement
Les 4 mails historique (UIDs 4910, 5914, 9368, 9376) sont **datés d'avant le 1er juin 2026** → le filtre va les skipper à nouveau. Si Daniel veut les traiter quand même, retirer le flag `AgentProcessed` via Thunderbird. Sinon, ils restent skippés (comportement souhaité).

### Note opérationnelle
Aucun autre changement. Le hotfix v1.21.3 (3 bugs IMAP + alerte) est conservé tel quel. Le bump mineur v1.21.3 → v1.21.4 documente le changement de date.

### Ajouté (doc)
- **`docs/PATTERNS_FROM_CHARLIE_V1.21.3.md`** : note technique complète sur les 3 bugs IMAP génériques (charset unknown-8bit, sqlite3 Header binding, retry éternel) + observabilité (compteur d'erreurs + alerte Resend) + 19 tests. **Cible** : backport dans Second Cerveau Pro / CDAL2 / tout nouveau client IMAP.
- **`docs/CERVEAU2_INTEGRATION.md` section 9** : pointe vers `PATTERNS_FROM_CHARLIE_V1.21.3.md` pour les agents externes qui scrapent IMAP.
- **`HANDOVER.md` section 9 "Point de vigilance #11"** : clarifie que v1.21.3 et v1.21.4 sont **100% côté Charlie**, **0 changement** dans `app/cerveau_client.py`, `CERVEAU2-DEtective/`, `SECONDCERVEAU-PRO/`, `CDAL2/`.
- **`CLAUDE.md` section "Documentation Cerveau2"** : ajout du lien vers le nouveau doc.

---

## [1.21.3] — 2026-06-04 (hotfix prod — poller IMAP cassé depuis ~26h)

### Contexte
Le poller IMAP crashe sur **chaque** mail de la boîte `detective_belgique` depuis le déploiement v1.21.2. **0 brouillon généré** depuis ~26h, 13 retries en boucle sur certains UIDs (9368, 9376, 5914). Daniel s'est plaint de l'absence de propositions de réponse. **3 bugs cumulés** identifiés et corrigés, plus un système d'alerte pour qu'on soit prévenu la prochaine fois.

### Fixé
- **Crash `LookupError` sur charset `unknown-8bit`** (38 occurrences en 200 logs) : `_decode_header` ne savait pas gérer les charsets exotiques (RFC 2047 incompliant). Patch : chaîne de fallback `charset → utf-8 → latin-1 → replace` + `try/except HeaderParseError` autour de `decode_header()`.
- **Crash `sqlite3.ProgrammingError: type 'Header' is not supported`** (12 occurrences) : quand `subject`/`sender`/`received_at` sont des `email.header.Header` au lieu de `str`, sqlite refuse la sérialisation. Patch : coercion `str()` défensive à l'entrée de `_persist` (ceinture + bretelles) + à l'acquisition de `received_at` (`str(msg.get("Date", "") or "")`).
- **Retry éternel structurel** : le flag `AgentProcessed` n'était posé qu'en cas de succès complet. Tout crash en cours pipeline → mail rejoué toutes les 5 min indéfiniment. Patch : nouveau flag `AgentAttempted` posé dans la branche `except` du try/except englobant → libère la queue IMAP, classe le mail comme "à inspecter manuellement" sans le rejouer en boucle. Le mail d'alerte explique comment retirer le flag via IMAP/Thunderbird pour forcer le rejeu.
- **`_is_verified_demande_client`** : `str()` défensif autour de `msg.get("From")` et `msg.get("Subject")` (cohérence avec le reste du pipeline).

### Ajouté
- **Try/except englobant dans `_process_single_mail`** : enveloppe tout le corps du pipeline. En cas d'exception :
  - `log.exception("poller.mail_crash", ...)` + télémétrie `poller_mail_crash` en DB (thread-safe via `asyncio.to_thread`)
  - Incrément du compteur `consecutive_errors` dans `HealthState`
  - **Alerte Resend à `cdal@digitalhs.biz`** si le compteur dépasse `poller_alert_threshold=5`
  - Pose du flag `AgentAttempted` → libère la queue
  - Retour `"error"` pour ne pas gonfler les `cycle_stats`
- **Compteur d'erreurs consécutives par boîte** dans `app/healthcheck.py` : `mark_error(mailbox)`, `reset_errors(mailbox)`, `error_snapshot()`, exposé dans `health.snapshot()`.
- **Alerte email poller persistant** dans `app/alerts.py` : `alert_poller_persistent_failure(mailbox_name, error_count, last_error, sample_uids)`. **Anti-spam 1h/boîte** (cooldown 3600s). Le mail contient : dernière erreur, échantillon d'UIDs, action requise (retirer le flag `AgentAttempted` via IMAP/Thunderbird).
- **Helper `_maybe_alert_poller_failure`** : fire-and-forget via `asyncio.create_task()` (n'await pas l'envoi Resend pour ne pas figer le poller).
- **Reset automatique** du compteur quand un cycle traite au moins 1 mail avec succès (pas de reset sur cycle vide, qui est suspect d'un autre bug en amont).
- **Setting `poller_alert_threshold: int = 5`** dans `app/config.py` (ajustable code uniquement, pas env).
- **Constante `AGENT_ATTEMPTED_FLAG = "AgentAttempted"`** dans `app/workers/imap_poller.py` (sans `$`, conforme Infomaniak).
- **19 tests de résilience** dans `tests/test_imap_poller_resilience.py` :
  - 4 tests `_decode_header` (charsets exotiques, fallback, garbage, vide)
  - 3 tests `_persist` avec `Header` objects
  - 3 tests `_process_single_mail` (try/except, télémétrie, pas d'`AgentAttempted` sur succès)
  - 6 tests compteur d'erreurs + alerte (seuil, reset, anti-spam)
  - 3 tests anti-spam 1h/boîte de l'alerte Resend

### Changé
- **`_process_single_mail`** : corps indenté d'un niveau pour wrapper dans le `try/except`. Le comportement nominal est strictement identique en cas de succès.

### Note opérationnelle
Le flag `AgentAttempted` a été posé sur les UIDs 9368/9376/5914 (et tous les autres en boucle de retry) au moment du deploy. Pour les rejouer après correction de la cause racine : retirer le flag manuellement via IMAP/Thunderbird (clic droit sur le mail → Flags → "AgentAttempted"). Procédure documentée dans le mail d'alerte.

---

## [1.21.2] — 2026-06-03 (hotfix — nettoyage traces de raisonnement kimi-k2.6)

### Fixé
- **Traces de raisonnement kimi-k2.6 dans le contenu retourné** : le modèle produit des artefacts type "L'utilisateur demande...", "Points importants :", "Structure possible :", "The user wants...", "Let me analyze...", "Refonte :", "Version plus X :", "C'est mieux." etc. qui polluaient les brouillons de réponse.
- **Auto-critique post-mail** : si le LLM écrit un mail puis le reprend ("Version plus Hurchon :", "C'est mieux.", "Refonte :"), on garde la **première** version et on tronque juste avant la critique.
- **Guillemets résiduels autour de la signature** : "Daniel Hurchon\"" → "Daniel Hurchon".
- **Patterns multilingues** : kimi-k2.6 raisonne en anglais sur des inputs FR/NL → ajout patterns EN (The user, Let me, I need, But wait, Given the style, etc.).

### Ajouté
- **`_clean_reasoning()` dans `app/llm/router.py`** : 30+ patterns regex qui identifient les traces de raisonnement typiques (FR + EN + listes + guillemets + auto-critique).

---

## [1.21.1] — 2026-06-03 (hotfix critique — modèle kimi-k2.6:cloud + reasoning_content)

### Fixé
- **Nom du modèle Ollama corrigé** : le vrai nom est `kimi-k2.6:cloud` (et non `kimi-k2`). Le `.env.production` du VPS utilisait en plus `gemma4:31b` (obsolète) et `claude-sonnet-4` (404 sur OpenRouter).
- **Extraction `reasoning_content` pour kimi-k2.6:cloud** : ce modèle est un *reasoning model* — sa réponse finale est dans `message.reasoning_content` et `message.content` reste vide. Notre wrapper litellm ne lisait que `.content` → fallback systématique vers `glm-5.1` (plus lent). Fix : si `.content` est vide, fallback sur `.reasoning_content`.
- **Base URL Ollama Pro** : défaut était `https://ollama.com/api` (mauvais), corrigé à `https://ollama.com/v1` (endpoint OpenAI-compatible attendu par litellm).
- **Table `app_settings` purgée** : 3 lignes obsolètes (`llm_model_default`, `llm_model_classifier`, `llm_model_fallback`) pointaient sur les anciens noms → supprimées pour retomber sur les défauts corrigés.

### Changé
- **`app/config.py`** : défauts `llm_model_default/fallback/classifier/chat` = `openai/kimi-k2.6:cloud` + `openai/glm-5.1:cloud`.
- **`.env.example`** : aligné sur la prod (noms corrects, base URL correcte).
- **VPS `.env.production`** : corrigé en SSH (cf. procédure).

---

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
