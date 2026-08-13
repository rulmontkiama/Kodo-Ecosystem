"""
Service de synchronisation Shopify asynchrone et non-bloquant pour Kōdo POS.
"""
import threading
import queue
import time
from core.config import ShopConfig

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
        # Simulation d'un appel d'API REST Shopify asynchrone
        time.sleep(0.5) # Simule le délai réseau sans bloquer la caisse
        # print(f"🟢 [Shopify Sync] Stock synchronisé pour SKU {task.get('sku')}: {task.get('stock')}")
