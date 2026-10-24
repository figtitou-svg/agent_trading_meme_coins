import inspect

import pytest

from watcher import config
from watcher import notifier


def test_build_recap_message_includes_all_figures():
    msg = notifier.build_recap_message(n_watched=8, n_bought=3, n_wins=1, n_losses=1, pnl_eur=4.25)
    assert "8" in msg and "3" in msg
    assert "1 gain" in msg or "gains" in msg
    assert "+4.25" in msg or "4.25" in msg


def test_build_recap_message_handles_zero_activity():
    msg = notifier.build_recap_message(n_watched=0, n_bought=0, n_wins=0, n_losses=0, pnl_eur=0.0)
    assert "0 sous surveillance" in msg
    assert "0 achetés" in msg
    assert "0 gain" in msg
    assert "0 perte" in msg
    assert "+0.00" in msg


def test_build_recap_message_formats_negative_pnl_correctly():
    msg = notifier.build_recap_message(n_watched=2, n_bought=1, n_wins=0, n_losses=1, pnl_eur=-3.5)
    assert "-3.50" in msg
    assert "+-3.50" not in msg  # pas de double signe


def test_build_recap_message_includes_per_source_breakdown():
    msg = notifier.build_recap_message(
        n_watched=8, n_bought=3, n_wins=1, n_losses=1, pnl_eur=4.25,
        by_source={
            "telegram": {"watched": 5, "bought": 2},
            "onchain": {"watched": 3, "bought": 1},
        },
    )
    assert "Telegram" in msg and "5" in msg and "2" in msg
    assert "On-chain" in msg and "3" in msg and "1" in msg


def test_build_recap_message_omits_breakdown_section_when_source_data_absent():
    msg = notifier.build_recap_message(n_watched=0, n_bought=0, n_wins=0, n_losses=0, pnl_eur=0.0)
    assert "Telegram" not in msg


def test_build_recap_message_survives_unexpected_by_source_shape():
    """`by_source` peut manquer une clé attendue, ou en contenir une inconnue
    (donnée corrompue / évolution future du schéma) : ne doit jamais lever."""
    msg = notifier.build_recap_message(
        n_watched=1, n_bought=1, n_wins=0, n_losses=0, pnl_eur=0.0,
        by_source={"onchain": {"watched": 1, "bought": 0}},
    )
    assert "Telegram" in msg and "On-chain" in msg

    msg2 = notifier.build_recap_message(
        n_watched=1, n_bought=1, n_wins=0, n_losses=0, pnl_eur=0.0,
        by_source={"discord": {"watched": 9, "bought": 9}},
    )
    assert "Telegram" in msg2 and "On-chain" in msg2


def test_alert_traction_confirmed_removed():
    assert not hasattr(notifier, "alert_traction_confirmed")


def test_alert_position_closed_removed():
    assert not hasattr(notifier, "alert_position_closed")


def test_alert_real_purchase_untouched():
    assert hasattr(notifier, "alert_real_purchase")
    sig = inspect.signature(notifier.alert_real_purchase)
    assert list(sig.parameters) == ["token_address", "name", "symbol", "pair_address"]


def test_recap_enabled_true_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(tmp_path / "does_not_exist.json"))
    assert notifier.recap_enabled() is True


def test_recap_enabled_true_when_invalid_json(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is True


def test_recap_enabled_true_when_not_a_dict(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is True


def test_recap_enabled_true_when_invalid_utf8(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff\xfe\x00\x01invalid")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is True


def test_recap_enabled_false_when_disabled(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"memecoin_recap_enabled": false}', encoding="utf-8")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is False


def test_recap_enabled_true_when_enabled_or_key_absent(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"memecoin_recap_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is True

    path.write_text('{"forex_mode": "detailed"}', encoding="utf-8")
    assert notifier.recap_enabled() is True


def test_trade_alerts_enabled_true_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(tmp_path / "does_not_exist.json"))
    assert notifier.trade_alerts_enabled() is True


def test_trade_alerts_enabled_false_when_disabled(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"memecoin_trade_alerts_enabled": false}', encoding="utf-8")
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.trade_alerts_enabled() is False


def test_trade_alerts_enabled_independent_from_recap_enabled(tmp_path, monkeypatch):
    """Les deux réglages doivent pouvoir être activés/désactivés
    indépendamment — pas de couplage caché entre récap et alertes par trade."""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"memecoin_recap_enabled": false, "memecoin_trade_alerts_enabled": true}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "NOTIFICATION_SETTINGS_PATH", str(path))
    assert notifier.recap_enabled() is False
    assert notifier.trade_alerts_enabled() is True


def test_alert_virtual_buy_sends_message_with_amount_and_source(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: True)
    notifier.alert_virtual_buy(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        "PoolAddr123", 1.0, "telegram",
    )
    assert len(sent) == 1
    assert "DWC" in sent[0]
    assert "1.00" in sent[0]
    assert "Telegram" in sent[0] or "salon Telegram" in sent[0]
    assert "PoolAddr123" in sent[0]


def test_alert_virtual_buy_shows_discord_source_label(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: True)
    notifier.alert_virtual_buy(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        "PoolAddr123", 1.0, "discord",
    )
    assert len(sent) == 1
    assert "salon Discord" in sent[0]


def test_alert_virtual_buy_does_nothing_when_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: False)
    notifier.alert_virtual_buy(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        "PoolAddr123", 1.0, "telegram",
    )
    assert sent == []


def test_alert_virtual_sell_shows_profit_and_pnl(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: True)
    notifier.alert_virtual_sell(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        2.5, 1.0, True, "onchain",
    )
    assert len(sent) == 1
    assert "Take Profit" in sent[0]
    assert "x2.50" in sent[0]
    assert "+1.50" in sent[0]  # P&L = 1.0 * (2.5 - 1) = 1.50€
    assert "✅" in sent[0]


def test_alert_virtual_sell_shows_loss_and_stop_loss_label(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: True)
    notifier.alert_virtual_sell(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        0.5, 1.0, False, "momentum",
    )
    assert len(sent) == 1
    assert "Stop Loss" in sent[0]
    assert "-0.50" in sent[0]  # P&L = 1.0 * (0.5 - 1) = -0.50€
    assert "❌" in sent[0]


def test_alert_virtual_sell_does_nothing_when_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "trade_alerts_enabled", lambda: False)
    notifier.alert_virtual_sell(
        "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC",
        2.5, 1.0, True, "onchain",
    )
    assert sent == []


def test_compute_stop_multiplier_bands():
    assert notifier.compute_stop_multiplier(1.0) == 0.75
    assert notifier.compute_stop_multiplier(1.99) == 0.75
    assert notifier.compute_stop_multiplier(2.0) == 1.2
    assert notifier.compute_stop_multiplier(2.99) == 1.2
    assert notifier.compute_stop_multiplier(3.0) == 2.0
    assert notifier.compute_stop_multiplier(4.99) == 2.0


def test_compute_stop_multiplier_trails_proportionally_above_peak_5():
    """Au-delà de x5, le stop suit le peak à 87,5% (tolérance de repli
    12,5%) au lieu d'un plafond fixe — décision du 01/08/2026 pour ne pas
    redonner trop de gain sur un très gros peak."""
    assert notifier.compute_stop_multiplier(5.0) == pytest.approx(4.375)
    assert notifier.compute_stop_multiplier(16.0) == pytest.approx(14.0)
    assert notifier.compute_stop_multiplier(100.0) == pytest.approx(87.5)


def test_build_recap_message_includes_portfolio_line_when_provided():
    msg = notifier.build_recap_message(
        n_watched=1, n_bought=1, n_wins=0, n_losses=0, pnl_eur=0.0,
        portfolio={"invested_eur": 10.0, "current_value_eur": 12.5},
    )
    assert "10.00" in msg
    assert "12.50" in msg
    assert "+25.0%" in msg


def test_build_recap_message_omits_portfolio_line_when_absent():
    msg = notifier.build_recap_message(n_watched=0, n_bought=0, n_wins=0, n_losses=0, pnl_eur=0.0)
    assert "Patrimoine" not in msg


def test_build_recap_message_portfolio_line_shows_negative_pct():
    msg = notifier.build_recap_message(
        n_watched=1, n_bought=1, n_wins=0, n_losses=0, pnl_eur=0.0,
        portfolio={"invested_eur": 10.0, "current_value_eur": 6.0},
    )
    assert "-40.0%" in msg


def test_build_recap_message_portfolio_line_omits_pct_when_nothing_invested():
    msg = notifier.build_recap_message(
        n_watched=0, n_bought=0, n_wins=0, n_losses=0, pnl_eur=0.0,
        portfolio={"invested_eur": 0, "current_value_eur": 0},
    )
    assert "Patrimoine" in msg
    assert "%" not in msg


def test_build_ondemand_recap_message_includes_portfolio_line_when_provided():
    msg = notifier.build_ondemand_recap_message(
        n_watched=1, active_positions=[], n_wins=0, n_losses=0, pnl_eur=0.0,
        portfolio={"invested_eur": 5.0, "current_value_eur": 5.0},
    )
    assert "5.00" in msg
    assert "Patrimoine" in msg


def test_build_ondemand_recap_message_no_open_positions():
    msg = notifier.build_ondemand_recap_message(
        n_watched=5, active_positions=[], n_wins=0, n_losses=0, pnl_eur=0.0,
    )
    assert "5" in msg
    assert "Aucune position" in msg
    assert "0 gain" in msg and "0 perte" in msg
    assert "+0.00" in msg


def test_build_ondemand_recap_message_lists_open_positions():
    positions = [
        {"token_address": "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH",
         "token_name": "DogWifCap", "token_symbol": "DWC", "peak_multiplier": 2.5},
        {"token_address": "9yPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXhI",
         "token_name": None, "token_symbol": None, "peak_multiplier": 1.0},
    ]
    msg = notifier.build_ondemand_recap_message(
        n_watched=3, active_positions=positions, n_wins=1, n_losses=0, pnl_eur=4.25,
    )
    assert "DWC" in msg
    assert "x2.50" in msg
    assert "9yPQtg2CW3" in msg  # repli sur token_address[:10] si pas de nom/symbole
    assert "x1.00" in msg


def test_build_ondemand_recap_message_shows_current_multiplier_and_stop_when_known():
    """Répond à la confusion utilisateur : 'pourquoi le SL/TP n'est pas
    déclenché ?' — en montrant le prix actuel ET le seuil de stop
    calculé, la réponse devient visible directement dans le récap."""
    positions = [
        {"token_address": "SassAddrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
         "token_name": "SASSYSHIBA", "token_symbol": "SASS",
         "peak_multiplier": 4.15, "current_multiplier": 2.10},
    ]
    msg = notifier.build_ondemand_recap_message(
        n_watched=1, active_positions=positions, n_wins=0, n_losses=0, pnl_eur=0.0,
    )
    assert "pic x4.15" in msg
    assert "actuel x2.10" in msg
    assert "stop x2.00" in msg  # peak >= 3.0 -> stop 2.0, pas encore franchi (2.10 > 2.0)


def test_build_ondemand_recap_message_falls_back_to_peak_only_when_current_missing():
    """Position 'pending' (prix d'entrée pas encore fixé) : pas de
    current_multiplier -> repli sur l'ancien format, pas de stop affiché
    (rien à calculer sans prix actuel)."""
    positions = [
        {"token_address": "PendingAddrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
         "token_name": "Fresh", "token_symbol": "FR", "peak_multiplier": 1.0},
    ]
    msg = notifier.build_ondemand_recap_message(
        n_watched=1, active_positions=positions, n_wins=0, n_losses=0, pnl_eur=0.0,
    )
    assert "x1.00" in msg
    assert "stop" not in msg


def test_build_ondemand_recap_message_zero_wins_losses():
    msg = notifier.build_ondemand_recap_message(
        n_watched=0, active_positions=[], n_wins=0, n_losses=0, pnl_eur=-1.5,
    )
    assert "-1.50" in msg
    assert "+-1.50" not in msg
