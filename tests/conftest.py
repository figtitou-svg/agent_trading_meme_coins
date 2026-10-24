"""
tests/conftest.py — Garde-fou global : aucun test ne doit jamais faire un
vrai appel réseau vers l'API Telegram.

Incident du 30/07/2026 : plusieurs tests appelaient main.evaluate_candidates()
/ refresh_positions() (qui déclenchent des alertes réelles depuis l'ajout des
notifs par trade) sans mocker le notifier. Exécutés sur le serveur de
production (vraies credentials dans .env) lors des vérifications
post-déploiement, ça a envoyé de VRAIES notifications Telegram avec des
données de test (MintC, MintG, MintT, MintN...) — repéré par l'utilisateur
qui recevait un spam d'"achats virtuels" sur des tokens inexistants.

Cette fixture autouse rend la classe de bug structurellement impossible :
`send_telegram` est le point d'entrée unique de tous les envois Telegram
(recap, alertes d'achat/vente) — la bloquer ici bloque tout, peu importe le
chemin de code emprunté. Un test qui a besoin de vérifier le contenu d'un
message doit explicitement re-mocker `send_telegram` lui-même (son propre
monkeypatch.setattr, exécuté après celui-ci, prend le dessus sans conflit).
"""

import pytest

from watcher import notifier


@pytest.fixture(autouse=True)
def _block_real_telegram_calls(monkeypatch):
    def _fail_on_unmocked_call(message):
        raise AssertionError(
            "notifier.send_telegram() appelé sans mock explicite dans ce "
            "test — un vrai message Telegram aurait été envoyé. Ajoutez "
            "monkeypatch.setattr(notifier, 'send_telegram', ...) (ou mockez "
            "une fonction de plus haut niveau comme alert_virtual_buy)."
        )
    monkeypatch.setattr(notifier, "send_telegram", _fail_on_unmocked_call)
