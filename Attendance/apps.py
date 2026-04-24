# Attendance/apps.py

from django.apps import AppConfig
import os
import sys


class AttendanceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Attendance'
    
    def ready(self):
        """Start background model warmup when Django boots."""
        # Skip for management commands
        skip_cmds = ['makemigrations', 'migrate', 'collectstatic', 'shell', 'test', 'createsuperuser']
        if any(cmd in sys.argv for cmd in skip_cmds):
            return
        
        # Django dev server: only warmup in main process, not reloader
        is_devserver = 'runserver' in sys.argv
        if is_devserver and os.environ.get('RUN_MAIN') != 'true':
            return
        
        try:
            import threading
            from . import views
            
            print("[Attendance] Starting background model warmup...")
            t = threading.Thread(target=views._background_startup, daemon=True)
            t.start()
        except Exception as e:
            print(f"[Attendance] Background warmup start failed: {e}")
            # Set the event so views don't hang waiting forever
            try:
                from . import views
                views._models_ready.set()
            except Exception:
                pass
