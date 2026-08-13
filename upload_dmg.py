import os
import sys
import json
import getpass
from ftplib import FTP

def print_progress(bytes_transferred, total_bytes):
    percent = (bytes_transferred / total_bytes) * 100
    bar = "#" * int(percent / 5) + "-" * (20 - int(percent / 5))
    sys.stdout.write(f"\r📤 Uploading: [{bar}] {percent:.1f}% ({bytes_transferred}/{total_bytes} bytes)")
    sys.stdout.flush()

def upload_ftp(host, port, username, password, remote_path, local_file):
    print(f"\n⚡ Connexion FTP à {host}:{port}...")
    ftp = FTP()
    ftp.connect(host, port)
    ftp.login(username, password)
    
    total_bytes = os.path.getsize(local_file)
    bytes_transferred = 0
    
    # Change directory if specified
    if remote_path:
        try:
            ftp.cwd(remote_path)
        except Exception:
            # Try to create dir if it doesn't exist
            ftp.mkd(remote_path)
            ftp.cwd(remote_path)
            
    filename = os.path.basename(local_file)
    print(f"🚀 Début de l'envoi de {filename} ({total_bytes} octets)...")
    
    def callback(data):
        nonlocal bytes_transferred
        bytes_transferred += len(data)
        print_progress(bytes_transferred, total_bytes)
        
    with open(local_file, 'rb') as f:
        ftp.storbinary(f'STOR {filename}', f, blocksize=8192, callback=callback)
        
    ftp.quit()
    print("\n✅ Transfert FTP terminé avec succès !")

def upload_sftp(host, port, username, password, remote_path, local_file):
    try:
        import paramiko
    except ImportError:
        print("\n❌ Erreur : La bibliothèque 'paramiko' est requise pour le protocole SFTP.")
        print("💡 Veuillez l'installer en exécutant : pip install paramiko")
        sys.exit(1)
        
    print(f"\n⚡ Connexion SFTP (SSH) à {host}:{port}...")
    transport = paramiko.Transport((host, port))
    transport.connect(username=username, password=password)
    
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    total_bytes = os.path.getsize(local_file)
    
    # Change directory or create it
    if remote_path:
        try:
            sftp.chdir(remote_path)
        except IOError:
            sftp.mkdir(remote_path)
            sftp.chdir(remote_path)
            
    filename = os.path.basename(local_file)
    print(f"🚀 Début de l'envoi de {filename} ({total_bytes} octets)...")
    
    def callback(transferred, total):
        print_progress(transferred, total_bytes)
        
    sftp.put(local_file, filename, callback=callback)
    
    sftp.close()
    transport.close()
    print("\n✅ Transfert SFTP terminé avec succès !")

def main():
    print("==========================================================")
    print("         MODULE D'UPLOAD WEB — KŌDO SOLUTIONS             ")
    print("==========================================================")
    
    local_file = "Installation_Kodo_POS.dmg"
    if not os.path.exists(local_file):
        print(f"❌ Erreur : Fichier '{local_file}' introuvable.")
        print("💡 Veuillez d'abord compiler le logiciel avec ./build_final_pro.sh")
        sys.exit(1)
        
    config_file = "upload_config.json"
    config = {}
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            print("💾 Configuration chargée depuis upload_config.json")
        except Exception as e:
            print(f"⚠️ Impossible de lire upload_config.json : {e}")
            
    # Prompts
    protocol = config.get("protocol", "").lower()
    if protocol not in ["ftp", "sftp"]:
        protocol = input("Choisissez le protocole (ftp / sftp) [sftp] : ").strip().lower() or "sftp"
        
    host = config.get("host", "")
    if not host:
        host = input("Hôte (ex: ftp.mon-site.com ou sftp.mon-site.com) : ").strip()
        
    port = config.get("port", None)
    if not port:
        default_port = 22 if protocol == "sftp" else 21
        port_input = input(f"Port [{default_port}] : ").strip()
        port = int(port_input) if port_input else default_port
    else:
        port = int(port)
        
    username = config.get("username", "")
    if not username:
        username = input("Identifiant / Nom d'utilisateur : ").strip()
        
    password = getpass.getpass("Mot de passe : ")
    
    remote_path = config.get("remote_path", "")
    if not remote_path:
        remote_path = input("Chemin distant sur le serveur (ex: /public_html/download) : ").strip()
        
    # Save config (without password for safety)
    save_config = {
        "protocol": protocol,
        "host": host,
        "port": port,
        "username": username,
        "remote_path": remote_path
    }
    
    # Save base URL if exists
    url_base = config.get("public_url", "")
    if url_base:
        save_config["public_url"] = url_base
        
    with open(config_file, 'w') as f:
        json.dump(save_config, f, indent=4)
    print(f"📝 Configuration sauvegardée dans {config_file} (le mot de passe a été exclu par sécurité).")
    
    try:
        if protocol == "sftp":
            upload_sftp(host, port, username, password, remote_path, local_file)
        else:
            upload_ftp(host, port, username, password, remote_path, local_file)
            
        # Generer le lien public
        if not url_base:
            url_base = input("Entrez l'URL publique de votre site pour générer le lien de téléchargement (ex: https://mon-site.com) : ").strip()
            if url_base:
                save_config["public_url"] = url_base
                with open(config_file, 'w') as f:
                    json.dump(save_config, f, indent=4)
            else:
                url_base = "https://votre-site.com"
                    
        clean_remote_path = remote_path.replace("public_html/", "").replace("www/", "").strip("/")
        if clean_remote_path:
            download_url = f"{url_base.rstrip('/')}/{clean_remote_path}/{local_file}"
        else:
            download_url = f"{url_base.rstrip('/')}/{local_file}"
            
        print("\n==========================================================")
        print("🔗 LIEN DE TÉLÉCHARGEMENT DIRECT :")
        print(download_url)
        print("==========================================================")
        
    except Exception as e:
        print(f"\n❌ Une erreur est survenue lors du transfert : {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
