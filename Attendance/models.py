# Attendance/models.py

import os
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.conf import settings

class Teacher(models.Model):
    """Represents an instructor/teacher linked to a Django User."""
    
    # Faculty status choices
    class FacultyStatusChoices(models.TextChoices):
        PERMANENT = 'PERM', 'Permanent Faculty'
        VISITING = 'VISIT', 'Visiting Faculty'
        ON_LEAVE = 'LEAVE', 'On Leave'
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True, related_name='teacher')
    
    # New field for instructor's profile picture
    profile_picture = models.ImageField(
        upload_to='instructor_photos/',
        blank=True,
        null=True,
        help_text="Profile picture of the instructor"
    )
    
    # Faculty status tracking
    faculty_status = models.CharField(
        max_length=5,
        choices=FacultyStatusChoices.choices,
        default=FacultyStatusChoices.PERMANENT,
        help_text="Current employment status of the faculty member"
    )
    
    # Additional fields for better instructor management
    department = models.CharField(max_length=100, blank=True, help_text="Department or division")
    office_location = models.CharField(max_length=100, blank=True, help_text="Office room number or location")
    phone_number = models.CharField(max_length=20, blank=True, help_text="Contact phone number")
    
    # Dates for tracking employment
    date_joined = models.DateField(null=True, blank=True, help_text="Date when instructor joined the institution")
    leave_start_date = models.DateField(null=True, blank=True, help_text="Start date of leave (if applicable)")
    leave_end_date = models.DateField(null=True, blank=True, help_text="Expected end date of leave (if applicable)")

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.get_faculty_status_display()})"

    @property
    def first_name(self):
        return self.user.first_name

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def email(self):
        return self.user.email
    
    @property
    def is_available(self):
        """Check if instructor is currently available (not on leave)"""
        return self.faculty_status != self.FacultyStatusChoices.ON_LEAVE
    
    class Meta:
        ordering = ['user__last_name', 'user__first_name']
        verbose_name = 'Teacher'
        verbose_name_plural = 'Teachers'

class Batch(models.Model):
    """Represents a student batch/cohort (e.g., FA20-BCS, SP21-CS)"""
    
    batch_code = models.CharField(
        max_length=50, 
        unique=True, 
        db_index=True,
        help_text="Unique batch identifier like 'FA20-BCS' or 'SP21-CS'"
    )
    batch_name = models.CharField(
        max_length=200,
        help_text="Descriptive name like 'Fall 2020 Bachelor of Computer Science'"
    )
    program = models.CharField(
        max_length=100,
        help_text="Program name like 'Computer Science', 'Software Engineering', etc."
    )
    degree_level = models.CharField(
        max_length=50,
        help_text="Bachelor's, Master's, PhD, etc."
    )
    start_year = models.IntegerField(help_text="Year the batch started")
    end_year = models.IntegerField(help_text="Expected graduation year")
    
    # Track batch status
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this batch is currently active"
    )

    def __str__(self):
        return f"{self.batch_code} - {self.batch_name}"
    
    class Meta:
        ordering = ['-start_year', 'batch_code']
        verbose_name_plural = "Batches"

class Section(models.Model):
    """Represents a section within a batch (e.g., Section A, Section B)"""
    
    batch = models.ForeignKey(
        Batch,
        on_delete=models.CASCADE,
        related_name='sections',
        help_text="The batch this section belongs to"
    )
    section_name = models.CharField(
        max_length=50,
        help_text="Section identifier like 'A', 'B', 'Gold', 'Silver'"
    )
    max_students = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum number of students allowed in this section"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this section is currently active"
    )

    def __str__(self):
        return f"{self.batch.batch_code} - Section {self.section_name}"
    
    @property
    def current_enrollment(self):
        """Get current number of students in this section"""
        return self.students.filter(is_active=True).count()
    
    class Meta:
        unique_together = ('batch', 'section_name')
        ordering = ['batch', 'section_name']
        verbose_name = 'Section'
        verbose_name_plural = 'Sections'

class Course(models.Model):
    """Represents a course/subject template (e.g., Introduction to Python, Data Structures)"""
    
    class CourseTypeChoices(models.TextChoices):
        CORE = 'CORE', 'Core Course'
        ELECTIVE = 'ELEC', 'Elective Course'
        LAB = 'LAB', 'Lab Course'
        PROJECT = 'PROJ', 'Project/Thesis'
    
    course_code = models.CharField(
        max_length=20,
        unique=True,
        help_text="Course code like 'CS101', 'MATH201'"
    )
    title = models.CharField(max_length=200, help_text="Course title like 'Introduction to Python'")
    description = models.TextField(blank=True, help_text="Detailed course description")
    credit_hours = models.PositiveSmallIntegerField(
        default=3,
        help_text="Number of credit hours for this course"
    )
    
    # NEW: Course type to identify electives
    course_type = models.CharField(
        max_length=4,
        choices=CourseTypeChoices.choices,
        default=CourseTypeChoices.CORE,
        help_text="Type of course (Core/Elective/Lab/Project)"
    )
    
    # Course categorization
    department = models.CharField(max_length=100, blank=True, help_text="Department offering this course")
    prerequisite_courses = models.ManyToManyField(
        'self',
        blank=True,
        symmetrical=False,
        help_text="Courses that must be completed before taking this course"
    )

    def __str__(self):
        return f"{self.course_code} - {self.title} ({self.get_course_type_display()})"
    
    @property
    def is_elective(self):
        """Check if this is an elective course"""
        return self.course_type == self.CourseTypeChoices.ELECTIVE
    
    class Meta:
        ordering = ['course_code']

class Class(models.Model):
    """Represents a specific instance of a course being taught"""
    
    # The core relationship: each class links a course
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='classes',
        help_text="The course being taught"
    )
    
    # CHANGED: Now supports multiple batches
    batches = models.ManyToManyField(
        Batch,
        related_name='classes',
        help_text="The batches taking this class",
        blank=True  # Allow empty initially for migration
    )
    
    # DEPRECATED: Keep for backward compatibility, will be removed in future migration
    batch = models.ForeignKey(
        Batch,
        on_delete=models.CASCADE,
        related_name='legacy_classes',
        help_text="DEPRECATED: Use 'batches' field instead",
        null=True,
        blank=True
    )
    
    # NEW: Sections that are taking this class (optional, for section-specific classes)
    sections = models.ManyToManyField(
        Section,
        related_name='classes',
        blank=True,
        help_text="Specific sections taking this class (leave empty for whole batch)"
    )
    
    # Instructor assignment - many-to-many because multiple instructors might teach one class
    instructors = models.ManyToManyField(
        Teacher,
        related_name='classes',
        help_text="Instructor(s) teaching this class"
    )
    
    # Class scheduling information
    semester = models.CharField(
        max_length=20,
        help_text="Semester like 'Fall 2024', 'Spring 2025'"
    )
    academic_year = models.CharField(
        max_length=10,
        help_text="Academic year like '2024-25'"
    )
    
    # Class timing and location
    class_days = models.CharField(
        max_length=20,
        blank=True,
        help_text="Days of week like 'Mon,Wed,Fri' or 'Tue,Thu'"
    )
    start_time = models.TimeField(null=True, blank=True, help_text="Class start time")
    end_time = models.TimeField(null=True, blank=True, help_text="Class end time")
    classroom = models.CharField(max_length=50, blank=True, help_text="Room number or location")
    
    # RTSP stream configuration
    rtsp_stream_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="RTSP stream URL for the classroom camera (e.g., rtsp://username:password@ip:port/stream)",
        verbose_name="RTSP Stream URL"
    )
    
    # NEW: Enrollment limits
    max_enrollment = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum number of students allowed in this class"
    )
    
    # Class status
    is_active = models.BooleanField(default=True, help_text="Whether this class is currently active")
    start_date = models.DateField(null=True, blank=True, help_text="Class start date")
    end_date = models.DateField(null=True, blank=True, help_text="Class end date")

    def __str__(self):
        instructors_names = ", ".join([str(instructor.user.username) for instructor in self.instructors.all()])
        batch_codes = ", ".join([b.batch_code for b in self.batches.all()[:2]])
        if self.batches.count() > 2:
            batch_codes += f" (+{self.batches.count() - 2} more)"
        return f"{self.course.course_code} - {batch_codes or 'No batches'} ({instructors_names})"
    
    @property
    def has_stream(self):
        """Check if this class has an RTSP stream configured"""
        return bool(self.rtsp_stream_url)
    
    @property
    def current_enrollment(self):
        """Get current enrollment count"""
        return self.enrollments.filter(is_active=True).count()
    
    @property
    def is_full(self):
        """Check if class has reached max enrollment"""
        if self.max_enrollment:
            return self.current_enrollment >= self.max_enrollment
        return False
    
    def get_all_eligible_students(self):
        """Get all students eligible for this class (from assigned batches/sections)"""
        students = Student.objects.none()
        
        # Get students from assigned batches
        for batch in self.batches.all():
            students |= batch.students.filter(is_active=True)
        
        # Get students from specific sections if any
        for section in self.sections.all():
            students |= section.students.filter(is_active=True)
        
        return students.distinct()
    
    class Meta:
        ordering = ['semester', 'course__course_code']
        verbose_name_plural = "Classes"
        # Remove unique_together constraint to allow multiple sections



class ClassroomOverride(models.Model):
    """Temporary classroom override for when regular classroom is unavailable"""
    
    class_instance = models.ForeignKey(
        'Class',
        on_delete=models.CASCADE,
        related_name='classroom_overrides'
    )
    
    # Override information
    original_classroom = models.CharField(
        max_length=50,
        help_text="Original classroom that was unavailable"
    )
    temporary_classroom = models.CharField(
        max_length=50,
        help_text="Temporary classroom being used"
    )
    temporary_rtsp_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Temporary RTSP stream URL for the alternate classroom"
    )
    
    # Tracking information
    override_date = models.DateField(
        help_text="Date when this override is/was active"
    )
    reason = models.TextField(
        help_text="Reason for classroom change (e.g., camera malfunction, room unavailable)"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='classroom_overrides_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this override is currently active"
    )
    
    class Meta:
        ordering = ['-override_date', '-created_at']
        unique_together = ('class_instance', 'override_date')
    
    def __str__(self):
        return f"{self.class_instance} - Override on {self.override_date}"

class Student(models.Model):
    """Represents a student enrolled in the system."""
    
    registration_id = models.CharField(
        max_length=50, 
        unique=True, 
        db_index=True,
        help_text="Unique student ID like 'FA20-BCS-001'"
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    
    # Link student to their batch
    batch = models.ForeignKey(
        Batch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students',
        help_text="The batch this student belongs to"
    )
    
    # NEW: Link student to their section (optional)
    section = models.ForeignKey(
        Section,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students',
        help_text="The section this student belongs to within their batch"
    )
    
    email = models.EmailField(unique=True, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True)
    
    # Face recognition data
    face_embedding_file = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Filename of the .npy embedding file located in the ATTENDANCE_MODELS_DIR."
    )
    
    # Student images folder path
    images_folder_path = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Absolute server path to folder containing student's images"
    )
    
    # Student status
    is_active = models.BooleanField(default=True, help_text="Whether student is currently enrolled")
    enrollment_date = models.DateField(null=True, blank=True, help_text="Date of enrollment")

    def __str__(self):
        section_info = f" (Section {self.section.section_name})" if self.section else ""
        return f"{self.first_name} {self.last_name} ({self.registration_id}){section_info}"

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}"

    def get_embedding_path(self):
        """Constructs the full path to the embedding file."""
        if not self.face_embedding_file:
            return None
        models_dir = getattr(settings, 'ATTENDANCE_MODELS_DIR', os.path.join(settings.BASE_DIR, "Attendance", "Models"))
        return os.path.join(models_dir, self.face_embedding_file)
    
    @property
    def has_images_folder(self):
        """Check if the student has an images folder configured and it exists"""
        if not self.images_folder_path:
            return False
        return os.path.exists(self.images_folder_path) and os.path.isdir(self.images_folder_path)
    
    def get_image_files(self, extensions=('.jpg', '.jpeg', '.png', '.bmp')):
        """Get list of image files from the student's folder"""
        if not self.has_images_folder:
            return []
        
        image_files = []
        for filename in os.listdir(self.images_folder_path):
            if filename.lower().endswith(extensions):
                image_files.append(os.path.join(self.images_folder_path, filename))
        return sorted(image_files)
    
    def get_enrolled_classes(self, semester=None, is_active=True):
        """Get all classes this student is enrolled in"""
        enrollments = self.enrollments.all()
        if is_active is not None:
            enrollments = enrollments.filter(is_active=is_active)
        if semester:
            enrollments = enrollments.filter(class_instance__semester=semester)
        return [e.class_instance for e in enrollments]
    
    class Meta:
        ordering = ['last_name', 'first_name']

class Enrollment(models.Model):
    """Tracks individual student enrollment in specific classes"""
    
    class EnrollmentTypeChoices(models.TextChoices):
        REGULAR = 'REG', 'Regular Enrollment'
        REPEAT = 'REP', 'Repeat/Improvement'
        AUDIT = 'AUD', 'Audit Only'
        TRANSFER = 'TRA', 'Transfer Credit'
    
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='enrollments',
        help_text="The student enrolled in the class"
    )
    class_instance = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='enrollments',
        help_text="The class the student is enrolled in"
    )
    
    # NEW: Type of enrollment
    enrollment_type = models.CharField(
        max_length=3,
        choices=EnrollmentTypeChoices.choices,
        default=EnrollmentTypeChoices.REGULAR,
        help_text="Type of enrollment (Regular/Repeat/Audit/Transfer)"
    )
    
    # Enrollment tracking
    enrollment_date = models.DateTimeField(
        auto_now_add=True,
        help_text="When the student was enrolled in this class"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this enrollment is currently active"
    )
    
    # Grade tracking (optional)
    final_grade = models.CharField(
        max_length=5,
        blank=True,
        null=True,
        help_text="Final grade for this enrollment (e.g., 'A', 'B+', etc.)"
    )
    
    # Notes
    notes = models.TextField(
        blank=True,
        help_text="Additional notes about this enrollment"
    )

    def __str__(self):
        return f"{self.student.registration_id} - {self.class_instance.course.course_code} ({self.get_enrollment_type_display()})"
    
    class Meta:
        unique_together = ('student', 'class_instance')
        ordering = ['-enrollment_date']
        verbose_name = 'Enrollment'
        verbose_name_plural = 'Enrollments'

class AttendanceRecord(models.Model):
    """Represents a single attendance entry for a student in a specific class."""
    
    class StatusChoices(models.TextChoices):
        PRESENT = 'P', 'Present'
        ABSENT = 'A', 'Absent'
        LATE = 'L', 'Late'
        EXCUSED = 'E', 'Excused'

    student = models.ForeignKey(
        Student, 
        on_delete=models.CASCADE, 
        related_name='attendance_records'
    )
    class_instance = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='attendance_records',
        help_text="The specific class instance this attendance is for"
    )
    
    attendance_date = models.DateField(default=timezone.now, db_index=True)
    attendance_time = models.TimeField(null=True, blank=True, help_text="Time marking occurred")
    status = models.CharField(
        max_length=1,
        choices=StatusChoices.choices,
        default=StatusChoices.PRESENT
    )
    marked_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='marked_attendance'
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # Additional fields for better tracking
    notes = models.TextField(blank=True, help_text="Additional notes about this attendance record")

    class Meta:
        # Ensure only one record per student, per class, per day
        unique_together = ('student', 'class_instance', 'attendance_date')
        ordering = ['-attendance_date', '-attendance_time', 'student__last_name']

    def __str__(self):
        return f"{self.student} - {self.class_instance} on {self.attendance_date} ({self.get_status_display()})"


# ═══════════════════════════════════════════════════════════════════
# FACE PIPELINE MODELS (Modular Add-on)
# ═══════════════════════════════════════════════════════════════════

class ExtractionSession(models.Model):
    """Tracks a face extraction event (from stream capture or image upload)."""

    class SourceChoices(models.TextChoices):
        STREAM = 'STREAM', 'RTSP Stream'
        UPLOAD = 'UPLOAD', 'Uploaded Image'

    class_instance = models.ForeignKey(
        Class, on_delete=models.CASCADE, related_name='extraction_sessions'
    )
    source_type = models.CharField(max_length=6, choices=SourceChoices.choices)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    frames_captured = models.PositiveIntegerField(default=0)
    faces_extracted = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, default='pending',
        help_text="pending / processing / completed / failed"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Extraction {self.pk} - {self.class_instance} ({self.created_at:%Y-%m-%d %H:%M})"


class Identity(models.Model):
    """An individual face identity. Initially unlabeled, then mapped to a Student."""

    class_instance = models.ForeignKey(
        Class, on_delete=models.CASCADE, related_name='identities'
    )
    student = models.ForeignKey(
        Student, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='identities',
        help_text="Linked after human labeling"
    )
    label = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Human-assigned label (student name or temp identifier)"
    )
    auto_label = models.CharField(
        max_length=50,
        help_text="Auto-generated label like 'identity_001'"
    )
    embedding_file = models.CharField(
        max_length=500, blank=True,
        help_text="Path to the aggregated .npy embedding file"
    )
    representative_image = models.CharField(
        max_length=500, blank=True,
        help_text="Path to the best sample image for human reference"
    )
    sample_count = models.PositiveIntegerField(default=0)
    is_labeled = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    needs_retraining = models.BooleanField(
        default=False, help_text="Flagged when samples change and embedding needs regeneration"
    )

    class Meta:
        ordering = ['class_instance', 'auto_label']
        unique_together = ('class_instance', 'auto_label')

    def __str__(self):
        name = self.label or self.auto_label
        status = "labeled" if self.is_labeled else "unlabeled"
        return f"{name} ({status}, {self.sample_count} samples)"

    @property
    def display_name(self):
        return self.label if self.label else self.auto_label

    @property
    def folder_path(self):
        """Directory holding this identity's face samples."""
        base = os.path.join(
            settings.MEDIA_ROOT, 'face_pipeline',
            f'class_{self.class_instance_id}', self.auto_label
        )
        return base

    def get_sample_images(self):
        """Return list of image file paths for this identity."""
        folder = self.folder_path
        if not os.path.isdir(folder):
            return []
        exts = ('.jpg', '.jpeg', '.png')
        return sorted([
            os.path.join(folder, f) for f in os.listdir(folder)
            if f.lower().endswith(exts) and not f.startswith('.')
        ])


class FaceSample(models.Model):
    """Individual face crop belonging to an Identity."""

    identity = models.ForeignKey(
        Identity, on_delete=models.CASCADE, related_name='samples'
    )
    session = models.ForeignKey(
        ExtractionSession, on_delete=models.SET_NULL, null=True, blank=True
    )
    image_path = models.CharField(max_length=500, help_text="Absolute path to face crop image")
    embedding_path = models.CharField(max_length=500, blank=True, help_text="Path to individual .npy embedding")
    quality_score = models.FloatField(default=0.0, help_text="Face quality score (0-100)")
    is_valid = models.BooleanField(default=True, help_text="Marked False if human flags as bad sample")
    source_frame = models.CharField(max_length=500, blank=True, help_text="Source frame filename")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-quality_score', '-created_at']

    def __str__(self):
        return f"Sample for {self.identity.display_name} (q={self.quality_score:.0f})"

    @property
    def image_url(self):
        """Convert absolute path to a media URL."""
        media_root = str(settings.MEDIA_ROOT)
        img_path = str(self.image_path) if self.image_path else ''
        if img_path and img_path.startswith(media_root):
            relative = img_path[len(media_root):].replace(os.sep, '/').lstrip('/')
            return f"{settings.MEDIA_URL}{relative}"
        return ''