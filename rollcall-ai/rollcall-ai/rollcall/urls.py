# rollcall/urls.py

"""
URL configuration for rollcall project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
"""

from django.contrib import admin
from django.urls import path, include  # Ensure 'include' is imported

# Imports for static and media file serving during development
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),

    # Include Attendance URLs at the root.
    # The namespace 'Attendance' is automatically picked up from Attendance/urls.py
    # because it defines app_name = 'Attendance'.
    path('', include('Attendance.urls')),

    # Add paths for other apps here if needed
    # path('other_app/', include('other_app.urls')),
]

# This block is for serving files during development (when DEBUG=True)
if settings.DEBUG:
    # Serve static files (CSS, JS, etc.)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    
    # Serve user-uploaded media files (e.g., profile pictures)
    # This line enables the serving of files from your MEDIA_ROOT.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)