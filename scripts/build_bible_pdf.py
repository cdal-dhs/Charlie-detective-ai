"""Génère la bible PDF complète du projet Detective.be Agent.

Usage : python scripts/build_bible_pdf.py
Sortie : docs/Bible_DetectiveBE.pdf

Le PDF est reproductible : ré-exécuter quand spec/roadmap évoluent.
Texte intégralement sélectionnable et copiable (prompts, commandes, .env, etc.).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).parent.parent
OUTPUT = ROOT / "docs" / "Bible_DetectiveBE.pdf"

NAVY = colors.HexColor("#0b2545")
ACCENT = colors.HexColor("#13315c")
MUTED = colors.HexColor("#5b6b7c")
CODE_BG = colors.HexColor("#f4f6f8")
CODE_BORDER = colors.HexColor("#d6dce3")
WARN_BG = colors.HexColor("#fff7e6")
WARN_BORDER = colors.HexColor("#f0c674")


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

styles = getSampleStyleSheet()

TITLE = ParagraphStyle(
    "BibleTitle", parent=styles["Title"], fontName="Helvetica-Bold",
    fontSize=32, leading=38, textColor=NAVY, alignment=TA_CENTER, spaceAfter=20,
)
SUBTITLE = ParagraphStyle(
    "BibleSubtitle", parent=styles["Title"], fontName="Helvetica",
    fontSize=16, leading=22, textColor=ACCENT, alignment=TA_CENTER, spaceAfter=10,
)
META = ParagraphStyle(
    "BibleMeta", parent=styles["Normal"], fontName="Helvetica",
    fontSize=11, leading=16, textColor=MUTED, alignment=TA_CENTER,
)
H1 = ParagraphStyle(
    "BibleH1", parent=styles["Heading1"], fontName="Helvetica-Bold",
    fontSize=20, leading=24, textColor=NAVY, spaceBefore=18, spaceAfter=10,
    keepWithNext=True,
)
H2 = ParagraphStyle(
    "BibleH2", parent=styles["Heading2"], fontName="Helvetica-Bold",
    fontSize=14, leading=18, textColor=ACCENT, spaceBefore=14, spaceAfter=6,
    keepWithNext=True,
)
H3 = ParagraphStyle(
    "BibleH3", parent=styles["Heading3"], fontName="Helvetica-Bold",
    fontSize=11, leading=14, textColor=NAVY, spaceBefore=10, spaceAfter=4,
    keepWithNext=True,
)
BODY = ParagraphStyle(
    "BibleBody", parent=styles["BodyText"], fontName="Helvetica",
    fontSize=10, leading=14, alignment=TA_JUSTIFY, spaceAfter=6,
)
BULLET = ParagraphStyle(
    "BibleBullet", parent=BODY, leftIndent=14, bulletIndent=2,
    bulletFontName="Helvetica", bulletFontSize=10, spaceAfter=3,
)
CODE = ParagraphStyle(
    "BibleCode", parent=styles["Code"], fontName="Courier",
    fontSize=8.5, leading=11, textColor=colors.black,
    backColor=CODE_BG, borderColor=CODE_BORDER, borderWidth=0.6,
    borderPadding=8, leftIndent=0, rightIndent=0,
    spaceBefore=6, spaceAfter=10,
)
PROMPT = ParagraphStyle(
    "BiblePrompt", parent=CODE, fontSize=9, leading=12,
    backColor=colors.HexColor("#fafbfc"),
)
WARN = ParagraphStyle(
    "BibleWarn", parent=BODY, backColor=WARN_BG,
    borderColor=WARN_BORDER, borderWidth=0.8, borderPadding=8,
    spaceBefore=6, spaceAfter=10, leftIndent=0, rightIndent=0,
)
TOC_LVL1 = ParagraphStyle(
    "TOC1", fontName="Helvetica-Bold", fontSize=11, leading=16,
    textColor=NAVY, leftIndent=0,
)
TOC_LVL2 = ParagraphStyle(
    "TOC2", fontName="Helvetica", fontSize=10, leading=14,
    textColor=ACCENT, leftIndent=14,
)


# ---------------------------------------------------------------------------
# DocTemplate avec page de garde + TOC + bookmarks pour les Hx
# ---------------------------------------------------------------------------

class BibleDoc(BaseDocTemplate):
    def __init__(self, filename: str, **kw) -> None:
        super().__init__(filename, pagesize=A4, leftMargin=2 * cm,
                         rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
                         title="Bible Detective.be Agent", author="CDAL — Digital HS",
                         **kw)
        cover_frame = Frame(self.leftMargin, self.bottomMargin,
                            self.width, self.height, id="cover")
        body_frame = Frame(self.leftMargin, self.bottomMargin,
                           self.width, self.height, id="body")
        self.addPageTemplates([
            PageTemplate(id="Cover", frames=[cover_frame]),
            PageTemplate(id="Body", frames=[body_frame], onPage=_draw_footer),
        ])

    def afterFlowable(self, flowable):
        if flowable.__class__.__name__ != "Paragraph":
            return
        style_name = flowable.style.name
        text = flowable.getPlainText()
        if style_name == "BibleH1":
            self.notify("TOCEntry", (0, text, self.page))
        elif style_name == "BibleH2":
            self.notify("TOCEntry", (1, text, self.page))


def _draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(2 * cm, 1.2 * cm, "Bible Detective.be Agent — CDAL / Digital HS")
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"page {doc.page - 1}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def p(text: str, style=BODY) -> Paragraph:
    return Paragraph(text, style)


def code(text: str, style=CODE) -> Preformatted:
    return Preformatted(text, style)


def warn(text: str) -> Paragraph:
    return Paragraph(f"<b>⚠ {text}</b>", WARN)


def bullets(items: list[str], style=BULLET) -> list[Paragraph]:
    return [Paragraph(item, style, bulletText="•") for item in items]


def table_kv(rows: list[tuple[str, str]], col_widths=(5 * cm, 11 * cm)) -> Table:
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.4, CODE_BORDER),
    ]))
    return t


def table_header(rows: list[list[str]], col_widths) -> Table:
    t = Table(rows, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.4, CODE_BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fb")]),
    ]))
    return t


# ---------------------------------------------------------------------------
# Construction du contenu
# ---------------------------------------------------------------------------

def build_story() -> list:
    s: list = []

    # ---------- COUVERTURE ----------
    s.append(Spacer(1, 4 * cm))
    s.append(p("Detective.be — Agent IA email", TITLE))
    s.append(p("Bible technique et opérationnelle du projet", SUBTITLE))
    s.append(Spacer(1, 3 * cm))
    cover_table = Table([
        ["Client", "Daniel Hurchon — Detective.be"],
        ["Marques", "Detective Belgique · Detective Belgium · DPDH Investigations"],
        ["Intégrateur", "CDAL — Digital HS (cdal@digitalhs.biz)"],
        ["Hébergement", "VPS Hostinger KVM8 (production) / Mac local (développement)"],
        ["LLM principal", "Kimi K2 via Ollama Pro · LiteLLM · OpenRouter (fallback)"],
        ["Date d'édition", date.today().strftime("%d %B %Y")],
        ["Version", "1.0 — MVP cadré"],
    ], colWidths=(4 * cm, 12 * cm), hAlign="CENTER")
    cover_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("TEXTCOLOR", (1, 0), (1, -1), MUTED),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, CODE_BORDER),
    ]))
    s.append(cover_table)
    s.append(Spacer(1, 3 * cm))
    s.append(p("Document confidentiel — usage interne projet uniquement.", META))

    s.append(NextPageTemplate("Body"))
    s.append(PageBreak())

    # ---------- TABLE DES MATIÈRES ----------
    s.append(p("Table des matières", H1))
    toc = TableOfContents()
    toc.levelStyles = [TOC_LVL1, TOC_LVL2]
    s.append(toc)
    s.append(PageBreak())

    # ---------- 1. RÉSUMÉ EXÉCUTIF ----------
    s.append(p("1. Résumé exécutif", H1))
    s.append(p(
        "Daniel Hurchon, détective privé belge, gère seul 3 boîtes mail Infomaniak (3 marques) "
        "et répond personnellement à environ 50 mails par jour mêlant demandes clients, factures, "
        "newsletters et spam. Le projet livre un agent IA Python qui surveille ces boîtes en continu, "
        "classifie les mails entrants, et génère un brouillon de réponse « à la Daniel » "
        "<b>uniquement pour les demandes clients</b>, en s'appuyant sur 1200 paires Q/R historiques "
        "anonymisées (RAG sur sqlite-vec)."
    ))
    s.append(p(
        "Au MVP, les brouillons sont envoyés par email à CDAL (intégrateur) via Resend pour "
        "validation qualité avant transfert à Daniel. La bascule vers dépôt direct dans le dossier "
        "Drafts IMAP de Daniel est planifiée en V2, après deux à quatre semaines de calibration."
    ))
    s.append(p("Chiffres clés", H2))
    s.append(table_kv([
        ("Boîtes surveillées", "3 (Infomaniak)"),
        ("Volume traité", "≈ 50 mails/jour"),
        ("Fréquence polling", "5 minutes"),
        ("Langues supportées", "FR / NL / EN (détection + réponse même langue)"),
        ("Délai cible MVP", "4 semaines (S1 → S4)"),
        ("Coût mensuel estimé", "≈ 25-30 € (Ollama Pro + OpenRouter ponctuel + backup)"),
        ("Hébergement", "VPS Hostinger KVM8 (8 vCPU / 32 Go RAM)"),
    ]))
    s.append(PageBreak())

    # ---------- 2. CONTEXTE BUSINESS ----------
    s.append(p("2. Contexte business", H1))
    s.append(p("2.1 Le client", H2))
    s.append(p(
        "Daniel Hurchon dirige seul un cabinet d'enquêtes privées. Pas de collaborateur intermédiaire, "
        "pas d'assistant. Il lit et répond aux mails depuis Outlook ou Apple Mail sur Mac. "
        "L'agent ne le remplace pas, il lui prépare le travail."
    ))
    s.append(p("2.2 Les 3 marques", H2))
    s.append(table_header([
        ["Marque", "Email", "Langue par défaut", "Public"],
        ["Detective Belgique", "contact@detectivebelgique.be", "FR", "francophones BE"],
        ["Detective Belgium", "contact@detectivebelgium.com", "EN", "international + NL"],
        ["DPDH Investigations", "info@dpdhuinvestigations.be", "FR", "dossiers spécifiques"],
    ], col_widths=(4 * cm, 6 * cm, 2.5 * cm, 4 * cm)))

    s.append(p("2.3 Typologie des mails entrants", H2))
    for item in [
        "<b>Demandes clients</b> (cible prioritaire MVP) : prospects, devis, suivis de dossier, relances",
        "<b>Factures / compta</b> : fournisseurs, OVH, Infomaniak, comptable",
        "<b>Newsletters</b> : marketing B2B, lettres d'info pro",
        "<b>Spam / phishing</b> : faux clients, tentatives d'arnaque",
        "<b>Urgences</b> : situation client critique, deadline imminente",
        "<b>Autre</b> : notifications systèmes, confirmations automatiques",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "•"

    s.append(p("2.4 Sensibilités métier (garde-fous éditoriaux)", H2))
    for item in [
        "<b>Confidentialité</b> : rappels discrets, jamais de divulgation d'autres dossiers",
        "<b>Pas d'engagement légal/contractuel par email</b> : prix, délais → renvoyer vers appel/RDV",
        "<b>Neutralité absolue</b> : pas de jugement sur la situation décrite",
        "<b>Vouvoiement par défaut</b>, tutoiement très rare",
        "<b>Concision</b> : Daniel répond court, va à l'essentiel",
        "<b>Toujours une porte de sortie vers un échange humain</b>",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "•"
    s.append(PageBreak())

    # ---------- 3. ARCHITECTURE CIBLE ----------
    s.append(p("3. Architecture cible", H1))
    s.append(p("3.1 Flux de bout en bout", H2))
    s.append(code(
        "[3 boîtes Infomaniak IMAP via app passwords]\n"
        "         |  polling 5 min (aioimaplib)\n"
        "         v\n"
        "[Worker Python asyncio, 1 task par boîte]\n"
        "         |  nouveau mail non-flaggé $AgentProcessed\n"
        "         v\n"
        "[Pipeline]\n"
        "  1. Pré-filtre règles  -> headers (List-Unsubscribe), expéditeurs\n"
        "                          connus -> tag IMAP + skip\n"
        "  2. Classification LLM -> 6 catégories (Kimi K2 via LiteLLM)\n"
        "  3. Si != demande_client -> tag IMAP catégorie + skip\n"
        "  4. Si demande_client :\n"
        "     a. Détection langue (fasttext FR/NL/EN)\n"
        "     b. Embedding du mail (multilingual-e5-large local CPU)\n"
        "     c. RAG -> top 5 paires Q/R similaires (sqlite-vec)\n"
        "     d. Génération brouillon (Kimi K2 par défaut)\n"
        "         |\n"
        "         v\n"
        "[Resend API] -> email formaté à cdal@digitalhs.biz\n"
        "[IMAP STORE] flag $AgentProcessed sur le mail entrant\n"
        "         |\n"
        "         v\n"
        "[CDAL valide -> forward à Daniel pour envoi]"
    ))
    s.append(p("3.2 Sous-systèmes (briques modulaires)", H2))
    s.append(table_header([
        ["#", "Sous-système", "Rôle"],
        ["1", "Worker IMAP", "Polling + détection nouveaux mails + flag idempotence"],
        ["2", "Pré-filtre règles", "Court-circuit sur newsletters / billing évidents"],
        ["3", "Classifier LLM", "6 catégories à partir du mail brut"],
        ["4", "Détection langue", "fasttext FR/NL/EN, défaut = langue de la boîte"],
        ["5", "RAG retrieval", "Top-K paires Q/R similaires depuis sqlite-vec"],
        ["6", "Generator", "Assemblage prompt système + few-shot + appel LLM"],
        ["7", "Delivery (Resend)", "Email HTML formaté → CDAL (MVP)"],
        ["8", "Healthcheck + alertes", "FastAPI /health + bot Telegram"],
    ], col_widths=(0.8 * cm, 4 * cm, 11.2 * cm)))
    s.append(PageBreak())

    # ---------- 4. STACK TECHNIQUE ----------
    s.append(p("4. Stack technique", H1))
    s.append(table_header([
        ["Couche", "Choix", "Justification"],
        ["Runtime", "Python 3.11+", "Écosystème mail/RAG/LLM riche"],
        ["Concurrence", "asyncio (1 task/boîte)", "Suffisant pour 50 mails/jour"],
        ["IMAP", "aioimaplib", "Async, gère reconnexions"],
        ["LLM router", "LiteLLM (proxy OpenAI-compat)", "Switch facile Kimi K2 / OpenRouter"],
        ["LLM principal", "Kimi K2 via Ollama Pro (20€/mois)", "Qualité top, coût plafonné"],
        ["LLM fallback", "OpenRouter (Claude / GPT-4o)", "Spécialisation par tâche"],
        ["Embeddings", "intfloat/multilingual-e5-large", "Gratuit, local CPU, FR/NL/EN excellent"],
        ["Vector store", "sqlite-vec", "Vit dans les DB existantes"],
        ["Détection langue", "fasttext (lid.176.bin)", "Local, instantané"],
        ["Email outbound", "Resend API", "Simple, free tier suffisant"],
        ["État/queue/logs", "agent_state.db (SQLite)", "Pas besoin de Redis"],
        ["Config / secrets", ".env + pydantic-settings", "Single-tenant, chmod 600"],
        ["Service prod", "systemd unit", "Natif Linux, pas de Docker au MVP"],
        ["Healthcheck", "FastAPI 127.0.0.1:8765", "Sondé par systemd timer"],
        ["Alertes", "Bot Telegram", "Gratuit, push immédiat"],
        ["Backup", "Cron → Backblaze B2 (chiffré age)", "Restore-tested"],
        ["Logs", "structlog JSON + journalctl", "Rotation 7 jours"],
    ], col_widths=(3 * cm, 5.5 * cm, 7.5 * cm)))

    s.append(Spacer(1, 6))
    s.append(warn(
        "Ne PAS introduire sans discussion explicite : Docker, Celery, Redis, Postgres, "
        "Kubernetes, ORM lourd. L'architecture est volontairement légère pour rester "
        "maintenable par une seule personne."
    ))
    s.append(PageBreak())

    # ---------- 5. CŒUR INTELLIGENT ----------
    s.append(p("5. Cœur intelligent — RAG + Style Daniel", H1))
    s.append(p("5.1 Bootstrap embeddings (one-shot)", H2))
    s.append(p(
        "Le script <font face='Courier'>scripts/bootstrap_embeddings.py</font> indexe les 1200 paires "
        "[mail entrant → réponse de Daniel] dans une table <font face='Courier'>pairs_vec</font> "
        "(extension sqlite-vec) à l'intérieur de chacune des 3 DB SQLite existantes. "
        "Les embeddings sont calculés via <font face='Courier'>multilingual-e5-large</font> "
        "(préfixe « passage: » conformément à e5)."
    ))
    s.append(p("5.2 Bootstrap personality (one-shot, reproductible)", H2))
    s.append(p(
        "Le script <font face='Courier'>scripts/extract_personality.py</font> échantillonne ~50 réponses "
        "représentatives, demande au LLM de produire un guide de style « personnalité Daniel » "
        "(ton, formules, longueur, signatures par marque, règles à respecter), et écrit le résultat "
        "dans <font face='Courier'>app/prompts/personality_daniel.txt</font>. À ré-exécuter quand le "
        "corpus s'enrichit."
    ))
    s.append(p("5.3 Génération à chaque demande client", H2))
    s.append(p(
        "Pour chaque mail classé <font face='Courier'>demande_client</font>, le générateur :"
    ))
    for item in [
        "détecte la langue du mail entrant (FR/NL/EN)",
        "calcule l'embedding query du mail (préfixe « query: »)",
        "récupère les <b>top 5</b> paires Q/R les plus similaires dans la DB de la boîte d'origine",
        "assemble un prompt système (personnalité Daniel + marque + langue obligatoire)",
        "ajoute le contexte RAG few-shot puis le mail à traiter",
        "appelle Kimi K2 via LiteLLM (fallback OpenRouter automatique en cas d'échec)",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "•"
    s.append(PageBreak())

    # ---------- 6. PROMPTS COMPLETS (COPIABLES) ----------
    s.append(p("6. Prompts complets (copier-coller)", H1))
    s.append(p(
        "Tous les prompts sont en monospace pour faciliter le copier-coller depuis ce PDF. "
        "Les versions sources vivent dans <font face='Courier'>app/prompts/</font> et dans les scripts."
    ))

    s.append(p("6.1 Prompt classification (app/prompts/classifier_prompt.txt)", H2))
    s.append(code(
        "Tu es un classifieur d'emails pour un cabinet de détectives privés.\n\n"
        "Catégories possibles (renvoie EXACTEMENT un seul mot, en minuscules) :\n"
        "- demande_client : nouveau prospect ou client existant qui pose une question,\n"
        "  demande un devis, donne suite à un dossier, sollicite un service.\n"
        "- facture : facture entrante d'un fournisseur, relance de paiement,\n"
        "  justificatif comptable.\n"
        "- newsletter : communication marketing, lettre d'info, promotion commerciale.\n"
        "- spam : message non sollicité, phishing, arnaque, contenu manifestement\n"
        "  frauduleux.\n"
        "- urgent : situation nécessitant une action immédiate (urgence client,\n"
        "  problème critique, deadline imminente explicite).\n"
        "- autre : tout le reste (échanges internes, confirmations automatiques,\n"
        "  notifications systèmes).\n\n"
        "Règles :\n"
        "- En cas de doute entre `demande_client` et `urgent`, choisis `urgent`\n"
        "  uniquement si l'urgence est explicite (mots comme \"urgent\", \"immédiat\",\n"
        "  \"asap\", \"aujourd'hui\", \"très important\").\n"
        "- Une demande de devis = `demande_client`.\n"
        "- Une notification automatique d'un service tiers (Stripe, Linkedin, Google,\n"
        "  etc.) = `autre`.\n\n"
        "Email à classer :\n"
        "De : {sender}\n"
        "Sujet : {subject}\n"
        "Corps : {body}\n\n"
        "Réponds par UN SEUL MOT parmi : demande_client, facture, newsletter, spam,\n"
        "urgent, autre.",
        PROMPT,
    ))

    s.append(p("6.2 Prompt personnalité Daniel (placeholder, à régénérer en S1)", H2))
    s.append(p(
        "Ce contenu est un placeholder. Le script <font face='Courier'>extract_personality.py</font> "
        "le remplacera par un guide de style généré à partir des 1200 paires."
    ))
    s.append(code(
        "Tu es Daniel Hurchon, détective privé belge dirigeant un cabinet d'enquêtes.\n"
        "Tu réponds personnellement à tes clients en français, néerlandais ou anglais\n"
        "selon la langue du message reçu.\n\n"
        "Style général :\n"
        "- Ton professionnel, direct, courtois mais pas obséquieux\n"
        "- Pas de jargon excessif, on parle à des clients qui n'ont jamais eu affaire\n"
        "  à un détective\n"
        "- Confidentialité toujours en avant (rappels discrets sur la discrétion)\n"
        "- Tutoiement rare, vouvoiement par défaut\n"
        "- Réponses concises, qui invitent à un appel ou un rendez-vous quand le sujet\n"
        "  est sensible\n\n"
        "Règles fixes :\n"
        "- TOUJOURS répondre dans la langue du mail entrant.\n"
        "- TOUJOURS terminer par une signature au nom du cabinet/marque indiqué.\n"
        "- JAMAIS citer de prix ferme par email pour des dossiers complexes — proposer\n"
        "  un appel.\n"
        "- JAMAIS prendre d'engagement légal/contractuel par email.\n"
        "- JAMAIS divulguer d'informations sur d'autres dossiers ou clients.\n\n"
        "Format attendu : corps de réponse uniquement, prêt à coller dans un email.\n"
        "Pas de \"Sujet:\", pas de markdown.",
        PROMPT,
    ))

    s.append(p("6.3 Prompt extraction personnalité (scripts/extract_personality.py)", H2))
    s.append(code(
        "Tu es un expert en analyse stylistique. Voici {n} réponses écrites par\n"
        "Daniel Hurchon, détective privé belge, à des clients en français/néerlandais/anglais.\n\n"
        "Produis un GUIDE DE STYLE concis et opérationnel (max 600 mots), destiné à être\n"
        "utilisé comme system prompt pour un LLM qui imitera Daniel. Le guide doit couvrir :\n\n"
        "1. Ton général (formel/informel, chaleureux/distant, etc.)\n"
        "2. Formules d'ouverture récurrentes (par langue)\n"
        "3. Formules de clôture récurrentes (par langue)\n"
        "4. Vocabulaire caractéristique et tics de langage\n"
        "5. Longueur typique des réponses (courte/moyenne/longue)\n"
        "6. Signature(s) utilisée(s) (par marque si différentes)\n"
        "7. Sujets sensibles : comment Daniel les évite ou les dévie (ex : prix,\n"
        "   engagements légaux)\n"
        "8. Règles à respecter ABSOLUMENT (toujours / jamais)\n\n"
        "Format de sortie : texte brut, prêt à être un system prompt. Pas de markdown.\n\n"
        "--- ÉCHANTILLON DES RÉPONSES ---\n"
        "{sample}",
        PROMPT,
    ))

    s.append(p("6.4 Prompt génération de brouillon (template assemblé)", H2))
    s.append(p(
        "Construit dynamiquement par <font face='Courier'>app/pipeline/generator.py</font> à chaque "
        "demande client. Voici le template complet :"
    ))
    s.append(code(
        "[SYSTEM]\n"
        "{contenu de personality_daniel.txt}\n"
        "\n"
        "Marque/boîte source : {brand}\n"
        "Langue de réponse OBLIGATOIRE : {language}\n"
        "\n"
        "[USER]\n"
        "Cas #1 (similarité 0.87, langue fr):\n"
        "--- Mail entrant ---\n"
        "{paire1.incoming}\n"
        "--- Réponse de Daniel ---\n"
        "{paire1.response}\n"
        "\n"
        "Cas #2 (similarité 0.81, langue fr):\n"
        "--- Mail entrant ---\n"
        "{paire2.incoming}\n"
        "--- Réponse de Daniel ---\n"
        "{paire2.response}\n"
        "\n"
        "(... top-5 paires)\n"
        "\n"
        "--- NOUVEAU MAIL À TRAITER ---\n"
        "De : {sender}\n"
        "Sujet : {subject}\n"
        "Corps :\n"
        "{body}\n"
        "\n"
        "Génère UN brouillon de réponse en {language}, signé au nom de {brand},\n"
        "dans le style de Daniel illustré par les cas ci-dessus. Renvoie\n"
        "UNIQUEMENT le corps du message, sans préambule, sans 'Sujet:', sans markdown.",
        PROMPT,
    ))
    s.append(PageBreak())

    # ---------- 7. CONFIGURATION ----------
    s.append(p("7. Configuration .env (copiable)", H1))
    s.append(p(
        "Copier <font face='Courier'>.env.example</font> vers <font face='Courier'>.env</font> "
        "et remplir les valeurs. Ne JAMAIS commiter le <font face='Courier'>.env</font> "
        "(le <font face='Courier'>.gitignore</font> le bloque)."
    ))
    s.append(code(
        "# --- Infomaniak IMAP (3 boîtes) ---\n"
        "IMAP_HOST=mail.infomaniak.com\n"
        "IMAP_PORT=993\n\n"
        "MAILBOX_1_NAME=detective_belgique\n"
        "MAILBOX_1_USER=contact@detectivebelgique.be\n"
        "MAILBOX_1_APP_PASSWORD=\n"
        "MAILBOX_1_BRAND=Detective Belgique\n"
        "MAILBOX_1_DEFAULT_LANG=fr\n\n"
        "MAILBOX_2_NAME=detective_belgium\n"
        "MAILBOX_2_USER=contact@detectivebelgium.com\n"
        "MAILBOX_2_APP_PASSWORD=\n"
        "MAILBOX_2_BRAND=Detective Belgium\n"
        "MAILBOX_2_DEFAULT_LANG=en\n\n"
        "MAILBOX_3_NAME=dpdh_investigations\n"
        "MAILBOX_3_USER=info@dpdhuinvestigations.be\n"
        "MAILBOX_3_APP_PASSWORD=\n"
        "MAILBOX_3_BRAND=DPDH Investigations\n"
        "MAILBOX_3_DEFAULT_LANG=fr\n\n"
        "# --- LLM (LiteLLM router) ---\n"
        "OLLAMA_PRO_API_KEY=\n"
        "OLLAMA_PRO_BASE_URL=https://ollama.com/api\n"
        "LLM_MODEL_DEFAULT=ollama_chat/kimi-k2\n"
        "OPENROUTER_API_KEY=\n"
        "LLM_MODEL_FALLBACK=openrouter/anthropic/claude-sonnet-4\n"
        "LLM_MODEL_CLASSIFIER=ollama_chat/kimi-k2\n\n"
        "# --- Resend ---\n"
        "RESEND_API_KEY=\n"
        "RESEND_FROM=agent@digitalhs.biz\n"
        "DRAFT_RECIPIENT=cdal@digitalhs.biz\n\n"
        "# --- RAG ---\n"
        "EMBEDDING_MODEL=intfloat/multilingual-e5-large\n"
        "RAG_TOP_K=5\n\n"
        "# --- Polling ---\n"
        "POLL_INTERVAL_SECONDS=300\n\n"
        "# --- Stockage ---\n"
        "DATA_DIR=./data\n"
        "DB_BOITE_1=./data/boite1.sqlite\n"
        "DB_BOITE_2=./data/boite2.sqlite\n"
        "DB_BOITE_3=./data/boite3.sqlite\n"
        "DB_AGENT_STATE=./data/agent_state.db\n\n"
        "# --- Healthcheck ---\n"
        "HEALTHCHECK_HOST=127.0.0.1\n"
        "HEALTHCHECK_PORT=8765\n\n"
        "# --- Telegram alertes (optionnel V1) ---\n"
        "TELEGRAM_BOT_TOKEN=\n"
        "TELEGRAM_CHAT_ID=\n\n"
        "# --- Logs ---\n"
        "LOG_LEVEL=INFO"
    ))
    s.append(PageBreak())

    # ---------- 8. LAYOUT FILESYSTEM ----------
    s.append(p("8. Layout filesystem", H1))
    s.append(p("8.1 En production (KVM8)", H2))
    s.append(code(
        "/opt/detective-agent/\n"
        "├── app/\n"
        "│   ├── main.py                 # entrypoint asyncio\n"
        "│   ├── config.py               # pydantic-settings\n"
        "│   ├── healthcheck.py          # FastAPI /health\n"
        "│   ├── workers/imap_poller.py  # 1 task asyncio par boîte\n"
        "│   ├── pipeline/\n"
        "│   │   ├── prefilter.py        # règles headers/expéditeurs\n"
        "│   │   ├── classifier.py       # LLM 6 catégories\n"
        "│   │   ├── language.py         # fasttext FR/NL/EN\n"
        "│   │   ├── rag.py              # embed + retrieve sqlite-vec\n"
        "│   │   └── generator.py        # assemblage prompt + LLM call\n"
        "│   ├── delivery/resend_notifier.py\n"
        "│   ├── llm/router.py           # wrapper LiteLLM + fallback\n"
        "│   └── prompts/\n"
        "│       ├── classifier_prompt.txt\n"
        "│       └── personality_daniel.txt\n"
        "├── data/\n"
        "│   ├── boite1.sqlite           # DB existantes anonymisées\n"
        "│   ├── boite2.sqlite\n"
        "│   ├── boite3.sqlite\n"
        "│   └── agent_state.db          # queue, logs, télémétrie\n"
        "├── scripts/\n"
        "│   ├── bootstrap_embeddings.py\n"
        "│   ├── extract_personality.py\n"
        "│   └── build_bible_pdf.py\n"
        "├── deploy/detective-agent.service\n"
        "├── .env                        # chmod 600\n"
        "├── venv/\n"
        "└── logs/"
    ))
    s.append(p("8.2 En développement local (Mac CDAL)", H2))
    s.append(p(
        "Identique, à la racine <font face='Courier'>/Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE/</font>."
    ))
    s.append(PageBreak())

    # ---------- 9. WORKFLOW DE LIVRAISON ----------
    s.append(p("9. Workflow de livraison MVP", H1))
    s.append(p(
        "Le brouillon est envoyé par email à <font face='Courier'>cdal@digitalhs.biz</font> via Resend, "
        "formaté comme suit :"
    ))
    s.append(p("• <b>Sujet</b> : <font face='Courier'>[AGENT][${marque}] ${sujet du mail original}</font>", BULLET))
    s[-1].bulletText = ""
    s.append(p("• <b>Corps HTML</b> :", BULLET))
    s[-1].bulletText = ""
    for item in [
        "Métadonnées : boîte source, expéditeur, date reçue, langue détectée, modèle utilisé",
        "Brouillon proposé (encadré, copiable, monospace)",
        "Mail original intégral (encadré jaune)",
        "Top 3 cas RAG utilisés (extraits + scores similarité)",
    ]:
        s.append(p(item, ParagraphStyle("inner", parent=BULLET, leftIndent=28, bulletIndent=14)))
        s[-1].bulletText = "–"

    s.append(p(
        "CDAL relit, ajuste si besoin, transmet à Daniel via forward standard. Quand la qualité est "
        "stabilisée (mesurée sur 2-4 semaines), bascule en V2 vers dépôt IMAP direct dans le dossier "
        "<font face='Courier'>Drafts</font> natif de Daniel."
    ))

    # ---------- 10. SUPERVISION 24/7 ----------
    s.append(p("10. Supervision 24/7", H1))
    for item in [
        "<b>systemd unit</b> avec <font face='Courier'>Restart=always</font>, <font face='Courier'>RestartSec=10</font>",
        "<b>Healthcheck</b> : endpoint <font face='Courier'>/health</font> (FastAPI sur 127.0.0.1:8765) "
        "renvoie OK si toutes les connexions IMAP actives + dernier cycle &lt; 10 min",
        "<b>systemd timer</b> sonde <font face='Courier'>/health</font> chaque minute, déclenche restart si KO",
        "<b>Bot Telegram</b> envoie alertes pour : agent down, IMAP timeout &gt; 3 tentatives consécutives, "
        "échec génération &gt; 5/heure, taux d'erreur LLM &gt; 10%",
        "<b>Logs structurés JSON</b> dans journalctl, rotation 7 jours",
        "<b>Tableau de bord léger</b> (V1.5) : page HTML statique générée par cron, accessible via SSH tunnel",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "•"
    s.append(PageBreak())

    # ---------- 11. SÉCURITÉ ----------
    s.append(p("11. Sécurité & garde-fous", H1))
    s.append(p("11.1 Secrets et accès", H2))
    for item in [
        "3 app passwords Infomaniak + clé Resend + clés Ollama/OpenRouter dans <font face='Courier'>.env</font> "
        "(chmod 600, propriétaire <font face='Courier'>detective-agent</font> en prod)",
        "TLS partout : IMAPS 993, SMTP 587 STARTTLS, HTTPS pour API LLM",
        "Healthcheck bind <font face='Courier'>127.0.0.1</font> uniquement, aucune API exposée publiquement",
        "Backups chiffrés (age) avant push vers Backblaze",
        "App passwords séparés permettent révocation individuelle par boîte",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "•"

    s.append(p("11.2 RGPD et confidentialité", H2))
    s.append(warn(
        "Pas de log du contenu intégral des mails en clair — uniquement IDs et métadonnées. "
        "Pour debug, ajouter un flag explicite LOG_MAIL_BODY=true."
    ))
    s.append(warn(
        "Les 3 DB SQLite anonymisées restent sensibles : ne jamais commit dans git, ne jamais "
        "uploader en clair vers un service tiers. Le .gitignore les bloque déjà."
    ))
    s.append(warn(
        "Ne jamais écrire dans les vraies boîtes Infomaniak en dev. Mode --dry-run obligatoire "
        "tant que la calibration n'est pas validée."
    ))

    # ---------- 12. COÛTS ----------
    s.append(p("12. Coûts mensuels", H1))
    s.append(table_header([
        ["Poste", "Coût mensuel"],
        ["VPS Hostinger KVM8", "déjà payé"],
        ["Infomaniak (3 boîtes)", "déjà payé"],
        ["Ollama Pro (Kimi K2)", "20 €"],
        ["OpenRouter (fallback ponctuel)", "< 5 €"],
        ["Resend (free tier 3000 mails/mois)", "0 €"],
        ["Backblaze B2 (~5 Go)", "< 1 €"],
        ["Bot Telegram", "0 €"],
        ["Total estimé", "≈ 25-30 €"],
    ], col_widths=(10 * cm, 6 * cm)))
    s.append(PageBreak())

    # ---------- 13. ROADMAP ----------
    s.append(p("13. Roadmap (S1 → V3)", H1))
    s.append(p(
        "État courant : Phase 0 (brainstorm + scaffolding) terminée. S1 démarre dès que les "
        "pré-requis bloquants côté CDAL sont fournis (DB SQLite, schéma, .env rempli)."
    ))

    s.append(p("Phase 0 — Brainstorm & cadrage (terminée 2026-05-13)", H2))
    for item in [
        "Spec technique figée (docs/SPEC.md)",
        "Choix LLM : Kimi K2 via Ollama Pro + LiteLLM",
        "Choix vector store : sqlite-vec",
        "Choix livraison MVP : Resend → cdal@digitalhs.biz",
        "Scaffolding code complet en place",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "✓"

    s.append(p("S1 — Infra & data (en cours)", H2))
    s.append(p("<b>Pré-requis bloquants</b> :", BODY))
    for item in [
        "Déposer les 3 fichiers .sqlite anonymisés dans data/",
        "Partager le schéma de chaque DB (sqlite3 ... \".schema\")",
        "Remplir .env avec : 3 app passwords Infomaniak, OLLAMA_PRO_API_KEY, RESEND_API_KEY",
        "Vérifier le domaine Resend agent@digitalhs.biz",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"
    s.append(p("<b>Tâches code</b> :", BODY))
    for item in [
        "Créer venv + installer deps (pip install -e .[dev])",
        "Smoke test LLM : appel Kimi K2 via LiteLLM",
        "Smoke test embeddings : charger e5-large, encoder, vérifier dim",
        "Smoke test sqlite-vec : indexer 5 vecteurs jouets, retrieve top-1",
        "Adapter scripts/bootstrap_embeddings.py au schéma réel",
        "Adapter scripts/extract_personality.py au schéma réel",
        "Exécuter bootstrap_embeddings → vérifier pairs_vec peuplé",
        "Exécuter extract_personality → relire et valider personality_daniel.txt",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"

    s.append(p("S2 — Pipeline ingestion IMAP", H2))
    for item in [
        "Implémenter imap_poller._poll_once (aioimaplib)",
        "Mode --dry-run sans flag",
        "Reconnexion automatique IMAP",
        "Tests sur 1 boîte d'abord, puis les 3",
        "Persister classifications dans agent_state.db",
        "Tests unitaires : mock IMAP + 5 mails-fixtures",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"

    s.append(p("S3 — Cœur intelligent : RAG + génération", H2))
    for item in [
        "Brancher language.detect_language sur les mails entrants",
        "Brancher rag.retrieve sur la vraie DB",
        "Brancher generator.generate_draft end-to-end",
        "Brancher delivery.resend_notifier.notify_draft",
        "Calibration qualité sur 50 mails réels",
        "Vérification multilingue FR/NL/EN",
        "Vérification signatures par marque",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"

    s.append(p("S4 — Production sur KVM8 + supervision", H2))
    for item in [
        "Setup KVM8 : user dédié, Python 3.11, venv, /opt/detective-agent/",
        "Installer .env prod (chmod 600)",
        "Installer systemd unit, systemctl enable --now",
        "Bot Telegram + chat_id",
        "systemd timer healthcheck",
        "Cron backup quotidien Backblaze (chiffré age)",
        "Procédure restore documentée et testée",
        "Documentation opérationnelle (restart, restore, ajout boîte)",
        "Lancement officiel + monitoring 1 semaine",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"

    s.append(p("V2 — Bascule Drafts IMAP + feedback loop", H2))
    for item in [
        "Module delivery/imap_drafts.py (IMAP APPEND dans Drafts)",
        "Switch config DELIVERY_MODE=resend|imap_drafts",
        "Capture feedback (diff brouillon vs envoyé)",
        "Tableau de bord taux d'acceptation + distance d'édition",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"

    s.append(p("V3 — Extensions", H2))
    for item in [
        "Module factures (extraction structurée + tâche compta)",
        "Bot WhatsApp client (Twilio / WhatsApp Business API)",
        "Dashboard web supervision",
        "Suppression mails > 28 jours (rétention RGPD)",
        "Architecture multi-sub-agents avec LLM différencié",
    ]:
        s.append(p(item, BULLET))
        s[-1].bulletText = "☐"
    s.append(PageBreak())

    # ---------- 14. VÉRIFICATION END-TO-END ----------
    s.append(p("14. Vérification end-to-end (10 tests avant livraison MVP)", H1))
    tests = [
        ("IMAP", "Injecter mail test depuis compte externe vers chacune des 3 boîtes → "
                  "détection < 5 min + tag $AgentProcessed posé"),
        ("Classification", "1 mail de chaque catégorie (newsletter, facture, demande client, spam) "
                           "→ classification correcte dans logs"),
        ("RAG", "Mail demande client similaire à un cas historique → top-5 contient le cas attendu"),
        ("Génération", "Brouillon respecte langue du mail entrant + signature de la bonne marque"),
        ("Livraison", "Email reçu sur cdal@digitalhs.biz avec format complet (métadonnées + brouillon "
                       "+ original + cas RAG)"),
        ("Multilingue", "3 mails identiques FR/NL/EN → 3 brouillons cohérents chacun dans sa langue"),
        ("Robustesse", "Kill brutal du process → systemd redémarre dans 10s, reprend sans doublon "
                        "(idempotence flag IMAP)"),
        ("Supervision", "Couper IMAP volontairement (firewall) → alerte Telegram reçue"),
        ("Backup", "Exécuter restore sur copie → DB lisibles"),
        ("Charge", "Injecter 100 mails simultanés → tous traités sans crash, latence moyenne < 30s/mail"),
    ]
    s.append(table_header([
        ["#", "Test", "Critère de succès"],
        *[[str(i + 1), t[0], t[1]] for i, t in enumerate(tests)],
    ], col_widths=(0.8 * cm, 3 * cm, 12.2 * cm)))
    s.append(PageBreak())

    # ---------- 15. SETUP & DÉPLOIEMENT ----------
    s.append(p("15. Setup local & déploiement", H1))
    s.append(p("15.1 Setup local (Mac CDAL)", H2))
    s.append(code(
        "cd /Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE\n"
        "python3 -m venv venv\n"
        "source venv/bin/activate\n"
        "pip install -e \".[dev]\"\n"
        "cp .env.example .env\n"
        "# éditer .env avec les vraies clés\n\n"
        "# Bootstrap one-shot (S1, après que les DB sont en place)\n"
        "python -m scripts.bootstrap_embeddings\n"
        "python -m scripts.extract_personality\n\n"
        "# Lancer l'agent\n"
        "python -m app.main"
    ))

    s.append(p("15.2 Déploiement KVM8 (S4)", H2))
    s.append(code(
        "# Sur le VPS, en root\n"
        "useradd -r -s /usr/sbin/nologin -d /opt/detective-agent detective-agent\n"
        "mkdir -p /opt/detective-agent\n"
        "chown -R detective-agent: /opt/detective-agent\n\n"
        "# Cloner / rsync le code\n"
        "rsync -av --exclude=venv --exclude=.env \\\n"
        "  /local/DETECTIVE_BE/ root@vps:/opt/detective-agent/\n\n"
        "# Sur le VPS, créer venv et installer\n"
        "sudo -u detective-agent python3.11 -m venv /opt/detective-agent/venv\n"
        "sudo -u detective-agent /opt/detective-agent/venv/bin/pip install -e \\\n"
        "  /opt/detective-agent\n\n"
        "# Copier .env (chmod 600)\n"
        "scp .env.prod root@vps:/opt/detective-agent/.env\n"
        "chmod 600 /opt/detective-agent/.env\n"
        "chown detective-agent: /opt/detective-agent/.env\n\n"
        "# systemd\n"
        "cp /opt/detective-agent/deploy/detective-agent.service \\\n"
        "  /etc/systemd/system/\n"
        "systemctl daemon-reload\n"
        "systemctl enable --now detective-agent\n"
        "systemctl status detective-agent\n"
        "journalctl -u detective-agent -f"
    ))
    s.append(PageBreak())

    # ---------- 16. CHEATSHEET COMMANDES ----------
    s.append(p("16. Cheatsheet commandes utiles", H1))
    s.append(p("16.1 Développement", H2))
    s.append(code(
        "# Lancer l'agent\n"
        "python -m app.main\n\n"
        "# Tests\n"
        "pytest\n"
        "pytest -k test_classifier  # un sous-ensemble\n\n"
        "# Lint / format\n"
        "ruff check .\n"
        "ruff format .\n\n"
        "# Inspecter le schéma d'une DB\n"
        "sqlite3 data/boite1.sqlite \".schema\"\n"
        "sqlite3 data/boite1.sqlite \".tables\"\n\n"
        "# Inspecter sqlite-vec\n"
        "sqlite3 data/boite1.sqlite \"SELECT count(*) FROM pairs_vec;\"\n\n"
        "# Régénérer la bible PDF\n"
        "python scripts/build_bible_pdf.py"
    ))

    s.append(p("16.2 Production (sur KVM8)", H2))
    s.append(code(
        "# Status\n"
        "systemctl status detective-agent\n\n"
        "# Logs en temps réel\n"
        "journalctl -u detective-agent -f\n\n"
        "# Logs des dernières 24h\n"
        "journalctl -u detective-agent --since '24 hours ago'\n\n"
        "# Restart\n"
        "systemctl restart detective-agent\n\n"
        "# Healthcheck manuel\n"
        "curl -s http://127.0.0.1:8765/health | jq\n\n"
        "# Backup manuel\n"
        "/opt/detective-agent/scripts/backup.sh\n\n"
        "# Tester un restore\n"
        "/opt/detective-agent/scripts/restore.sh /tmp/restore-test"
    ))
    s.append(PageBreak())

    # ---------- 17. GLOSSAIRE ----------
    s.append(p("17. Glossaire", H1))
    glossary = [
        ("App password", "Mot de passe applicatif Infomaniak distinct du mot de passe principal, "
                          "permet une révocation par boîte sans tout casser."),
        ("$AgentProcessed", "Flag IMAP custom posé sur les mails entrants après traitement par "
                            "l'agent, pour garantir l'idempotence (un mail = un brouillon)."),
        ("e5-large", "Modèle d'embeddings multilingue intfloat/multilingual-e5-large, gratuit, "
                     "tourne en CPU. Convention : préfixer les passages par « passage: » et les "
                     "requêtes par « query: »."),
        ("Few-shot RAG", "Technique de génération qui injecte 3-5 exemples concrets (paires Q/R "
                          "retrouvées par similarité) directement dans le prompt pour faire imiter "
                          "un style sans fine-tuning."),
        ("Kimi K2", "Modèle de langue de Moonshot AI, accessible via Ollama Pro (abonnement "
                    "20€/mois). Performances comparables à Claude / GPT-4 sur la rédaction multilingue."),
        ("LiteLLM", "Bibliothèque Python qui expose une API unique compatible OpenAI mais route "
                    "vers n'importe quel provider (Ollama, OpenRouter, Anthropic, etc.)."),
        ("OpenRouter", "Marketplace de LLMs avec une seule API, utilisé ici en fallback et pour "
                       "tester d'autres modèles à la demande."),
        ("Resend", "Service d'envoi d'emails transactionnels par API HTTP, free tier 3000 mails/mois."),
        ("sqlite-vec", "Extension SQLite récente qui ajoute des opérateurs vectoriels (recherche "
                       "par similarité) à n'importe quelle DB SQLite. Évite d'avoir un service "
                       "vectoriel séparé."),
        ("structlog", "Bibliothèque de logs structurés JSON (vs texte plat), parsable par les "
                       "outils de monitoring."),
        ("systemd unit", "Fichier de configuration qui décrit un service à long terme géré par "
                          "systemd : lancement, supervision, redémarrage automatique."),
        ("Hostinger KVM8", "Plan de VPS Hostinger : 8 vCPU, 32 Go RAM, 400 Go NVMe."),
    ]
    s.append(table_header([
        ["Terme", "Définition"],
        *[[t, d] for t, d in glossary],
    ], col_widths=(4 * cm, 12 * cm)))

    s.append(Spacer(1, 1 * cm))
    s.append(p(
        "<i>Fin de la bible. Pour toute question, ouvrir une session Claude Code dans le dossier "
        "DETECTIVE_BE — il chargera CLAUDE.md et saura quoi faire.</i>",
        ParagraphStyle("end", parent=BODY, alignment=TA_CENTER, textColor=MUTED, fontSize=9),
    ))

    return s


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = BibleDoc(str(OUTPUT))
    story = build_story()
    doc.multiBuild(story)
    print(f"PDF généré : {OUTPUT}")


if __name__ == "__main__":
    main()
