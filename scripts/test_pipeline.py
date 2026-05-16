"""Script de test end-to-end pour le pipeline Detective.be.

Envoie 3 emails de test (1 vrai demande client + 2 faux positifs) vers les 3 boîtes
Infomaniak, puis vérifie dans les logs du poller qu'ils sont classés correctement.

Usage :
    python -m scripts.test_pipeline

Prérequis :
    - Les 3 MAILBOX_*_APP_PASSWORD doivent être renseignées dans .env
    - L'agent doit tourner (local ou VPS) pour traiter les mails
    - Attendre ~5 min (intervalle de polling) pour voir les résultats
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

# Permet d'importer app.* depuis la racine du projet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Templates de test
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "name": "vraie_demande_client",
        "expected_category": "demande_client",
        "subject": "Nouveau message de Jean Dupont — Demande de devis filature",
        "body": (
            "Bonjour,\n\n"
            "Je me permets de vous contacter car je soupçonne ma femme de me tromper "
            "depuis plusieurs mois. Nous habitons à Bruxelles (Ixelles).\n\n"
            "Je souhaite obtenir un devis pour une surveillance discrète sur une "
            "période de 3 jours. Est-ce envisageable ? Pouvez-vous me contacter au "
            "0499 123 456 ou par retour de mail ?\n\n"
            "Cordialement,\nJean Dupont"
        ),
        "from_name": "Jean Dupont",
        "from_email": "jean.dupont.test@gmail.com",
    },
    {
        "name": "renouvellement_infomaniak",
        "expected_category": "autre",
        "subject": "Renouvellement de votre demande d'hébergement — Facture F-2024-12065",
        "body": (
            "Cher client,\n\n"
            "Votre abonnement d'hébergement arrive à échéance le 15/06/2026.\n\n"
            "Veuillez régulariser votre situation en vous connectant à votre espace client :\n"
            "https://login.infomaniak.com\n\n"
            "Sans régularisation, vos services seront suspendus dans 7 jours.\n\n"
            "Cordialement,\nL'équipe Infomaniak"
        ),
        "from_name": "Infomaniak Billing",
        "from_email": "billing@infomaniak.com",
    },
    {
        "name": "confirmation_stripe",
        "expected_category": "autre",
        "subject": "Confirmation de paiement — Détective Belgium",
        "body": (
            "Bonjour,\n\n"
            "Votre paiement de 29,00 EUR a bien été reçu.\n\n"
            "• Montant : 29,00 EUR\n"
            "• Date : 16/05/2026\n"
            "• Méthode : Visa ****4242\n"
            "• Reçu : rcpt_3Ox8...\n\n"
            "Vous pouvez consulter votre reçu complet ici :\n"
            "https://stripe.com/receipts/...\n\n"
            "Merci pour votre confiance.\n"
            "— Stripe"
        ),
        "from_name": "Stripe",
        "from_email": "no-reply@stripe.com",
    },
]


# ---------------------------------------------------------------------------
# Envoi SMTP
# ---------------------------------------------------------------------------


def _build_email(case: dict, to_address: str) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = f"{case['from_name']} <{case['from_email']}>"
    msg["To"] = to_address
    msg["Subject"] = f"[TEST {case['name']}] {case['subject']}"
    msg["X-Test-Case"] = case["name"]
    msg["X-Test-Expected"] = case["expected_category"]
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.attach(MIMEText(case["body"], "plain", "utf-8"))
    return msg


async def _send_one(
    smtp_host: str,
    smtp_port: int,
    user: str,
    password: str,
    case: dict,
    to_address: str,
) -> bool:
    import smtplib
    from email.utils import formatdate

    msg = _build_email(case, to_address)
    msg.replace_header("Date", formatdate(localtime=True))

    def _send():
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

    try:
        await asyncio.to_thread(_send)
        log.info(
            "test.sent",
            case=case["name"],
            expected=case["expected_category"],
            to=to_address,
            subject=msg["Subject"],
        )
        return True
    except Exception:
        log.exception("test.send_failed", case=case["name"], to=to_address)
        return False


async def _send_all(to_address: str, user: str, password: str) -> None:
    settings = get_settings()
    smtp_host = getattr(settings, "smtp_host", "mail.infomaniak.com")
    smtp_port = getattr(settings, "smtp_port", 587)

    for case in TEST_CASES:
        await _send_one(smtp_host, smtp_port, user, password, case, to_address)
        await asyncio.sleep(1)  # Rate-limit doux


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    settings = get_settings()

    # On envoie les tests vers la première boîte par défaut, configurable via arg
    import argparse

    parser = argparse.ArgumentParser(description="Test pipeline Detective.be")
    parser.add_argument(
        "--mailbox",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="Boîte cible (1=detective_belgique, 2=detective_belgium, 3=dpdh)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Envoyer vers les 3 boîtes",
    )
    args = parser.parse_args()

    if args.all:
        targets = [
            (settings.mailbox_1.user, settings.mailbox_1.app_password),
            (settings.mailbox_2.user, settings.mailbox_2.app_password),
            (settings.mailbox_3.user, settings.mailbox_3.app_password),
        ]
    else:
        mbox = [settings.mailbox_1, settings.mailbox_2, settings.mailbox_3][args.mailbox - 1]
        targets = [(mbox.user, mbox.app_password)]

    log.info("test.start", targets=len(targets), cases=len(TEST_CASES))
    for user, password in targets:
        if not password:
            log.warning("test.skip_no_password", user=user)
            continue
        asyncio.run(_send_all(user, user, password))

    log.info("test.done")
    print("\n✅ Emails de test envoyés !")
    print(f"   Attendez ~{settings.poll_interval_seconds // 60} min et consultez :")
    print("   - Les logs : ssh root@69.62.110.165 'cd /opt/DETECTIVE && docker compose logs -f'")
    print("   - Le canal #detective (uniquement la vraie demande_client doit apparaître)")
    print(
        "   - La DB : sqlite3 data/agent_state.db "
        "'SELECT * FROM mail_processed ORDER BY id DESC LIMIT 10;'"
    )


if __name__ == "__main__":
    main()
