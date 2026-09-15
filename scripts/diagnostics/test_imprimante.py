import os
import sys
import subprocess

def list_usb_devices():
    print("Recherche des imprimantes USB connectées...")
    if sys.platform == "darwin":
        result = subprocess.run(["system_profiler", "SPUSBDataType"], stdout=subprocess.PIPE, text=True)
        print("Scanné. (Si vous voyez votre imprimante dans la liste, c'est bon signe !)")
    else:
        print("La recherche automatique est optimisée pour macOS.")

def test_print():
    print("\nTentative d'impression via python-escpos...")
    try:
        try:
            import usb.backend.libusb1
            import libusb_package
            _old_get_backend = usb.backend.libusb1.get_backend
            usb.backend.libusb1.get_backend = lambda *a, **k: _old_get_backend(find_library=libusb_package.find_library)
        except Exception:
            pass
            
        from escpos.printer import Usb
        # Liste élargie des imprimantes thermiques génériques
        LOW_BUDGET_PRINTERS = [
            (0x04b8, 0x0202), # Epson / Générique
            (0x0416, 0x5011), # Munbyn
            (0x04b8, 0x0e20), # Epson TM-m30-II / Munbyn Emulation
            (0x0483, 0x5740), # STMicroelectronics
            (0x1fc9, 0x2016), # NXP
            (0x04b8, 0x0e28), # Epson TM-T20III
            (0x04b8, 0x0e15), # Epson TM-T20II
            (0x0fe6, 0x811e), # Xprinter
            (0x154f, 0x154f), # Bixolon
        ]
        
        printer = None
        for vid, pid in LOW_BUDGET_PRINTERS:
            try:
                import usb.core
                backend = None
                try:
                    import libusb_package
                    backend = libusb_package.get_libusb1_backend()
                except Exception:
                    pass
                
                if usb.core.find(idVendor=vid, idProduct=pid, backend=backend) is not None:
                    printer = Usb(vid, pid)
                    print(f"✅ Imprimante trouvée (VID:{hex(vid)} PID:{hex(pid)}) !")
                    break
            except Exception as e:
                pass
                
        if printer:
            print("Envoi du ticket de test...")
            printer.text("==========================================\n")
            printer.text("           KODO POS - TEST OK             \n")
            printer.text("==========================================\n")
            printer.text("Si vous lisez ceci, l'imprimante est bien\n")
            printer.text("connectée et configurée en natif USB.\n")
            printer.text("==========================================\n")
            printer.text("\n\n\n\n\n\n")
            printer.cut()
            
            print("Voulez-vous tester l'ouverture du tiroir-caisse ? (o/n)")
            rep = input().lower()
            if rep == 'o':
                printer.cashdraw(2)
                print("Tiroir ouvert !")
            
            return True
        else:
            print("❌ Aucune imprimante thermique USB trouvée parmi la liste connue.")
            return False
            
    except ImportError:
        print("La bibliothèque python-escpos n'est pas installée.")
        return False
    except Exception as e:
        print(f"Erreur inattendue : {e}")
        return False

if __name__ == "__main__":
    print("==================================================")
    print("        UTILITAIRE DE TEST IMPRIMANTE POS         ")
    print("==================================================")
    list_usb_devices()
    test_print()
    print("==================================================")
