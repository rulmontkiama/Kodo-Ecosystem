# -*- coding: utf-8 -*-
"""Tests unitaires de la file de synchronisation offline-first (kodo_core.sync.offline_queue)."""

import sqlite3

import pytest

from kodo_core.sync.offline_queue import (
    EventType,
    OfflineQueueManager,
    QueueStatus,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def manager(conn):
    return OfflineQueueManager(conn)


def test_ensure_schema_is_idempotent(conn):
    OfflineQueueManager(conn)
    OfflineQueueManager(conn)


def test_enqueue_event_persists_with_pending_status(manager):
    event_id = manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 42})

    event = manager.get_event(event_id)
    assert event["event_type"] == EventType.VENTE_CREEE
    assert event["status"] == QueueStatus.PENDING
    assert event["retry_count"] == 0
    assert event["max_retries"] == 3


def test_enqueue_event_rejects_unknown_event_type(manager):
    with pytest.raises(ValueError):
        manager.enqueue_event("EVENT_INCONNU", {"foo": "bar"})


def test_enqueue_stock_ajuste_requires_relative_delta(manager):
    with pytest.raises(ValueError):
        manager.enqueue_event(EventType.STOCK_AJUSTE, {"product_id": "PROD-1", "quantite": 10})


def test_enqueue_stock_ajuste_accepts_relative_delta(manager):
    event_id = manager.enqueue_event(
        EventType.STOCK_AJUSTE, {"product_id": "PROD-1", "delta": -3}
    )
    event = manager.get_event(event_id)
    assert event["status"] == QueueStatus.PENDING


def test_process_pending_marks_successful_events(manager):
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 1})
    manager.enqueue_event(EventType.CLIENT_MODIFIE, {"client_id": 2})

    calls = []

    def handler(event_type, payload):
        calls.append((event_type, payload))

    summary = manager.process_pending(handler)

    assert summary == {"success": 2, "retried": 0, "failed": 0}
    assert len(calls) == 2
    statuses = {event["status"] for event in manager.list_events()}
    assert statuses == {QueueStatus.SUCCESS}


def test_process_pending_respects_fifo_order(manager):
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 1})
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 2})
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 3})

    processed_order = []

    def handler(event_type, payload):
        processed_order.append(payload["vente_id"])

    manager.process_pending(handler)

    assert processed_order == [1, 2, 3]


def test_process_pending_retries_on_failure_below_max_retries(manager):
    event_id = manager.enqueue_event(
        EventType.VENTE_CREEE, {"vente_id": 1}, max_retries=3
    )

    def failing_handler(event_type, payload):
        raise ConnectionError("panne réseau simulée")

    summary = manager.process_pending(failing_handler)

    assert summary == {"success": 0, "retried": 1, "failed": 0}
    event = manager.get_event(event_id)
    assert event["status"] == QueueStatus.PENDING
    assert event["retry_count"] == 1
    assert "panne réseau simulée" in event["error_message"]


def test_process_pending_marks_failed_after_max_retries(manager):
    event_id = manager.enqueue_event(
        EventType.VENTE_CREEE, {"vente_id": 1}, max_retries=2
    )

    def failing_handler(event_type, payload):
        raise ConnectionError("panne réseau simulée")

    # 1ère tentative : retry_count passe à 1, toujours < max_retries -> PENDING.
    manager.process_pending(failing_handler)
    assert manager.get_event(event_id)["status"] == QueueStatus.PENDING

    # 2ème tentative : retry_count atteint max_retries -> FAILED définitif.
    summary = manager.process_pending(failing_handler)

    assert summary == {"success": 0, "retried": 0, "failed": 1}
    event = manager.get_event(event_id)
    assert event["status"] == QueueStatus.FAILED
    assert event["retry_count"] == 2


def test_process_pending_does_not_reprocess_failed_or_success_events(manager):
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": 1}, max_retries=1)

    call_count = {"n": 0}

    def failing_handler(event_type, payload):
        call_count["n"] += 1
        raise ConnectionError("panne")

    manager.process_pending(failing_handler)  # retry_count=1 >= max_retries=1 -> FAILED
    manager.process_pending(failing_handler)  # ne doit plus être repris

    assert call_count["n"] == 1


def test_process_pending_isolates_events_success_and_failure(manager):
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": "ok"})
    manager.enqueue_event(EventType.VENTE_CREEE, {"vente_id": "ko"}, max_retries=1)

    def handler(event_type, payload):
        if payload["vente_id"] == "ko":
            raise ConnectionError("échec simulé")

    summary = manager.process_pending(handler)

    assert summary == {"success": 1, "retried": 0, "failed": 1}
    events_by_status = {event["status"] for event in manager.list_events()}
    assert events_by_status == {QueueStatus.SUCCESS, QueueStatus.FAILED}


def test_process_pending_with_empty_queue_returns_zero_summary(manager):
    summary = manager.process_pending(lambda event_type, payload: None)
    assert summary == {"success": 0, "retried": 0, "failed": 0}


def test_stock_ajuste_delta_resolution_allows_additive_merge_semantics(manager):
    """Deux ajustements relatifs concurrents sur le même produit doivent pouvoir être
    appliqués par simple addition des deltas, sans écrasement de l'un par l'autre."""
    manager.enqueue_event(EventType.STOCK_AJUSTE, {"product_id": "PROD-1", "delta": -2})
    manager.enqueue_event(EventType.STOCK_AJUSTE, {"product_id": "PROD-1", "delta": 5})

    applied_deltas = []

    def handler(event_type, payload):
        applied_deltas.append(payload["delta"])

    manager.process_pending(handler)

    # La résolution de conflit "delta relatif" attend que la somme des deltas soit
    # appliquée, contrairement à un écrasement par la dernière valeur absolue.
    assert sum(applied_deltas) == 3
