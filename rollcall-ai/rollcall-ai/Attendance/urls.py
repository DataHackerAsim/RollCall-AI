# Attendance/urls.py

from django.urls import path
from .views import (
    login_view,
    logout_view,
    page1_view,
    attendance_view,
    take_attendance,
    take_attendance_image,
    download_attendance,
    upload_csv,
    video_feed,
    course_api_list,
    class_api_detail,
    reload_embeddings,
    set_classroom_override,
    enrollment_api,
    save_training_image,
    # Pipeline views
    face_manager_view,
    pipeline_extract,
    pipeline_label,
    pipeline_move_sample,
    pipeline_invalidate_sample,
    pipeline_add_sample,
    pipeline_retrain,
    pipeline_identities_api,
    pipeline_enroll_student,
    pipeline_manual_capture,
)

app_name = 'Attendance'

urlpatterns = [
    # --- Main Page Views ---
    path('', login_view, name='app_root'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('dashboard/', page1_view, name='instructor_dashboard'),
    path('attendance/', attendance_view, name='pro_attend'),

    # --- Backend and AJAX Views ---
    path('take_attendance/', take_attendance, name='take_attendance'),
    path('take_attendance_image/', take_attendance_image, name='take_attendance_image'),
    path('video_feed/', video_feed, name='video_feed'),
    
    # --- Data Handling Views ---
    path('download_attendance/', download_attendance, name='download_attendance'),
    path('upload_csv/', upload_csv, name='upload_csv'),
    path('set_classroom_override/', set_classroom_override, name='set_classroom_override'),
    path('save_training_image/', save_training_image, name='save_training_image'),

    # --- Utility Views ---
    path('reload_embeddings/', reload_embeddings, name='reload_embeddings'),

    # --- API Endpoints ---
    path('api/courses/', course_api_list, name='api_course_list'),
    path('api/classes/<int:pk>/', class_api_detail, name='api_class_detail'),
    path('api/class/<int:class_id>/enrollments/', enrollment_api, name='enrollment_api'),

    # --- Face Pipeline ---
    path('face-manager/', face_manager_view, name='face_manager'),
    path('api/pipeline/extract/', pipeline_extract, name='pipeline_extract'),
    path('api/pipeline/label/', pipeline_label, name='pipeline_label'),
    path('api/pipeline/move-sample/', pipeline_move_sample, name='pipeline_move_sample'),
    path('api/pipeline/invalidate-sample/', pipeline_invalidate_sample, name='pipeline_invalidate_sample'),
    path('api/pipeline/add-sample/', pipeline_add_sample, name='pipeline_add_sample'),
    path('api/pipeline/retrain/', pipeline_retrain, name='pipeline_retrain'),
    path('api/pipeline/identities/', pipeline_identities_api, name='pipeline_identities'),
    path('api/pipeline/enroll-student/', pipeline_enroll_student, name='pipeline_enroll_student'),
    path('api/pipeline/manual-capture/', pipeline_manual_capture, name='pipeline_manual_capture'),
]