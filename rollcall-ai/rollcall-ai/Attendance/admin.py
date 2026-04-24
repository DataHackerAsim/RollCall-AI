# Attendance/admin.py

from django.contrib import admin
from django.contrib.auth.models import User
from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe
from .models import Teacher, Batch, Section, Course, Class, Student, AttendanceRecord, Enrollment, ClassroomOverride, ExtractionSession, Identity, FaceSample

# Customize admin site header and title
admin.site.site_header = "RollCall AI — Administration"
admin.site.site_title = "RollCall AI Admin"
admin.site.index_title = "Attendance management"
from django.utils.html import format_html

class TeacherAdminForm(forms.ModelForm):
    """Custom form for Teacher admin to show all users and indicate which ones already have teachers"""
    user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        required=True,
        widget=forms.Select,
        empty_label="Select a user"
    )
    
    class Meta:
        model = Teacher
        fields = '__all__'
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Get users who already have teachers (excluding current instance if editing)
        existing_teacher_users = Teacher.objects.values_list('user', flat=True)
        if self.instance.pk:
            existing_teacher_users = existing_teacher_users.exclude(pk=self.instance.pk)
        
        # Custom label for users to show which ones already have teachers
        user_choices = []
        for user in User.objects.all():
            if user.pk in existing_teacher_users:
                label = f"{user.username} ({user.get_full_name()}) - ⚠️ Already has teacher profile"
            else:
                label = f"{user.username} ({user.get_full_name() or 'No full name'})"
            user_choices.append((user.pk, label))
        
        self.fields['user'].choices = [('', 'Select a user')] + user_choices
    
    def clean_user(self):
        user = self.cleaned_data.get('user')
        if user:
            # Check if this user already has a teacher profile (excluding current instance)
            existing = Teacher.objects.filter(user=user)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            
            if existing.exists():
                raise forms.ValidationError(
                    f"User '{user.username}' already has a teacher profile. "
                    "Each user can only have one teacher profile."
                )
        return user


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    form = TeacherAdminForm
    list_display = ('user_display', 'faculty_status_badge', 'department', 'is_available_badge', 'contact_info')
    list_filter = ('faculty_status', 'department')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'user__email')
    readonly_fields = ('profile_picture_preview',)
    
    fieldsets = (
        ('User Information', {
            'fields': ('user', 'profile_picture', 'profile_picture_preview'),
            'description': 'Select a user to create a teacher profile. Each user can only have one teacher profile.'
        }),
        ('Professional Information', {
            'fields': ('faculty_status', 'department', 'office_location', 'phone_number')
        }),
        ('Employment Dates', {
            'fields': ('date_joined', 'leave_start_date', 'leave_end_date'),
            'classes': ('collapse',)
        }),
    )
    
    def user_display(self, obj):
        return format_html(
            '<strong>{}</strong><br><small>{}</small>',
            obj.user.get_full_name() or obj.user.username,
            obj.user.email
        )
    user_display.short_description = 'Teacher'
    
    def faculty_status_badge(self, obj):
        colors = {
            'PERM': '#48bb78',
            'VISIT': '#4299e1',
            'LEAVE': '#ed8936'
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            colors.get(obj.faculty_status, '#718096'),
            obj.get_faculty_status_display()
        )
    faculty_status_badge.short_description = 'Status'
    
    def is_available_badge(self, obj):
        if obj.is_available:
            return format_html('<span style="color: #48bb78;">✔ Available</span>')
        return format_html('<span style="color: #e53e3e;">✗ On Leave</span>')
    is_available_badge.short_description = 'Availability'
    
    def contact_info(self, obj):
        return format_html(
            '<small>{}</small>',
            obj.phone_number or 'No phone'
        )
    contact_info.short_description = 'Contact'
    
    def profile_picture_preview(self, obj):
        if obj.profile_picture:
            return format_html('<img src="{}" width="100" style="border-radius: 8px;" />', obj.profile_picture.url)
        return "No image"
    profile_picture_preview.short_description = 'Preview'


# Inline for Sections in Batch admin
class SectionInline(admin.TabularInline):
    model = Section
    extra = 1
    fields = ('section_name', 'max_students', 'is_active', 'current_enrollment')
    readonly_fields = ('current_enrollment',)
    
    def current_enrollment(self, obj):
        if obj.pk:
            return obj.current_enrollment
        return 0
    current_enrollment.short_description = 'Students'


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ('batch_code', 'batch_name', 'program_badge', 'year_range', 'sections_count', 'student_count', 'is_active_badge')
    list_filter = ('is_active', 'program', 'degree_level', 'start_year')
    search_fields = ('batch_code', 'batch_name', 'program')
    inlines = [SectionInline]
    
    def program_badge(self, obj):
        return format_html(
            '<span style="background-color: #f0f4f8; color: #2c5282; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            obj.program
        )
    program_badge.short_description = 'Program'
    
    def year_range(self, obj):
        return f"{obj.start_year} - {obj.end_year}"
    year_range.short_description = 'Duration'
    
    def sections_count(self, obj):
        count = obj.sections.count()
        if count > 0:
            return format_html('<strong>{}</strong> section(s)', count)
        return format_html('<span style="color: #718096;">No sections</span>')
    sections_count.short_description = 'Sections'
    
    def student_count(self, obj):
        count = obj.students.count()
        return format_html('<strong>{}</strong> students', count)
    student_count.short_description = 'Enrolled'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #e53e3e;">● Inactive</span>')
    is_active_badge.short_description = 'Status'


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ('section_display', 'batch_badge', 'enrollment_status', 'is_active_badge')
    list_filter = ('is_active', 'batch__batch_code')
    search_fields = ('section_name', 'batch__batch_code', 'batch__batch_name')
    
    def section_display(self, obj):
        return format_html(
            '<strong>Section {}</strong>',
            obj.section_name
        )
    section_display.short_description = 'Section'
    
    def batch_badge(self, obj):
        return format_html(
            '<span style="background-color: #f0f4f8; color: #2c5282; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            obj.batch.batch_code
        )
    batch_badge.short_description = 'Batch'
    
    def enrollment_status(self, obj):
        current = obj.current_enrollment
        max_students = obj.max_students
        if max_students:
            percentage = (current / max_students) * 100 if max_students > 0 else 0
            percentage_display = f"{percentage:.0f}%"   # format before passing
            color = '#48bb78' if percentage < 90 else '#ed8936' if percentage < 100 else '#e53e3e'
            return format_html(
                '<span style="color: {};">{}/{} ({})</span>',
                color, current, max_students, percentage_display
            )
        return format_html('<strong>{}</strong> students', current)

    enrollment_status.short_description = 'Enrollment'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #e53e3e;">● Inactive</span>')
    is_active_badge.short_description = 'Status'


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('course_code_display', 'title', 'course_type_badge', 'credit_hours_badge', 'department', 'prerequisites_count')
    list_filter = ('course_type', 'department', 'credit_hours')
    search_fields = ('course_code', 'title')
    filter_horizontal = ('prerequisite_courses',)
    
    def course_code_display(self, obj):
        return format_html(
            '<span style="font-family: monospace; background-color: #f0f4f8; padding: 4px 8px; border-radius: 4px;">{}</span>',
            obj.course_code
        )
    course_code_display.short_description = 'Code'
    
    def course_type_badge(self, obj):
        colors = {
            'CORE': '#2c5282',
            'ELEC': '#48bb78',
            'LAB': '#ed8936',
            'PROJ': '#9f7aea'
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            colors.get(obj.course_type, '#718096'),
            obj.get_course_type_display()
        )
    course_type_badge.short_description = 'Type'
    
    def credit_hours_badge(self, obj):
        return format_html(
            '<span style="background-color: #4299e1; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{} CR</span>',
            obj.credit_hours
        )
    credit_hours_badge.short_description = 'Credits'
    
    def prerequisites_count(self, obj):
        count = obj.prerequisite_courses.count()
        if count == 0:
            return format_html('<span style="color: #718096;">None</span>')
        return format_html('<strong>{}</strong> course(s)', count)
    prerequisites_count.short_description = 'Prerequisites'


# Inline for Enrollments in Class admin
class EnrollmentInline(admin.TabularInline):
    model = Enrollment
    extra = 0
    fields = ('student', 'enrollment_type', 'is_active', 'final_grade')
    autocomplete_fields = ['student']
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "student":
            kwargs["queryset"] = Student.objects.filter(is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ClassAdminForm(forms.ModelForm):
    """Custom form for Class admin to validate RTSP URLs"""
    class Meta:
        model = Class
        fields = '__all__'
    
    def clean_rtsp_stream_url(self):
        url = self.cleaned_data.get('rtsp_stream_url')
        if url:
            # Basic validation for RTSP URL format
            if not url.startswith('rtsp://'):
                raise forms.ValidationError(
                    "RTSP URL must start with 'rtsp://'"
                )
            # Optional: Add more validation if needed
            if '@' not in url:
                raise forms.ValidationError(
                    "RTSP URL should include authentication (username:password@ip)"
                )
        return url


@admin.register(Class)
class ClassAdmin(admin.ModelAdmin):
    form = ClassAdminForm
    list_display = ('class_display', 'course_type', 'semester_badge', 'enrollment_info', 'schedule_info', 'stream_status', 'is_active_badge')
    list_filter = ('semester', 'course__course_type', 'is_active', 'academic_year', 'instructors')
    search_fields = ('course__course_code', 'course__title', 'batches__batch_code', 'classroom')
    filter_horizontal = ('instructors', 'batches', 'sections')
    date_hierarchy = 'start_date'
    inlines = [EnrollmentInline]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('course', 'instructors')
        }),
        ('Target Audience', {
            'fields': ('batches', 'sections', 'max_enrollment'),
            'description': 'Select batches for the class. Optionally specify sections for section-specific classes.'
        }),
        ('Schedule', {
            'fields': ('semester', 'academic_year', 'class_days', 'start_time', 'end_time', 'classroom')
        }),
        ('Stream Configuration', {
            'fields': ('rtsp_stream_url',),
            'description': 'Enter the RTSP URL for the classroom camera (e.g., rtsp://username:password@ip:port/stream)'
        }),
        ('Status', {
            'fields': ('is_active', 'start_date', 'end_date')
        }),
    )
    
    def class_display(self, obj):
        batches_info = ", ".join([b.batch_code for b in obj.batches.all()[:2]])
        if obj.batches.count() > 2:
            batches_info += f" (+{obj.batches.count() - 2})"
        
        instructors = ", ".join([t.user.get_full_name() or t.user.username for t in obj.instructors.all()[:2]])
        if obj.instructors.count() > 2:
            instructors += f" (+{obj.instructors.count() - 2})"
        
        return format_html(
            '<strong>{}</strong> - {}<br><small>Instructors: {}</small>',
            obj.course.course_code,
            batches_info or "No batches",
            instructors or "Not assigned"
        )
    class_display.short_description = 'Class'
    
    def course_type(self, obj):
        return obj.course.get_course_type_display()
    course_type.short_description = 'Type'
    
    def semester_badge(self, obj):
        return format_html(
            '<span style="background-color: #2c5282; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            obj.semester
        )
    semester_badge.short_description = 'Semester'
    

    def enrollment_info(self, obj):
        try:
            current = float(obj.current_enrollment)
        except (TypeError, ValueError):
            current = 0.0

        try:
            max_enroll = float(obj.max_enrollment)
        except (TypeError, ValueError):
            max_enroll = 0.0

        if max_enroll > 0:
            percentage = (current / max_enroll) * 100
            # Pre-format the percentage into a normal Python string
            perc_str = f"{percentage:.0f}%"
            color = '#48bb78' if percentage < 90 else '#ed8936' if percentage < 100 else '#e53e3e'
            return format_html(
                '<span style="color: {};">{}/{} ({})</span>',
                color,
                int(current),
                int(max_enroll),
                perc_str
            )

        return format_html('<strong>{}</strong> enrolled', int(current))

    enrollment_info.short_description = 'Enrollment'

    
    def schedule_info(self, obj):
        if obj.start_time and obj.end_time:
            return format_html(
                '<small>{}<br>{} - {}</small>',
                obj.class_days or 'No days set',
                obj.start_time.strftime('%I:%M %p'),
                obj.end_time.strftime('%I:%M %p')
            )
        return format_html('<small style="color: #718096;">Not scheduled</small>')
    schedule_info.short_description = 'Schedule'
    
    def stream_status(self, obj):
        if obj.has_stream:
            return format_html('<span style="color: #48bb78;">✔ Configured</span>')
        return format_html('<span style="color: #e53e3e;">✗ No stream</span>')
    stream_status.short_description = 'Stream'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #e53e3e;">● Inactive</span>')
    is_active_badge.short_description = 'Status'


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('student_display', 'batch_badge', 'section_badge', 'contact_display', 'data_status', 'is_active_badge')
    list_filter = ('is_active', 'batch', 'section', 'enrollment_date')
    search_fields = ('registration_id', 'first_name', 'last_name', 'email')
    date_hierarchy = 'enrollment_date'
    
    fieldsets = (
        ('Personal Information', {
            'fields': ('registration_id', 'first_name', 'last_name', 'email', 'phone_number')
        }),
        ('Academic Information', {
            'fields': ('batch', 'section', 'enrollment_date', 'is_active')
        }),
        ('Face Recognition Data', {
            'fields': ('face_embedding_file', 'images_folder_path'),
            'description': 'Face embedding file should be a .npy file. Images folder path should be absolute server path.',
            'classes': ('collapse',)
        }),
    )
    
    def student_display(self, obj):
        return format_html(
            '<strong>{}</strong><br><small>{}</small>',
            obj.get_full_name(),
            obj.registration_id
        )
    student_display.short_description = 'Student'
    
    def batch_badge(self, obj):
        if obj.batch:
            return format_html(
                '<span style="background-color: #f0f4f8; color: #2c5282; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
                obj.batch.batch_code
            )
        return format_html('<span style="color: #718096;">No batch</span>')
    batch_badge.short_description = 'Batch'
    
    def section_badge(self, obj):
        if obj.section:
            return format_html(
                '<span style="background-color: #9f7aea; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">Section {}</span>',
                obj.section.section_name
            )
        return format_html('<span style="color: #718096;">-</span>')
    section_badge.short_description = 'Section'
    
    def contact_display(self, obj):
        contact_info = []
        if obj.email:
            contact_info.append(f'✉ {obj.email}')
        if obj.phone_number:
            contact_info.append(f'☎ {obj.phone_number}')
        return format_html('<small>{}</small>', '<br>'.join(contact_info) or 'No contact info')
    contact_display.short_description = 'Contact'
    
    def data_status(self, obj):
        statuses = []
        if obj.face_embedding_file:
            statuses.append('<span style="color: #48bb78;">✔ Embedding</span>')
        else:
            statuses.append('<span style="color: #e53e3e;">✗ Embedding</span>')
            
        if obj.has_images_folder:
            statuses.append('<span style="color: #48bb78;">✔ Images</span>')
        else:
            statuses.append('<span style="color: #e53e3e;">✗ Images</span>')
            
        return format_html(' | '.join(statuses))
    data_status.short_description = 'Data'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #e53e3e;">● Inactive</span>')
    is_active_badge.short_description = 'Status'


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ('student_display', 'class_display', 'enrollment_type_badge', 'enrollment_date_display', 'grade_display', 'is_active_badge')
    list_filter = ('enrollment_type', 'is_active', 'class_instance__semester', 'class_instance__course__course_type')
    search_fields = ('student__registration_id', 'student__first_name', 'student__last_name', 'class_instance__course__course_code')
    date_hierarchy = 'enrollment_date'
    autocomplete_fields = ['student', 'class_instance']
    
    def student_display(self, obj):
        section_info = f" (Sec {obj.student.section.section_name})" if obj.student.section else ""
        return format_html(
            '<strong>{}</strong><br><small>{}{}</small>',
            obj.student.get_full_name(),
            obj.student.registration_id,
            section_info
        )
    student_display.short_description = 'Student'
    
    def class_display(self, obj):
        return format_html(
            '{} - {}',
            obj.class_instance.course.course_code,
            obj.class_instance.semester
        )
    class_display.short_description = 'Class'
    
    def enrollment_type_badge(self, obj):
        colors = {
            'REG': '#48bb78',
            'REP': '#ed8936',
            'AUD': '#4299e1',
            'TRA': '#9f7aea'
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            colors.get(obj.enrollment_type, '#718096'),
            obj.get_enrollment_type_display()
        )
    enrollment_type_badge.short_description = 'Type'
    
    def enrollment_date_display(self, obj):
        return obj.enrollment_date.strftime('%b %d, %Y')
    enrollment_date_display.short_description = 'Enrolled'
    
    def grade_display(self, obj):
        if obj.final_grade:
            return format_html(
                '<strong style="color: #2c5282;">{}</strong>',
                obj.final_grade
            )
        return format_html('<span style="color: #718096;">-</span>')
    grade_display.short_description = 'Grade'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #e53e3e;">● Inactive</span>')
    is_active_badge.short_description = 'Status'


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ('student_display', 'class_display', 'date_time_display', 'status_badge', 'marked_by_display')
    list_filter = ('status', 'attendance_date', 'class_instance__course', 'class_instance__batches')
    search_fields = ('student__registration_id', 'student__first_name', 'student__last_name')
    date_hierarchy = 'attendance_date'
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('student', 'student__section', 'class_instance__course', 'marked_by').prefetch_related('class_instance__batches')
    
    def student_display(self, obj):
        section_info = f" (Sec {obj.student.section.section_name})" if obj.student.section else ""
        return format_html(
            '<strong>{}</strong><br><small>{}{}</small>',
            obj.student.get_full_name(),
            obj.student.registration_id,
            section_info
        )
    student_display.short_description = 'Student'
    
    def class_display(self, obj):
        batches = ", ".join([b.batch_code for b in obj.class_instance.batches.all()[:2]])
        return format_html(
            '{} - {}',
            obj.class_instance.course.course_code,
            batches
        )
    class_display.short_description = 'Class'
    
    def date_time_display(self, obj):
        date_str = obj.attendance_date.strftime('%b %d, %Y')
        time_str = obj.attendance_time.strftime('%I:%M %p') if obj.attendance_time else 'No time'
        return format_html(
            '<strong>{}</strong><br><small>{}</small>',
            date_str,
            time_str
        )
    date_time_display.short_description = 'Date & Time'
    
    def status_badge(self, obj):
        colors = {
            'P': '#48bb78',
            'A': '#e53e3e',
            'L': '#ed8936',
            'E': '#4299e1'
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px;">{}</span>',
            colors.get(obj.status, '#718096'),
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    
    def marked_by_display(self, obj):
        if obj.marked_by:
            return format_html('<small>{}</small>', obj.marked_by.username)
        return format_html('<small style="color: #718096;">System</small>')
    marked_by_display.short_description = 'Marked By'


@admin.register(ClassroomOverride)
class ClassroomOverrideAdmin(admin.ModelAdmin):
    list_display = ('class_display', 'override_date', 'classroom_change', 'reason_summary', 'created_by', 'is_active_badge')
    list_filter = ('is_active', 'override_date', 'created_by')
    search_fields = ('class_instance__course__course_code', 'reason', 'temporary_classroom')
    date_hierarchy = 'override_date'
    
    def class_display(self, obj):
        return format_html(
            '<strong>{}</strong>',
            obj.class_instance
        )
    class_display.short_description = 'Class'
    
    def classroom_change(self, obj):
        return format_html(
            '<span style="text-decoration: line-through; color: #e53e3e;">{}</span> → <span style="color: #48bb78;">{}</span>',
            obj.original_classroom,
            obj.temporary_classroom
        )
    classroom_change.short_description = 'Classroom Change'
    
    def reason_summary(self, obj):
        reason = obj.reason[:50] + '...' if len(obj.reason) > 50 else obj.reason
        return format_html('<small>{}</small>', reason)
    reason_summary.short_description = 'Reason'
    
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #48bb78;">● Active</span>')
        return format_html('<span style="color: #718096;">○ Inactive</span>')
    is_active_badge.short_description = 'Status'

# ═══════════════════════════════════════════════════════════════════
# FACE PIPELINE ADMIN
# ═══════════════════════════════════════════════════════════════════

class FaceSampleInline(admin.TabularInline):
    model = FaceSample
    extra = 0
    readonly_fields = ('image_path', 'embedding_path', 'quality_score', 'source_frame', 'created_at')
    fields = ('image_path', 'quality_score', 'is_valid', 'source_frame', 'created_at')


@admin.register(Identity)
class IdentityAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'class_instance', 'is_labeled', 'student', 'sample_count', 'needs_retraining')
    list_filter = ('is_labeled', 'needs_retraining', 'is_active', 'class_instance')
    search_fields = ('label', 'auto_label', 'student__first_name', 'student__last_name')
    inlines = [FaceSampleInline]
    readonly_fields = ('auto_label', 'created_at', 'updated_at', 'embedding_file', 'representative_image')


@admin.register(ExtractionSession)
class ExtractionSessionAdmin(admin.ModelAdmin):
    list_display = ('pk', 'class_instance', 'source_type', 'faces_extracted', 'status', 'created_by', 'created_at')
    list_filter = ('status', 'source_type')
    readonly_fields = ('created_at',)


@admin.register(FaceSample)
class FaceSampleAdmin(admin.ModelAdmin):
    list_display = ('pk', 'identity', 'quality_score', 'is_valid', 'source_frame', 'created_at')
    list_filter = ('is_valid', 'identity__class_instance')
    search_fields = ('identity__label', 'identity__auto_label')
