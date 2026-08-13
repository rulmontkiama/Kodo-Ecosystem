"""
Téléchargeur résilient reprenable via HTTP Range Requests.
"""
import os
import hashlib
import urllib.request
import urllib.error

class ResumableDownloadError(Exception):
    """Exception levée lors d'un échec de téléchargement reprenable."""
    pass

class ResumableDownloader:
    """Gère le téléchargement de fichiers volumineux avec reprise automatique à l'octet près."""

    @classmethod
    def calculate_sha256(cls, filepath: str) -> str:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()

    @classmethod
    def download_file(cls, url: str, destination_path: str, expected_sha256: str = None, progress_callback=None) -> str:
        """Télécharge un fichier avec support de reprise (HTTP Range Request)."""
        part_path = destination_path + ".part"
        existing_size = 0
        
        if os.path.exists(part_path):
            existing_size = os.path.getsize(part_path)

        req = urllib.request.Request(url)

        # Si le fichier partiel existe déjà, demander le reste à partir de l'octet courant
        if existing_size > 0:
            req.add_header("Range", f"bytes={existing_size}-")

        mode = "ab" if existing_size > 0 else "wb"

        try:
            with urllib.request.urlopen(req) as response, open(part_path, mode) as out_file:
                # Code HTTP 206 Partial Content (Reprise) ou 200 OK (Nouveau)
                status_code = getattr(response, 'status', 200)
                
                # Si le serveur ne supporte pas le Range (200 OK au lieu de 206), repartir de zéro
                if existing_size > 0 and status_code == 200:
                    out_file.close()
                    open(part_path, "wb").close()
                    out_file = open(part_path, "wb")
                    existing_size = 0

                content_length = response.headers.get("Content-Length")
                total_size = int(content_length) + existing_size if content_length else None

                downloaded = existing_size
                while True:
                    buffer = response.read(8192)
                    if not buffer:
                        break
                    out_file.write(buffer)
                    downloaded += len(buffer)
                    if progress_callback and total_size:
                        progress_callback(downloaded, total_size)

        except urllib.error.HTTPError as he:
            # Code 416 : Range Not Satisfiable -> Fichier déjà complet
            if he.code == 416 and existing_size > 0:
                pass
            else:
                raise ResumableDownloadError(f"Erreur HTTP {he.code}: {he.reason}")
        except Exception as e:
            raise ResumableDownloadError(f"Échec de connexion durant le téléchargement: {e}")

        # Validation de l'empreinte SHA256 post-téléchargement
        if expected_sha256:
            actual_sha = cls.calculate_sha256(part_path)
            if actual_sha.lower() != expected_sha256.lower():
                raise ResumableDownloadError(f"Checksum SHA256 corrompu post-téléchargement ! Attendu: {expected_sha256}, Obtenu: {actual_sha}")

        # Renommer le fichier .part vers sa destination finale
        os.replace(part_path, destination_path)
        return destination_path
