"""
Service de synchronisation Shopify asynchrone et non-bloquant pour Kōdo POS.

File d'attente des CORRECTIONS D'INVENTAIRE (une quantité comptée en boutique qu'on impose à
la boutique en ligne). Les ventes et les remboursements ne passent pas par ici : ils sont
poussés en relatif, avec journal d'idempotence, par `kodo_core.sync.shopify`. Mélanger les deux
recréerait exactement la double décrémentation qu'on vient de corriger.

Avant, `_execute_task` était un placebo : son corps était un `time.sleep(0.5)` commenté
« Simulation d'un appel d'API REST Shopify ». La file tournait, la caisse croyait synchroniser,
et rien n'était jamais envoyé.
"""
import threading
import queue
import time

class ShopifyService:
    """Gestionnaire de file de synchronisation e-commerce en arrière-plan."""

    _task_queue = queue.Queue()
    _worker_thread = None
    _running = False

    @classmethod
    def start_service(cls):
        """Démarre le thread d'arrière-plan de synchronisation."""
        if not cls._running:
            cls._running = True
            cls._worker_thread = threading.Thread(target=cls._process_queue, daemon=True)
            cls._worker_thread.start()

    @classmethod
    def enqueue_stock_sync(cls, product_id: int, variant_sku: str, new_stock: int):
        """Ajoute une mise à jour de stock à la file asynchrone sans bloquer l'IHM."""
        cls.start_service()
        cls._task_queue.put({
            "action": "sync_stock",
            "product_id": product_id,
            "sku": variant_sku,
            "stock": new_stock,
            "timestamp": time.time()
        })

    @classmethod
    def _process_queue(cls):
        while cls._running:
            try:
                task = cls._task_queue.get(timeout=2)
                cls._execute_task(task)
                cls._task_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"⚠️ Erreur Sync Shopify: {e}")

    @classmethod
    def _execute_task(cls, task: dict):
        """Impose à Shopify la quantité comptée localement pour un SKU."""
        if task.get("action") != "sync_stock":
            return
        sku = task.get("sku")
        if not sku:
            return

        from kodo_core.sync import shopify as shopify_sync

        # Une suite de tests ne doit jamais ouvrir de connexion vers une boutique.
        if shopify_sync._tests_en_cours():
            return

        moteur = shopify_sync.ShopifySync()
        # Rien n'est envoyé si la boutique n'est pas configurée ou si la commerçante a éteint
        # la synchronisation du stock dans ses réglages.
        if not moteur.est_configure() or not moteur.auto_sync:
            return

        location_id = moteur.get_location_id()
        if not location_id:
            return
        inventory_item_id = moteur.find_inventory_item_id(str(sku))
        if not inventory_item_id:
            return
        moteur.set_shopify_stock(inventory_item_id, location_id, int(task.get("stock") or 0))
