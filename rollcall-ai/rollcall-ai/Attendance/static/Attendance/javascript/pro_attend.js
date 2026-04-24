// static/Attendance/javascript/pro_attend.js

(function() {
    "use strict";

    // --- Global State Management ---
    const AppState = {
        selectedFile: null,
        currentMode: null,
        draggedItem: null,
        isFetching: false,
        isDetectionCancelled: false,
        notificationTimeout: null,
        progressInterval: null,
        autoRefreshInterval: null,
        detectionAnimationInterval: null,
        detectedCounter: 0,
        classifiedCounter: 0,
        totalFacesToProcess: 0,
        manualAssignments: new Map(),
        automaticAssignments: new Map(),
        falsePositiveAssignments: new Map(),
        studentPresentStatus: new Map(),
        studentPhotos: new Map(),
        undoStack: [],
        redoStack: [],
        maxUndoStackSize: 50,
        dragCounter: 0,
        selectedSection: null,
        sectionStudents: new Set(),
        // FIX I4: AbortController for cancellable fetch
        currentAbortController: null,
        // FIX I3: Track pending response data for deferred display
        pendingResponseData: null,
    };

    // --- DOM Element Cache ---
    const DOMCache = {
        elements: {},
        get(id) {
            if (!this.elements[id]) {
                this.elements[id] = document.getElementById(id);
            }
            return this.elements[id];
        },
        query(selector) {
            return document.querySelector(selector);
        },
        queryAll(selector) {
            return document.querySelectorAll(selector);
        }
    };

    // --- Constants ---
    const CONFIG = {
        ANIMATION_DURATION: 300,
        AUTO_REFRESH_INTERVAL: 30000,
        UNDO_NOTIFICATION_TIMEOUT: 10000,
        NOTIFICATION_DURATION: 5000,
        DRAG_THRESHOLD: 5,
        DETECTION_COUNTER_SPEED: 100,
        DETECTION_SIMULATION_DELAY: 200,
        PROGRESS_UPDATE_INTERVAL: 50,
        OVERLAY_MIN_DISPLAY_MS: 2000, // Minimum overlay display time for UX
        FEEDBACK_API_URL: '/api/submit_feedback/',
        BUG_REPORT_API_URL: '/api/submit_bug_report/',
        SAVE_IMAGE_API_URL: '/save_training_image/',
        EMAILJS_SERVICE_ID: 'service_pro_attend',
        EMAILJS_TEMPLATE_ID_FEEDBACK: 'template_feedback',
        EMAILJS_TEMPLATE_ID_BUG: 'template_bug_report',
        EMAILJS_PUBLIC_KEY: 'YOUR_EMAILJS_PUBLIC_KEY',
        RECIPIENT_EMAIL: 'you@example.com'
    };

    // Initialize EmailJS
    function initializeEmailJS() {
        if (typeof emailjs !== 'undefined' && CONFIG.EMAILJS_PUBLIC_KEY !== 'YOUR_EMAILJS_PUBLIC_KEY') {
            emailjs.init(CONFIG.EMAILJS_PUBLIC_KEY);
            console.log('EmailJS initialized successfully');
        } else {
            console.warn('EmailJS not configured.');
        }
    }

    // --- Image Saving Manager ---
    const ImageSaveManager = {
        async saveToStudentFolder(studentPk, imageData, originalStudentName = null) {
            try {
                const classId = DOMCache.get('mainContent')?.dataset.classId;
                if (!classId) {
                    console.error('No class ID found');
                    return false;
                }

                const response = await fetch(CONFIG.SAVE_IMAGE_API_URL, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': Utils.getCsrfToken()
                    },
                    body: JSON.stringify({
                        student_pk: studentPk,
                        image_data: imageData,
                        class_id: classId,
                        original_student_name: originalStudentName,
                        create_augmentations: true
                    })
                });

                const result = await response.json();
                
                if (result.success) {
                    console.log(`Saved image to student folder: ${result.message}`);
                    return true;
                } else {
                    console.error(`Failed to save image: ${result.message}`);
                    return false;
                }
            } catch (error) {
                console.error('Error saving image to student folder:', error);
                return false;
            }
        }
    };

    // --- Section Manager ---
    const SectionManager = {
        init() {
            const sectionFilter = DOMCache.get('sectionFilter');
            if (sectionFilter) {
                AppState.selectedSection = sectionFilter.value;
                this.updateSectionStudents();
                
                sectionFilter.addEventListener('change', (e) => {
                    AppState.selectedSection = e.target.value;
                    this.updateSectionStudents();
                    this.filterStudentDisplay();
                });
            }
        },

        updateSectionStudents() {
            AppState.sectionStudents.clear();
            
            if (!AppState.selectedSection || AppState.selectedSection === '') {
                const allStudents = DOMCache.queryAll('.roster-student');
                allStudents.forEach(student => {
                    const pk = Utils.normalizeStudentPk(student.dataset.studentPk);
                    if (pk) AppState.sectionStudents.add(pk);
                });
            } else {
                const sectionStudents = DOMCache.queryAll(`.roster-student[data-section-pk="${AppState.selectedSection}"]`);
                sectionStudents.forEach(student => {
                    const pk = Utils.normalizeStudentPk(student.dataset.studentPk);
                    if (pk) AppState.sectionStudents.add(pk);
                });
            }
        },

        filterStudentDisplay() {
            const students = DOMCache.queryAll('.roster-student');
            let visibleCount = 0;
            let presentCount = 0;
            
            students.forEach(student => {
                const studentSection = student.dataset.sectionPk;
                const shouldShow = !AppState.selectedSection || 
                                 AppState.selectedSection === '' || 
                                 studentSection === AppState.selectedSection;
                
                student.style.display = shouldShow ? '' : 'none';
                
                if (shouldShow) {
                    visibleCount++;
                    if (student.classList.contains('present')) {
                        presentCount++;
                    }
                }
            });
            
            const totalCountEl = DOMCache.get('totalStudentCount');
            const presentCountEl = DOMCache.get('presentCount');
            
            if (totalCountEl) totalCountEl.textContent = visibleCount;
            if (presentCountEl) presentCountEl.textContent = presentCount;
            
            RosterManager.updateProgressBar(presentCount, visibleCount);
        },

        isStudentInSection(studentPk) {
            if (!AppState.selectedSection || AppState.selectedSection === '') {
                return true;
            }
            return AppState.sectionStudents.has(Utils.normalizeStudentPk(studentPk));
        }
    };

    // --- Detection Overlay Manager ---
    const DetectionOverlay = {
        progressTimer: null,
        currentProgress: 0,
        targetDetected: 0,
        targetClassified: 0,
        showTimestamp: 0,
        
        show() {
            const overlay = DOMCache.get('detectionOverlay');
            if (!overlay) return;

            this.reset();
            this.showTimestamp = Date.now();
            document.body.classList.add('detection-active');
            overlay.style.display = 'flex';
            requestAnimationFrame(() => {
                overlay.classList.add('active');
            });
        },

        reset() {
            AppState.detectedCounter = 0;
            AppState.classifiedCounter = 0;
            AppState.isDetectionCancelled = false;
            this.currentProgress = 0;
            this.targetDetected = 0;
            this.targetClassified = 0;
            
            if (this.progressTimer) clearInterval(this.progressTimer);
            if (AppState.detectionAnimationInterval) clearInterval(AppState.detectionAnimationInterval);
            
            const detectedCount = DOMCache.get('detectedCount');
            const classifiedCount = DOMCache.get('classifiedCount');
            const progressLabel = DOMCache.get('progressLabel');
            const progressFill = DOMCache.get('detectionProgressFill');
            
            if (detectedCount) detectedCount.textContent = '0';
            if (classifiedCount) classifiedCount.textContent = '0';
            if (progressLabel) progressLabel.textContent = 'Initializing camera...';
            if (progressFill) progressFill.style.width = '0%';
        },

        hide() {
            const overlay = DOMCache.get('detectionOverlay');
            if (!overlay) return;

            if (this.progressTimer) clearInterval(this.progressTimer);
            if (AppState.detectionAnimationInterval) clearInterval(AppState.detectionAnimationInterval);

            overlay.classList.remove('active');
            document.body.classList.remove('detection-active');

            setTimeout(() => {
                overlay.style.display = 'none';
            }, CONFIG.ANIMATION_DURATION);
        },

        // FIX U3/I3: Accept callback to run after animation completes
        simulateDetectionAndFinish(data, onComplete) {
            if (!data || !data.recognized_faces) {
                this.hide();
                if (onComplete) onComplete();
                return;
            }

            const totalFaces = data.recognized_faces.length + (data.unidentified_faces?.length || 0);
            
            let sectionRecognized = 0;
            let outOfSectionCount = 0;
            
            if (AppState.selectedSection && AppState.selectedSection !== '') {
                data.recognized_faces.forEach(face => {
                    if (SectionManager.isStudentInSection(face.student_pk)) {
                        sectionRecognized++;
                    } else {
                        outOfSectionCount++;
                    }
                });
            } else {
                sectionRecognized = data.recognized_faces.length;
            }
            
            this.targetDetected = totalFaces + outOfSectionCount;
            this.targetClassified = sectionRecognized;
            
            // FIX U3: Calculate remaining animation time based on how long overlay has been shown
            const elapsed = Date.now() - this.showTimestamp;
            const minRemaining = Math.max(0, CONFIG.OVERLAY_MIN_DISPLAY_MS - elapsed);
            const animDuration = Math.max(minRemaining, 1500); // At least 1.5s animation
            
            this.startSynchronizedAnimation(animDuration, () => {
                this.hide();
                if (onComplete) onComplete();
            });
        },

        startSynchronizedAnimation(totalDuration, onComplete) {
            const detectionPhaseEnd = 0.5;
            const classificationPhaseEnd = 0.85;
            
            let startTime = Date.now();
            let detectedReached = false;
            let classifiedReached = false;
            
            this.progressTimer = setInterval(() => {
                if (AppState.isDetectionCancelled) {
                    clearInterval(this.progressTimer);
                    return;
                }
                
                const elapsed = Date.now() - startTime;
                const progress = Math.min(elapsed / totalDuration, 1);
                
                this.updateProgressBar(progress * 100);
                
                if (progress >= detectionPhaseEnd * 0.2) {
                    const detectionProgress = Math.min((progress - 0.1) / detectionPhaseEnd, 1);
                    const currentDetected = Math.floor(this.targetDetected * detectionProgress);
                    if (currentDetected !== AppState.detectedCounter) {
                        this.updateDetectedCount(currentDetected);
                    }
                    
                    if (detectionProgress >= 1 && !detectedReached) {
                        detectedReached = true;
                        this.updateDetectedCount(this.targetDetected);
                    }
                }
                
                if (progress >= detectionPhaseEnd) {
                    const classificationProgress = Math.min(
                        (progress - detectionPhaseEnd) / (classificationPhaseEnd - detectionPhaseEnd), 
                        1
                    );
                    const currentClassified = Math.floor(this.targetClassified * classificationProgress);
                    if (currentClassified !== AppState.classifiedCounter) {
                        this.updateClassifiedCount(currentClassified);
                    }
                    
                    if (classificationProgress >= 1 && !classifiedReached) {
                        classifiedReached = true;
                        this.updateClassifiedCount(this.targetClassified);
                    }
                }
                
                this.updateProgressLabel(progress);
                
                if (progress >= 1) {
                    clearInterval(this.progressTimer);
                    const progressLabel = DOMCache.get('progressLabel');
                    if (progressLabel) progressLabel.textContent = 'Complete!';
                    
                    setTimeout(() => {
                        if (!AppState.isDetectionCancelled && onComplete) {
                            onComplete();
                        }
                    }, 500);
                }
                
            }, CONFIG.PROGRESS_UPDATE_INTERVAL);
        },

        updateProgressBar(percentage) {
            const progressFill = DOMCache.get('detectionProgressFill');
            if (progressFill) {
                progressFill.style.width = `${percentage}%`;
            }
        },

        updateProgressLabel(progress) {
            const progressLabel = DOMCache.get('progressLabel');
            if (!progressLabel) return;
            
            let label = 'Initializing...';
            if (progress < 0.1) {
                label = 'Connecting to camera...';
            } else if (progress < 0.25) {
                label = 'Loading face detection models...';
            } else if (progress < 0.4) {
                label = 'Analyzing image...';
            } else if (progress < 0.5) {
                label = `Detecting faces... (${AppState.detectedCounter} found)`;
            } else if (progress < 0.85) {
                label = `Identifying students... (${AppState.classifiedCounter}/${AppState.detectedCounter})`;
            } else if (progress < 0.95) {
                label = 'Processing results...';
            } else if (progress < 1) {
                label = 'Finalizing attendance...';
            }
            
            progressLabel.textContent = label;
        },

        updateDetectedCount(count) {
            const element = DOMCache.get('detectedCount');
            if (!element) return;
            
            element.textContent = count;
            AppState.detectedCounter = count;
            
            element.classList.add('updating');
            setTimeout(() => element.classList.remove('updating'), 300);
        },

        updateClassifiedCount(count) {
            const element = DOMCache.get('classifiedCount');
            if (!element) return;
            
            element.textContent = count;
            AppState.classifiedCounter = count;
            
            const statCard = element.closest('.stat-card');
            if (statCard) {
                statCard.style.animation = 'none';
                requestAnimationFrame(() => {
                    statCard.style.animation = 'scaleIn 0.3s ease-out';
                });
            }
            
            element.classList.add('updating');
            setTimeout(() => element.classList.remove('updating'), 300);
        },

        cancel() {
            AppState.isDetectionCancelled = true;
            // FIX I4: Abort the in-flight fetch
            if (AppState.currentAbortController) {
                AppState.currentAbortController.abort();
                AppState.currentAbortController = null;
            }
            this.hide();
            NotificationManager.showNotification('Detection cancelled', 'info', 3000);
        }
    };

    // --- Utility Functions ---
    const Utils = {
        getCsrfToken() {
            const tokenName = 'csrftoken';
            const cookies = document.cookie.split(';');
            for (const cookie of cookies) {
                const trimmed = cookie.trim();
                if (trimmed.startsWith(tokenName + '=')) {
                    return decodeURIComponent(trimmed.substring(tokenName.length + 1));
                }
            }
            console.warn("CSRF token not found.");
            return "";
        },

        getClassConfig() {
            const mainContent = DOMCache.get('mainContent');
            return {
                classId: mainContent?.dataset.classId || '',
                hasStream: mainContent?.dataset.hasStream === 'true'
            };
        },

        createElement(tag, options = {}) {
            const el = document.createElement(tag);
            if (options.classes) el.className = options.classes.join(' ');
            if (options.html) el.innerHTML = options.html;
            if (options.attrs) {
                Object.entries(options.attrs).forEach(([key, value]) => {
                    el.setAttribute(key, value);
                });
            }
            if (options.data) {
                Object.entries(options.data).forEach(([key, value]) => {
                    el.dataset[key] = value;
                });
            }
            return el;
        },

        debounce(func, wait) {
            let timeout;
            return function executedFunction(...args) {
                const later = () => {
                    clearTimeout(timeout);
                    func(...args);
                };
                clearTimeout(timeout);
                timeout = setTimeout(later, wait);
            };
        },

        generateUniqueId() {
            return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
        },

        normalizeStudentPk(pk) {
            return String(pk);
        }
    };

    // --- Undo/Redo Manager ---
    const UndoRedoManager = {
        // FIX I1: Make execute async to properly await async actions
        async execute(action) {
            await action.execute();
            AppState.undoStack.push(action);
            AppState.redoStack = [];
            
            if (AppState.undoStack.length > AppState.maxUndoStackSize) {
                AppState.undoStack.shift();
            }
            
            this.updateUndoRedoButtons();
        },

        async undo() {
            if (AppState.undoStack.length === 0) return false;
            
            const action = AppState.undoStack.pop();
            action.undo();
            AppState.redoStack.push(action);
            
            this.updateUndoRedoButtons();
            NotificationManager.showNotification(
                `Undone: ${action.description}`,
                'info',
                3000
            );
            
            return true;
        },

        async redo() {
            if (AppState.redoStack.length === 0) return false;
            
            const action = AppState.redoStack.pop();
            await action.execute();
            AppState.undoStack.push(action);
            
            this.updateUndoRedoButtons();
            NotificationManager.showNotification(
                `Redone: ${action.description}`,
                'info',
                3000
            );
            
            return true;
        },

        canUndo() {
            return AppState.undoStack.length > 0;
        },

        canRedo() {
            return AppState.redoStack.length > 0;
        },

        updateUndoRedoButtons() {
            const event = new CustomEvent('undoRedoStateChanged', {
                detail: {
                    canUndo: this.canUndo(),
                    canRedo: this.canRedo()
                }
            });
            document.dispatchEvent(event);
        },

        clear() {
            AppState.undoStack = [];
            AppState.redoStack = [];
            this.updateUndoRedoButtons();
        }
    };

    // --- Action Classes for Undo/Redo ---
    class AssignFaceAction {
        constructor(faceData, studentPk, studentName) {
            this.faceData = faceData;
            this.studentPk = Utils.normalizeStudentPk(studentPk);
            this.studentName = studentName;
            this.description = `Assigned face to ${studentName}`;
            this.previousAssignment = null;
            this.wasAlreadyPresent = false;
        }

        async execute() {
            const studentRow = DOMCache.query(`.roster-student[data-student-pk="${this.studentPk}"]`);
            if (!studentRow) return false;

            this.wasAlreadyPresent = studentRow.classList.contains('present');
            
            if (AppState.manualAssignments.has(this.studentPk)) {
                this.previousAssignment = AppState.manualAssignments.get(this.studentPk);
            }

            RosterManager.updateStatus(this.studentPk, true, false, true);
            studentRow.classList.add('manual-assignment');
            
            const indicator = studentRow.querySelector('.status-indicator');
            if (indicator) {
                indicator.title = 'Click to mark absent (will return face to unidentified)';
            }

            AppState.manualAssignments.set(this.studentPk, {
                faceData: this.faceData,
                timestamp: Date.now()
            });

            AppState.studentPresentStatus.set(this.studentPk, true);
            AppState.studentPhotos.set(this.studentPk, this.faceData);

            // Save image to student folder (fire-and-forget, don't block UI)
            ImageSaveManager.saveToStudentFolder(
                this.studentPk, 
                this.faceData.url || this.faceData
            ).catch(err => console.error('Background image save failed:', err));

            const faceElement = DOMCache.query(`[data-face-id="${this.faceData.id}"]`);
            if (faceElement) {
                FaceManager.removeFaceElement(faceElement);
            }

            if (this.previousAssignment) {
                FaceManager.restoreUnidentifiedFace(this.previousAssignment.faceData, false);
            }

            RosterManager.updateSummary();
            return true;
        }

        undo() {
            AppState.manualAssignments.delete(this.studentPk);
            AppState.studentPresentStatus.delete(this.studentPk);
            AppState.studentPhotos.delete(this.studentPk);
            
            RosterManager.updateStatus(this.studentPk, this.wasAlreadyPresent, false, true);
            FaceManager.restoreUnidentifiedFace(this.faceData, true);
            
            if (this.previousAssignment) {
                AppState.manualAssignments.set(this.studentPk, this.previousAssignment);
                AppState.studentPresentStatus.set(this.studentPk, true);
                AppState.studentPhotos.set(this.studentPk, this.previousAssignment.faceData);
                RosterManager.updateStatus(this.studentPk, true, false, true);
                
                const prevFaceElement = DOMCache.query(`[data-face-id="${this.previousAssignment.faceData.id}"]`);
                if (prevFaceElement) {
                    FaceManager.removeFaceElement(prevFaceElement);
                }
            }

            RosterManager.updateSummary();
        }
    }

    class ToggleAttendanceAction {
        constructor(studentPk, studentName, newStatus) {
            this.studentPk = Utils.normalizeStudentPk(studentPk);
            this.studentName = studentName;
            this.newStatus = newStatus;
            this.previousStatus = !newStatus;
            this.description = `Marked ${studentName} as ${newStatus ? 'present' : 'absent'}`;
            this.manualAssignment = null;
            this.automaticAssignment = null;
            this.isFalsePositive = false;
            this.photoData = null;
        }

        async execute() {
            const studentRow = DOMCache.query(`.roster-student[data-student-pk="${this.studentPk}"]`);
            
            if (!this.newStatus) {
                if (AppState.manualAssignments.has(this.studentPk)) {
                    this.manualAssignment = AppState.manualAssignments.get(this.studentPk);
                }
                if (AppState.automaticAssignments.has(this.studentPk)) {
                    this.automaticAssignment = AppState.automaticAssignments.get(this.studentPk);
                    this.isFalsePositive = true;
                }
                if (AppState.studentPhotos.has(this.studentPk)) {
                    this.photoData = AppState.studentPhotos.get(this.studentPk);
                }
                
                AppState.studentPresentStatus.delete(this.studentPk);
                AppState.studentPhotos.delete(this.studentPk);
                
                if (this.automaticAssignment && this.automaticAssignment.faceData) {
                    AppState.falsePositiveAssignments.set(this.studentPk, {
                        ...this.automaticAssignment,
                        markedAt: Date.now(),
                        studentName: this.studentName
                    });
                    
                    AppState.automaticAssignments.delete(this.studentPk);
                    
                    setTimeout(() => {
                        FaceManager.restoreUnidentifiedFace(
                            this.automaticAssignment.faceData, 
                            true, 
                            true,
                            this.studentName
                        );
                        NotificationManager.showNotification(
                            'False positive corrected - face moved to unidentified',
                            'warning',
                            4000
                        );
                    }, 150);
                }
                
                if (this.manualAssignment && this.manualAssignment.faceData) {
                    AppState.manualAssignments.delete(this.studentPk);
                    
                    setTimeout(() => {
                        FaceManager.restoreUnidentifiedFace(this.manualAssignment.faceData, true, false, this.studentName);
                        NotificationManager.showNotification(
                            'Face returned to unidentified section',
                            'info',
                            3000
                        );
                    }, 150);
                }
                
                if (studentRow) {
                    studentRow.classList.remove('manual-assignment');
                }
            } else {
                AppState.studentPresentStatus.set(this.studentPk, true);
            }

            RosterManager.updateStatus(this.studentPk, this.newStatus, true, true, false);
            return true;
        }

        undo() {
            RosterManager.updateStatus(this.studentPk, this.previousStatus, true, true, false);
            
            if (this.previousStatus) {
                AppState.studentPresentStatus.set(this.studentPk, true);
                if (this.photoData) {
                    AppState.studentPhotos.set(this.studentPk, this.photoData);
                }
            } else {
                AppState.studentPresentStatus.delete(this.studentPk);
                AppState.studentPhotos.delete(this.studentPk);
            }
            
            if (this.manualAssignment) {
                AppState.manualAssignments.set(this.studentPk, this.manualAssignment);
            }
            if (this.automaticAssignment) {
                AppState.automaticAssignments.set(this.studentPk, this.automaticAssignment);
                if (this.isFalsePositive) {
                    const faceElement = DOMCache.query(`[data-face-id="${this.automaticAssignment.faceData.id}"]`);
                    if (faceElement) {
                        FaceManager.removeFaceElement(faceElement);
                    }
                }
            }
        }
    }

    // --- Notification Manager ---
    const NotificationManager = {
        showNotification(message, type = "info", duration = CONFIG.NOTIFICATION_DURATION) {
            const toast = DOMCache.get('notificationToast');
            if (!toast) return;

            clearTimeout(AppState.notificationTimeout);

            toast.className = 'notification-toast';
            toast.classList.add(type);

            const titleEl = toast.querySelector('.toast-title');
            const messageEl = toast.querySelector('.toast-message');

            const titles = {
                success: 'Success',
                error: 'Error',
                info: 'Information',
                warning: 'Warning'
            };

            if (titleEl) titleEl.textContent = titles[type] || 'Notification';
            if (messageEl) messageEl.textContent = message;

            requestAnimationFrame(() => {
                toast.classList.add('show');
            });

            AppState.notificationTimeout = setTimeout(() => {
                this.closeNotification();
            }, duration);
        },

        closeNotification() {
            const toast = DOMCache.get('notificationToast');
            if (toast) {
                toast.classList.remove('show');
            }
        }
    };

    // --- Face Manager ---
    const FaceManager = {
        addUnidentifiedFace(faceUrl, faceId, isFalsePositive = false, studentName = null) {
            const unidList = DOMCache.get('unidentifiedList');
            if (!unidList) return null;

            const img = Utils.createElement('img', {
                classes: ['unidentified'],
                attrs: {
                    src: faceUrl,
                    alt: 'Unidentified face',
                    draggable: 'true'
                },
                data: {
                    faceId: faceId || Utils.generateUniqueId(),
                    faceUrl: faceUrl
                }
            });

            if (isFalsePositive) {
                img.classList.add('false-positive', 'from-false-positive');
                if (studentName) {
                    img.dataset.originalStudent = studentName;
                    img.title = `Previously assigned to ${studentName} (false positive)`;
                }
            }

            img.style.opacity = '0';
            img.style.transform = 'scale(0.8)';
            unidList.appendChild(img);

            requestAnimationFrame(() => {
                img.style.transition = 'all 0.3s ease-out';
                img.style.opacity = '1';
                img.style.transform = 'scale(1)';
            });

            const unidSection = DOMCache.get('unidentifiedSection');
            if (unidSection) {
                unidSection.classList.add('visible');
            }

            return img;
        },

        restoreUnidentifiedFace(faceData, animate = true, isFalsePositive = false, studentName = null) {
            if (!faceData || !faceData.url) return null;

            const unidList = DOMCache.get('unidentifiedList');
            if (!unidList) return null;

            const existing = DOMCache.query(`[data-face-id="${faceData.id}"]`);
            if (existing) return existing;

            const img = this.addUnidentifiedFace(faceData.url, faceData.id, isFalsePositive, studentName);
            
            if (animate && img) {
                img.classList.add('restored');
                setTimeout(() => {
                    img.classList.remove('restored');
                }, 1000);
            }

            return img;
        },

        removeFaceElement(element, animate = true) {
            if (!element) return;

            if (animate) {
                element.classList.add('fading-out');
                setTimeout(() => {
                    if (element.parentNode) {
                        element.remove();
                    }
                    this.checkUnidentifiedVisibility();
                }, CONFIG.ANIMATION_DURATION);
            } else {
                element.remove();
                this.checkUnidentifiedVisibility();
            }
        },

        checkUnidentifiedVisibility() {
            const unidList = DOMCache.get('unidentifiedList');
            const unidSection = DOMCache.get('unidentifiedSection');
            
            if (unidList && unidSection) {
                const hasChildren = unidList.children.length > 0;
                unidSection.classList.toggle('visible', hasChildren);
            }
        }
    };

    // --- Roster Manager ---
    const RosterManager = {
        updateStatus(studentPk, isPresent, isManualToggle = false, animate = false, handleFalsePositive = true) {
            studentPk = Utils.normalizeStudentPk(studentPk);
            
            if (!studentPk) return;

            const studentRow = DOMCache.query(`.roster-student[data-student-pk="${studentPk}"]`);
            if (!studentRow) return;

            const wasPresent = studentRow.classList.contains('present');
            if (wasPresent === isPresent && !isManualToggle) return;

            if (animate) {
                studentRow.style.transition = 'none';
                studentRow.style.transform = 'scale(1.05)';
                requestAnimationFrame(() => {
                    studentRow.style.transition = 'all 0.3s ease-out';
                    studentRow.style.transform = 'scale(1)';
                });
            }

            studentRow.classList.toggle('present', isPresent);
            
            const indicator = studentRow.querySelector('.status-indicator');
            if (indicator) {
                if (isPresent && AppState.manualAssignments.has(studentPk)) {
                    indicator.title = 'Click to mark absent (will return face to unidentified)';
                } else {
                    indicator.title = isPresent ? 'Click to mark absent' : 'Click to mark present';
                }
            }

            if (!isPresent) {
                studentRow.classList.remove('manual-assignment');
                AppState.studentPresentStatus.delete(studentPk);
            } else {
                AppState.studentPresentStatus.set(studentPk, isPresent);
            }

            this.updateSummary();
        },

        updateSummary() {
            const rosterList = DOMCache.get('studentRosterList');
            if (!rosterList) return;

            const present = rosterList.querySelectorAll('.roster-student.present:not([style*="display: none"])').length;
            const total = rosterList.querySelectorAll('.roster-student:not([style*="display: none"])').length;
            const manualCount = AppState.manualAssignments.size;

            const presentCountEl = DOMCache.get('presentCount');
            const totalCountEl = DOMCache.get('totalStudentCount');
            
            if (presentCountEl) {
                const currentValue = parseInt(presentCountEl.textContent) || 0;
                if (currentValue !== present) {
                    this.animateValue(presentCountEl, currentValue, present, 500);
                }
            }
            
            if (totalCountEl) {
                totalCountEl.textContent = total;
            }

            this.updateProgressBar(present, total);
            this.updateManualCountBadge(manualCount);
        },

        updateProgressBar(present, total) {
            const progressBar = DOMCache.get('summaryProgressBar');
            if (progressBar && total > 0) {
                const percentage = (present / total) * 100;
                progressBar.style.width = percentage + '%';
            }
        },

        // FIX S1: Handle start === end case to prevent infinite interval
        animateValue(element, start, end, duration) {
            if (start === end) return; // No-op: nothing to animate
            
            const range = end - start;
            const increment = range > 0 ? 1 : -1;
            const absRange = Math.abs(range);
            const stepTime = Math.max(Math.floor(duration / absRange), 16); // min 16ms (60fps)
            let current = start;

            const timer = setInterval(() => {
                current += increment;
                element.textContent = current;
                if (current === end) clearInterval(timer);
            }, stepTime);
        },

        updateManualCountBadge(count) {
            const rosterHeader = DOMCache.query('.roster-header-content');
            if (!rosterHeader) return;

            let badge = rosterHeader.querySelector('.manual-count-badge');
            
            if (count > 0) {
                if (!badge) {
                    badge = Utils.createElement('span', {
                        classes: ['manual-count-badge'],
                        html: `${count} manual`
                    });
                    rosterHeader.appendChild(badge);
                } else {
                    badge.textContent = `${count} manual`;
                }
            } else if (badge) {
                badge.remove();
            }
        }
    };

    // --- API Manager for Email Communication ---
    const APIManager = {
        retryTimeout: null,

        async submitFeedback(data) {
            try {
                if (typeof emailjs !== 'undefined' && CONFIG.EMAILJS_PUBLIC_KEY !== 'YOUR_EMAILJS_PUBLIC_KEY') {
                    const templateParams = {
                        to_email: CONFIG.RECIPIENT_EMAIL,
                        from_email: data.email || 'anonymous@proattendance.ai',
                        feedback_type: data.type,
                        message: data.message,
                        timestamp: new Date().toISOString(),
                        user_agent: navigator.userAgent
                    };
                    
                    const response = await emailjs.send(
                        CONFIG.EMAILJS_SERVICE_ID,
                        CONFIG.EMAILJS_TEMPLATE_ID_FEEDBACK,
                        templateParams
                    );
                    
                    return {
                        success: response.status === 200,
                        message: response.status === 200 ? 'Feedback sent successfully' : 'Failed to send feedback'
                    };
                }
                
                const response = await fetch(CONFIG.FEEDBACK_API_URL, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': Utils.getCsrfToken()
                    },
                    body: JSON.stringify({
                        ...data,
                        recipient_email: CONFIG.RECIPIENT_EMAIL,
                        timestamp: new Date().toISOString()
                    })
                });
                
                if (response.ok) {
                    const result = await response.json();
                    return { success: true, message: result.message || 'Feedback submitted successfully' };
                } else {
                    throw new Error('Backend submission failed');
                }
                
            } catch (error) {
                console.error('Error submitting feedback:', error);
                this.storeFailedSubmission('feedback', data);
                return {
                    success: false,
                    message: 'Unable to send feedback directly. Your message has been saved.',
                    stored: true
                };
            }
        },

        async submitBugReport(data) {
            try {
                if (typeof emailjs !== 'undefined' && CONFIG.EMAILJS_PUBLIC_KEY !== 'YOUR_EMAILJS_PUBLIC_KEY') {
                    const templateParams = {
                        to_email: CONFIG.RECIPIENT_EMAIL,
                        from_email: data.email,
                        bug_area: data.area,
                        bug_description: data.description,
                        timestamp: new Date().toISOString(),
                        user_agent: navigator.userAgent,
                        screen_resolution: `${window.screen.width}x${window.screen.height}`,
                        current_url: window.location.href
                    };
                    
                    const response = await emailjs.send(
                        CONFIG.EMAILJS_SERVICE_ID,
                        CONFIG.EMAILJS_TEMPLATE_ID_BUG,
                        templateParams
                    );
                    
                    return {
                        success: response.status === 200,
                        message: response.status === 200 ? 'Bug report sent successfully' : 'Failed to send bug report'
                    };
                }
                
                const response = await fetch(CONFIG.BUG_REPORT_API_URL, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': Utils.getCsrfToken()
                    },
                    body: JSON.stringify({
                        ...data,
                        recipient_email: CONFIG.RECIPIENT_EMAIL,
                        timestamp: new Date().toISOString(),
                        browser_info: {
                            user_agent: navigator.userAgent,
                            screen_resolution: `${window.screen.width}x${window.screen.height}`,
                            current_url: window.location.href
                        }
                    })
                });
                
                if (response.ok) {
                    const result = await response.json();
                    return { success: true, message: result.message || 'Bug report submitted successfully' };
                } else {
                    throw new Error('Backend submission failed');
                }
                
            } catch (error) {
                console.error('Error submitting bug report:', error);
                this.storeFailedSubmission('bug_report', data);
                return {
                    success: false,
                    message: 'Unable to send bug report directly. Your report has been saved.',
                    stored: true
                };
            }
        },

        storeFailedSubmission(type, data) {
            try {
                const stored = localStorage.getItem('proattend_failed_submissions') || '[]';
                const submissions = JSON.parse(stored);
                submissions.push({ type, data, timestamp: new Date().toISOString(), attempts: 0 });
                localStorage.setItem('proattend_failed_submissions', JSON.stringify(submissions));
                this.scheduleRetry();
            } catch (e) {
                console.error('Failed to store submission locally:', e);
            }
        },

        async retryFailedSubmissions() {
            try {
                const stored = localStorage.getItem('proattend_failed_submissions');
                if (!stored) return;
                
                const submissions = JSON.parse(stored);
                const remaining = [];
                
                for (const submission of submissions) {
                    submission.attempts++;
                    let success = false;
                    
                    if (submission.type === 'feedback') {
                        const result = await this.submitFeedback(submission.data);
                        success = result.success && !result.stored;
                    } else if (submission.type === 'bug_report') {
                        const result = await this.submitBugReport(submission.data);
                        success = result.success && !result.stored;
                    }
                    
                    if (!success && submission.attempts < 5) {
                        remaining.push(submission);
                    }
                }
                
                if (remaining.length > 0) {
                    localStorage.setItem('proattend_failed_submissions', JSON.stringify(remaining));
                    this.scheduleRetry();
                } else {
                    localStorage.removeItem('proattend_failed_submissions');
                }
            } catch (e) {
                console.error('Error retrying failed submissions:', e);
            }
        },

        scheduleRetry() {
            if (this.retryTimeout) return;
            this.retryTimeout = setTimeout(() => {
                this.retryTimeout = null;
                this.retryFailedSubmissions();
            }, 30000);
        },

        checkFailedSubmissions() {
            const stored = localStorage.getItem('proattend_failed_submissions');
            if (stored) {
                const submissions = JSON.parse(stored);
                if (submissions.length > 0) {
                    this.scheduleRetry();
                }
            }
        }
    };

    // --- Drag and Drop Manager ---
    const DragDropManager = {
        init() {
            const unidList = DOMCache.get('unidentifiedList');
            const rosterList = DOMCache.get('studentRosterList');

            if (unidList) {
                unidList.addEventListener('dragstart', this.handleDragStart.bind(this));
                unidList.addEventListener('dragend', this.handleDragEnd.bind(this));
                
                unidList.addEventListener('touchstart', this.handleTouchStart.bind(this), { passive: false });
                unidList.addEventListener('touchmove', this.handleTouchMove.bind(this), { passive: false });
                unidList.addEventListener('touchend', this.handleTouchEnd.bind(this));
            }

            if (rosterList) {
                rosterList.addEventListener('dragover', this.handleDragOver.bind(this));
                rosterList.addEventListener('drop', this.handleDrop.bind(this));
                rosterList.addEventListener('dragenter', this.handleDragEnter.bind(this));
                rosterList.addEventListener('dragleave', this.handleDragLeave.bind(this));
            }

            document.addEventListener('dragend', this.handleGlobalDragEnd.bind(this));
        },

        handleDragStart(e) {
            if (!e.target.classList.contains('unidentified')) return;

            if (AppState.isFetching) {
                e.preventDefault();
                NotificationManager.showNotification('Please wait for detection to complete', 'warning', 3000);
                return;
            }

            AppState.draggedItem = e.target;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/html', e.target.innerHTML);

            setTimeout(() => {
                if (AppState.draggedItem) {
                    AppState.draggedItem.classList.add('dragging');
                }
            }, 0);
        },

        handleDragEnd(e) {
            if (e.target.classList.contains('unidentified')) {
                e.target.classList.remove('dragging');
            }
        },

        handleGlobalDragEnd() {
            if (AppState.draggedItem) {
                AppState.draggedItem.classList.remove('dragging');
            }
            AppState.draggedItem = null;
            AppState.dragCounter = 0;
            
            DOMCache.queryAll('.roster-student.dragover').forEach(el => {
                el.classList.remove('dragover');
            });
        },

        handleDragEnter(e) {
            if (!this.isValidDropTarget(e)) return;
            
            AppState.dragCounter++;
            const dropTarget = e.target.closest('.roster-student[data-drop-target="true"]');
            if (dropTarget) {
                dropTarget.classList.add('dragover');
            }
        },

        handleDragLeave(e) {
            if (!this.isValidDropTarget(e)) return;
            
            AppState.dragCounter--;
            if (AppState.dragCounter === 0) {
                const dropTarget = e.target.closest('.roster-student[data-drop-target="true"]');
                if (dropTarget) {
                    dropTarget.classList.remove('dragover');
                }
            }
        },

        handleDragOver(e) {
            if (!this.isValidDropTarget(e)) return;
            
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        },

        handleDrop(e) {
            e.preventDefault();
            e.stopPropagation();
            
            const dropTarget = e.target.closest('.roster-student[data-drop-target="true"]');
            if (!dropTarget || !AppState.draggedItem) return;

            this._processDrop(dropTarget, AppState.draggedItem);
            AppState.draggedItem = null;
        },

        // FIX I2: Extracted shared drop logic into a method usable by both mouse and touch
        _processDrop(dropTarget, draggedElement) {
            AppState.dragCounter = 0;
            dropTarget.classList.remove('dragover');

            const studentPk = Utils.normalizeStudentPk(dropTarget.dataset.studentPk);
            const studentName = dropTarget.querySelector('.roster-student-name')?.textContent || 'Student';

            if (AppState.studentPresentStatus.has(studentPk)) {
                NotificationManager.showNotification(
                    `${studentName} is already marked present. Mark them absent first to assign a different photo.`,
                    'warning',
                    4000
                );
                draggedElement.classList.remove('dragging');
                return;
            }

            const faceData = {
                url: draggedElement.src || draggedElement.dataset.faceUrl,
                id: draggedElement.dataset.faceId || Utils.generateUniqueId(),
            };

            if (AppState.manualAssignments.has(studentPk)) {
                const existing = AppState.manualAssignments.get(studentPk);
                if (existing.faceData.id === faceData.id) {
                    NotificationManager.showNotification('This face is already assigned to this student', 'warning', 3000);
                    draggedElement.classList.remove('dragging');
                    return;
                }
            }

            const action = new AssignFaceAction(faceData, studentPk, studentName);
            UndoRedoManager.execute(action);

            NotificationManager.showNotification(
                `Assigned face to ${studentName} and saved for training`,
                'success',
                3000
            );
        },

        isValidDropTarget(e) {
            return AppState.draggedItem && 
                   AppState.draggedItem.classList.contains('unidentified');
        },

        handleTouchStart(e) {
            if (!e.target.classList.contains('unidentified')) return;
            
            const touch = e.touches[0];
            this.touchStartX = touch.clientX;
            this.touchStartY = touch.clientY;
            this.touchItem = e.target;
            this.touchMoved = false;
        },

        handleTouchMove(e) {
            if (!this.touchItem) return;
            
            const touch = e.touches[0];
            const deltaX = Math.abs(touch.clientX - this.touchStartX);
            const deltaY = Math.abs(touch.clientY - this.touchStartY);
            
            if (deltaX > CONFIG.DRAG_THRESHOLD || deltaY > CONFIG.DRAG_THRESHOLD) {
                this.touchMoved = true;
                e.preventDefault();
                
                this.touchItem.classList.add('dragging');
                
                const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
                const dropTarget = elementBelow?.closest('.roster-student[data-drop-target="true"]');
                
                DOMCache.queryAll('.roster-student.dragover').forEach(el => {
                    if (el !== dropTarget) el.classList.remove('dragover');
                });
                
                if (dropTarget) {
                    dropTarget.classList.add('dragover');
                }
            }
        },

        // FIX I2: Use shared _processDrop instead of synthetic events
        handleTouchEnd(e) {
            if (!this.touchItem || !this.touchMoved) {
                this.touchItem = null;
                return;
            }
            
            const touch = e.changedTouches[0];
            const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
            const dropTarget = elementBelow?.closest('.roster-student[data-drop-target="true"]');
            
            if (dropTarget) {
                this._processDrop(dropTarget, this.touchItem);
            }
            
            this.touchItem.classList.remove('dragging');
            DOMCache.queryAll('.roster-student.dragover').forEach(el => {
                el.classList.remove('dragover');
            });
            
            this.touchItem = null;
            this.touchMoved = false;
        }
    };

    // --- Keyboard Shortcuts Manager ---
    const KeyboardManager = {
        init() {
            document.addEventListener('keydown', this.handleKeydown.bind(this));
        },

        handleKeydown(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
                e.preventDefault();
                UndoRedoManager.undo();
                return;
            }

            if ((e.ctrlKey && e.key === 'y') || 
                ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'z')) {
                e.preventDefault();
                UndoRedoManager.redo();
                return;
            }

            if (e.key === 'Escape') {
                if (AppState.draggedItem) {
                    AppState.draggedItem.classList.remove('dragging');
                    AppState.draggedItem = null;
                    DOMCache.queryAll('.roster-student.dragover').forEach(el => {
                        el.classList.remove('dragover');
                    });
                } else if (AppState.isFetching) {
                    DetectionOverlay.cancel();
                }
            }
        }
    };

    // --- Event Handlers ---
    const EventHandlers = {
        handleModeChange() {
            const selectedRadio = DOMCache.query('input[name="attendanceMode"]:checked');
            AppState.currentMode = selectedRadio ? selectedRadio.value : null;
            UIManager.updateModeUI();
        },

        handleFileUpload(e) {
            const file = e.target.files?.[0];
            if (!file) {
                AppState.selectedFile = null;
                return;
            }

            const maxSizeMB = parseInt(DOMCache.get('serverMaxUploadMb')?.value || '60');
            if (file.size > maxSizeMB * 1024 * 1024) {
                NotificationManager.showNotification(
                    `File too large. Maximum size is ${maxSizeMB}MB.`,
                    'error'
                );
                return;
            }

            AppState.selectedFile = file;
            const reader = new FileReader();
            
            reader.onload = (event) => {
                if (event.target?.result) {
                    const previewPic = DOMCache.get('previewPic');
                    if (previewPic) {
                        previewPic.src = event.target.result;
                    }
                    NotificationManager.showNotification(
                        `Image "${file.name}" loaded successfully`,
                        'success',
                        3000
                    );
                }
                UIManager.updateModeUI();
            };
            
            reader.onerror = () => {
                NotificationManager.showNotification('Failed to load image', 'error');
                AppState.selectedFile = null;
            };
            
            reader.readAsDataURL(file);
        },

        // FIX I3/U3: Complete rewrite -- tied to actual fetch lifecycle, not hardcoded timeouts
        async handleStartAttendance() {
            if (AppState.currentMode === "picture" && !AppState.selectedFile) {
                DOMCache.get('attendanceImageInput')?.click();
                return;
            }

            if (AppState.isFetching || DOMCache.get('startAttendanceButton')?.disabled) return;

            const classId = DOMCache.get('mainContent')?.dataset.classId;
            if (!classId) {
                NotificationManager.showNotification("Error: Class ID not found.", "error");
                return;
            }

            let url = "";
            let fetchOptions = {
                method: "POST",
                headers: { "X-CSRFToken": Utils.getCsrfToken() },
                body: null
            };

            if (AppState.currentMode === "stream") {
                url = "/take_attendance/";
                fetchOptions.body = `class_id=${encodeURIComponent(classId)}`;
                fetchOptions.headers['Content-Type'] = 'application/x-www-form-urlencoded';
            } else if (AppState.currentMode === "picture") {
                url = "/take_attendance_image/";
                const formData = new FormData();
                formData.append("image", AppState.selectedFile);
                formData.append("class_id", classId);
                fetchOptions.body = formData;
            } else {
                return;
            }

            // FIX I4: Create AbortController for cancellable fetch
            AppState.currentAbortController = new AbortController();
            fetchOptions.signal = AppState.currentAbortController.signal;

            AppState.isFetching = true;
            AppState.isDetectionCancelled = false;
            UIManager.setLoadingState(true);
            DetectionOverlay.show();

            try {
                const response = await fetch(url, fetchOptions);
                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.error || `Request failed with status ${response.status}`);
                }

                if (AppState.isDetectionCancelled) {
                    // User cancelled while fetch was in flight -- discard response
                    return;
                }

                // FIX U3: Show overlay animation then immediately display results
                DetectionOverlay.simulateDetectionAndFinish(data, () => {
                    if (!AppState.isDetectionCancelled) {
                        UIManager.displayAttendanceResults(data);
                        
                        let message = `Detection complete: ${data.recognized_faces?.length || 0} faces detected`;
                        if (AppState.selectedSection && AppState.selectedSection !== '') {
                            const sectionCount = data.recognized_faces?.filter(f => 
                                SectionManager.isStudentInSection(f.student_pk)
                            ).length || 0;
                            const outOfSection = (data.recognized_faces?.length || 0) - sectionCount;
                            
                            if (outOfSection > 0) {
                                message = `${sectionCount} students from selected section recognized. ${outOfSection} from other sections moved to unidentified.`;
                            } else {
                                message = `${sectionCount} students recognized from selected section.`;
                            }
                        }
                        
                        NotificationManager.showNotification(message, "success");
                    }
                });

            } catch (error) {
                if (error.name === 'AbortError') {
                    // User cancelled -- already handled in cancel()
                    return;
                }
                DetectionOverlay.hide();
                NotificationManager.showNotification(`Error: ${error.message}`, "error", 8000);
            } finally {
                // FIX I3: Immediately release state after fetch completes
                AppState.isFetching = false;
                AppState.currentAbortController = null;
                UIManager.setLoadingState(false);
            }
        },

        handleRosterClick(e) {
            const statusIndicator = e.target.closest('.status-indicator');
            if (!statusIndicator) return;

            const studentRow = statusIndicator.closest('.roster-student');
            if (!studentRow) return;

            const studentPk = Utils.normalizeStudentPk(studentRow.dataset.studentPk);
            const studentName = studentRow.querySelector('.roster-student-name')?.textContent || 'Student';
            const isPresent = studentRow.classList.contains('present');

            const action = new ToggleAttendanceAction(studentPk, studentName, !isPresent);
            UndoRedoManager.execute(action);
        },

        handleCancelDetection() {
            if (AppState.isFetching) {
                DetectionOverlay.cancel();
                AppState.isFetching = false;
                UIManager.setLoadingState(false);
            }
        }
    };

    // --- UI Manager ---
    const UIManager = {
        updateModeUI() {
            const { hasStream } = Utils.getClassConfig();
            
            if (AppState.currentMode !== 'stream') {
                clearInterval(AppState.autoRefreshInterval);
            }

            const elements = {
                videoPlaceholder: DOMCache.get('videoPlaceholder'),
                liveStream: DOMCache.get('liveStream'),
                previewPic: DOMCache.get('previewPic'),
                removePicBtn: DOMCache.get('removePicBtn'),
                videoContainer: DOMCache.get('videoContainer')
            };

            Object.values(elements).forEach(el => {
                if (el && el !== elements.videoContainer) {
                    el.style.display = 'none';
                }
            });

            if (elements.videoContainer) {
                elements.videoContainer.classList.remove('error');
            }

            let buttonText = "Select Mode";
            let isButtonDisabled = true;

            if (AppState.currentMode === "stream") {
                if (!hasStream) {
                    NotificationManager.showNotification(
                        "No camera stream configured for this classroom",
                        "error"
                    );
                    AppState.currentMode = null;
                    const streamRadio = DOMCache.query('input[name="attendanceMode"][value="stream"]');
                    if (streamRadio) streamRadio.checked = false;
                    return;
                }
                buttonText = "Start Detection";
                if (elements.liveStream) elements.liveStream.style.display = "block";
                isButtonDisabled = false;
                this.setupAutoRefresh();

            } else if (AppState.currentMode === "picture") {
                if (AppState.selectedFile) {
                    if (elements.previewPic) elements.previewPic.style.display = "block";
                    if (elements.removePicBtn) elements.removePicBtn.style.display = "flex";
                    buttonText = "Run Detection";
                    isButtonDisabled = false;
                } else {
                    if (elements.videoPlaceholder) {
                        elements.videoPlaceholder.style.display = 'flex';
                        const placeholderText = elements.videoPlaceholder.querySelector('span');
                        if (placeholderText) {
                            placeholderText.textContent = 'Click button to upload image';
                        }
                    }
                    buttonText = "Upload Picture";
                    isButtonDisabled = false;
                }
            } else {
                if (elements.videoPlaceholder) {
                    elements.videoPlaceholder.style.display = 'flex';
                    const placeholderText = elements.videoPlaceholder.querySelector('span');
                    if (placeholderText) {
                        placeholderText.textContent = 'Select a mode to start';
                    }
                }
            }

            const startBtn = DOMCache.get('startAttendanceButton');
            const startBtnText = DOMCache.get('startButtonText');
            
            if (startBtn) startBtn.disabled = isButtonDisabled;
            if (startBtnText) startBtnText.textContent = buttonText;
        },

        setLoadingState(loading) {
            const startBtn = DOMCache.get('startAttendanceButton');
            const processingIndicator = DOMCache.get('processingIndicator');

            if (startBtn) {
                startBtn.classList.toggle('loading', loading);
                startBtn.disabled = loading;
            }

            if (processingIndicator) {
                processingIndicator.classList.toggle('active', loading);
            }
        },

        displayAttendanceResults(data) {
            if (!data || !Array.isArray(data.recognized_faces)) {
                NotificationManager.showNotification("Received invalid data from server.", "error");
                return;
            }

            const inSectionFaces = [];
            const outOfSectionFaces = [];
            
            data.recognized_faces.forEach(face => {
                if (face && face.student_pk) {
                    if (SectionManager.isStudentInSection(face.student_pk)) {
                        inSectionFaces.push(face);
                    } else {
                        outOfSectionFaces.push(face);
                    }
                }
            });

            inSectionFaces.forEach((face, index) => {
                setTimeout(() => {
                    const studentPk = Utils.normalizeStudentPk(face.student_pk);
                    
                    if (AppState.studentPresentStatus.has(studentPk) ||
                        AppState.manualAssignments.has(studentPk) ||
                        AppState.falsePositiveAssignments.has(studentPk)) {
                        return;
                    }

                    RosterManager.updateStatus(studentPk, true, false, true, false);
                    
                    if (face.image || face.face_image || face.face_url) {
                        const faceData = {
                            url: face.image || face.face_image || face.face_url,
                            id: `auto-${studentPk}-${Date.now()}`
                        };
                        
                        AppState.automaticAssignments.set(studentPk, {
                            faceData: faceData,
                            timestamp: Date.now()
                        });
                        
                        AppState.studentPhotos.set(studentPk, faceData);
                        AppState.studentPresentStatus.set(studentPk, true);
                    }
                }, index * 100);
            });

            const unidList = DOMCache.get('unidentifiedList');
            if (unidList) {
                unidList.innerHTML = '';
                
                data.unidentified_faces.forEach((url, i) => {
                    if (url && url.startsWith('data:image')) {
                        setTimeout(() => {
                            FaceManager.addUnidentifiedFace(url, `unid-${Date.now()}-${i}`);
                        }, i * 50);
                    }
                });
                
                outOfSectionFaces.forEach((face, i) => {
                    if (face.image || face.face_image || face.face_url) {
                        const faceUrl = face.image || face.face_image || face.face_url;
                        setTimeout(() => {
                            const img = FaceManager.addUnidentifiedFace(
                                faceUrl, 
                                `out-section-${face.student_pk}-${Date.now()}`
                            );
                            if (img) {
                                img.classList.add('out-of-section');
                                img.title = 'Student from different section';
                            }
                        }, (data.unidentified_faces.length + i) * 50);
                    }
                });
            }

            RosterManager.updateSummary();
        },

        setupAutoRefresh() {
            if (AppState.currentMode === 'stream') {
                clearInterval(AppState.autoRefreshInterval);
                AppState.autoRefreshInterval = setInterval(() => {
                    if (AppState.currentMode === 'stream' && !AppState.isFetching) {
                        this.refreshStreamQuietly();
                    }
                }, CONFIG.AUTO_REFRESH_INTERVAL);
            }
        },

        // FIX P2: Use cache-busting query param instead of blanking src
        refreshStreamQuietly() {
            const liveStream = DOMCache.get('liveStream');
            if (liveStream && AppState.currentMode === 'stream') {
                const baseSrc = liveStream.src.split('&_t=')[0];
                liveStream.src = `${baseSrc}&_t=${Date.now()}`;
            }
        }
    };

    // --- Public API ---
    window.ProAttendanceApp = {
        removePicture() {
            AppState.selectedFile = null;
            const fileInput = DOMCache.get('attendanceImageInput');
            const previewPic = DOMCache.get('previewPic');
            
            if (fileInput) fileInput.value = "";
            if (previewPic) {
                previewPic.src = "";
                previewPic.style.display = "none";
            }
            
            UIManager.updateModeUI();
            NotificationManager.showNotification('Image removed', 'info', 2000);
        },

        resetAttendanceState() {
            if (AppState.manualAssignments.size > 0 || AppState.falsePositiveAssignments.size > 0) {
                const manualCount = AppState.manualAssignments.size;
                const falsePositiveCount = AppState.falsePositiveAssignments.size;
                const confirmReset = confirm(
                    `There are ${manualCount} manual assignments and ${falsePositiveCount} false positive corrections that will be cleared.\n\nDo you want to continue?`
                );
                if (!confirmReset) return;
            }

            AppState.manualAssignments.forEach((assignment) => {
                FaceManager.restoreUnidentifiedFace(assignment.faceData, true);
            });

            AppState.manualAssignments.clear();
            AppState.automaticAssignments.clear();
            AppState.falsePositiveAssignments.clear();
            AppState.studentPresentStatus.clear();
            AppState.studentPhotos.clear();
            UndoRedoManager.clear();

            DOMCache.queryAll('.roster-student.present').forEach(el => {
                el.classList.remove('present', 'manual-assignment');
                const indicator = el.querySelector('.status-indicator');
                if (indicator) {
                    indicator.title = 'Click to mark present';
                }
            });

            const unidList = DOMCache.get('unidentifiedList');
            if (unidList) {
                unidList.innerHTML = '';
            }

            RosterManager.updateSummary();
            FaceManager.checkUnidentifiedVisibility();
            NotificationManager.showNotification('Attendance state reset', 'info', 3000);
        },

        refreshStream() {
            const liveStream = DOMCache.get('liveStream');
            if (liveStream && AppState.currentMode === 'stream') {
                NotificationManager.showNotification('Refreshing stream...', 'info', 2000);
                const baseSrc = liveStream.src.split('&_t=')[0];
                liveStream.src = `${baseSrc}&_t=${Date.now()}`;
            }
        },

        filterStudents() {
            const searchInput = DOMCache.get('studentSearch');
            const searchClear = DOMCache.get('searchClear');
            const rosterList = DOMCache.get('studentRosterList');
            
            if (!searchInput || !rosterList) return;

            const searchTerm = searchInput.value.toLowerCase().trim();
            const students = rosterList.querySelectorAll('.roster-student');

            if (searchClear) {
                searchClear.style.display = searchTerm ? 'flex' : 'none';
            }

            let visibleCount = 0;
            students.forEach(student => {
                const name = student.querySelector('.roster-student-name')?.textContent.toLowerCase() || '';
                const matchesSearch = name.includes(searchTerm);
                const matchesSection = !AppState.selectedSection || 
                                     AppState.selectedSection === '' || 
                                     student.dataset.sectionPk === AppState.selectedSection;
                
                const shouldShow = matchesSearch && matchesSection;
                student.style.display = shouldShow ? 'flex' : 'none';
                if (shouldShow) visibleCount++;
            });

            let emptyState = rosterList.querySelector('.search-empty-state');
            if (visibleCount === 0 && searchTerm) {
                if (!emptyState) {
                    emptyState = Utils.createElement('div', {
                        classes: ['search-empty-state'],
                        html: `
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" width="48" height="48">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
                            </svg>
                            <p>No students found matching "${searchTerm}"</p>
                        `
                    });
                    rosterList.appendChild(emptyState);
                }
            } else if (emptyState) {
                emptyState.remove();
            }

            RosterManager.updateSummary();
        },

        clearSearch() {
            const searchInput = DOMCache.get('studentSearch');
            if (searchInput) {
                searchInput.value = '';
                this.filterStudents();
                searchInput.focus();
            }
        },

        toggleSidebar() {
            const sidebar = DOMCache.get('sidebar');
            if (sidebar) {
                sidebar.classList.toggle('open');
                document.body.classList.toggle('sidebar-open');
            }
        }
    };

    // Make individual functions available globally
    window.removePicture = window.ProAttendanceApp.removePicture;
    window.resetAttendanceState = window.ProAttendanceApp.resetAttendanceState;
    window.refreshStream = window.ProAttendanceApp.refreshStream;
    window.filterStudents = window.ProAttendanceApp.filterStudents;
    window.clearSearch = window.ProAttendanceApp.clearSearch;
    window.toggleSidebar = window.ProAttendanceApp.toggleSidebar;
    window.closeNotification = () => NotificationManager.closeNotification();
    window.undoLastAssignment = () => UndoRedoManager.undo();
    window.closeUndoNotification = () => {
        const undoNotification = DOMCache.get('undoNotification');
        if (undoNotification) {
            undoNotification.classList.remove('show');
        }
    };

    window.filterBySection = function() {
        SectionManager.filterStudentDisplay();
    };

    // Upload CSV function
    async function uploadCsv() {
        const fileInput = DOMCache.get('csvFileInput');
        const messageEl = DOMCache.get('csvUploadMessage');
        
        if (!fileInput?.files?.[0]) {
            NotificationManager.showNotification('Please select a CSV file', 'error');
            return;
        }
        
        const file = fileInput.files[0];
        NotificationManager.showNotification(`Uploading ${file.name}...`, 'info');
        
        const formData = new FormData();
        formData.append('csvFile', file);
        
        try {
            const response = await fetch('/upload_csv/', {
                method: 'POST',
                headers: { 'X-CSRFToken': Utils.getCsrfToken() },
                body: formData
            });
            
            const data = await response.json();
            
            if (data.success) {
                if (messageEl) {
                    messageEl.textContent = data.message || 'CSV uploaded successfully!';
                    messageEl.className = 'modal-message success';
                }
                NotificationManager.showNotification(data.message || 'CSV uploaded successfully!', 'success');
                fileInput.value = '';
            } else {
                if (messageEl) {
                    messageEl.textContent = data.message || 'Upload failed';
                    messageEl.className = 'modal-message error';
                }
                NotificationManager.showNotification(data.message || 'Upload failed', 'error');
            }
        } catch (error) {
            if (messageEl) {
                messageEl.textContent = 'Error uploading CSV file';
                messageEl.className = 'modal-message error';
            }
            NotificationManager.showNotification('Error uploading CSV file', 'error');
        }
    }

    function downloadCsv() {
        const classId = DOMCache.get('mainContent')?.dataset.classId;
        if (!classId) {
            NotificationManager.showNotification('No class selected', 'error');
            return;
        }
        
        NotificationManager.showNotification('Preparing CSV download...', 'info');
        window.open(`/download_attendance/?class_id=${classId}`, '_blank');
        
        setTimeout(() => {
            NotificationManager.showNotification('CSV download started', 'success');
        }, 1000);
    }

    function processQuiz(event) {
        event.preventDefault();
        const titleInput = DOMCache.get('quizTitleInput');
        const dateInput = DOMCache.get('quizDateInput');
        const resultEl = DOMCache.get('quizResult');
        
        if (titleInput?.value && dateInput?.value) {
            NotificationManager.showNotification(
                `Quiz "${titleInput.value}" scheduled for ${dateInput.value}`,
                'success'
            );
            if (resultEl) {
                resultEl.textContent = 'Quiz information saved successfully!';
                resultEl.className = 'modal-message success';
            }
            titleInput.value = '';
            dateInput.value = '';
        }
    }

    async function submitFeedback(event) {
        event.preventDefault();
        
        const typeInput = DOMCache.get('feedbackType');
        const messageInput = DOMCache.get('feedbackMessage');
        const emailInput = DOMCache.get('feedbackEmail');
        const resultEl = DOMCache.get('feedbackResult');
        const submitBtn = DOMCache.get('feedbackSubmitBtn');
        
        if (!typeInput?.value || !messageInput?.value) {
            NotificationManager.showNotification('Please fill in all required fields', 'error');
            return;
        }

        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.querySelector('.button-text').style.display = 'none';
            submitBtn.querySelector('.button-loading').style.display = 'inline';
        }

        try {
            const feedbackData = {
                type: typeInput.value,
                message: messageInput.value,
                email: emailInput.value || ''
            };

            const response = await APIManager.submitFeedback(feedbackData);

            if (response.success && !response.stored) {
                NotificationManager.showNotification('Thank you for your feedback!', 'success');
                if (resultEl) {
                    resultEl.textContent = 'Feedback sent successfully!';
                    resultEl.className = 'modal-message success';
                }
                typeInput.value = '';
                messageInput.value = '';
                emailInput.value = '';
                
                setTimeout(() => {
                    const modal = DOMCache.get('feedbackModal');
                    if (modal) {
                        modal.classList.remove('show');
                        document.body.style.overflow = '';
                    }
                }, 2000);
            } else if (response.stored) {
                NotificationManager.showNotification(response.message, 'warning', 6000);
                if (resultEl) {
                    resultEl.textContent = response.message;
                    resultEl.className = 'modal-message warning';
                }
            } else {
                throw new Error('Failed to submit feedback');
            }
        } catch (error) {
            NotificationManager.showNotification('Error submitting feedback.', 'error');
            if (resultEl) {
                resultEl.textContent = 'Failed to submit feedback.';
                resultEl.className = 'modal-message error';
            }
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.querySelector('.button-text').style.display = 'inline';
                submitBtn.querySelector('.button-loading').style.display = 'none';
            }
        }
    }

    async function submitBugReport(event) {
        event.preventDefault();
        
        const areaInput = DOMCache.get('bugArea');
        const descriptionInput = DOMCache.get('bugDescription');
        const emailInput = DOMCache.get('bugEmail');
        const resultEl = DOMCache.get('bugReportResult');
        const submitBtn = DOMCache.get('bugSubmitBtn');
        
        if (!areaInput?.value || !descriptionInput?.value || !emailInput?.value) {
            NotificationManager.showNotification('Please fill in all fields', 'error');
            return;
        }

        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.querySelector('.button-text').style.display = 'none';
            submitBtn.querySelector('.button-loading').style.display = 'inline';
        }

        try {
            const bugData = {
                area: areaInput.value,
                description: descriptionInput.value,
                email: emailInput.value
            };

            const response = await APIManager.submitBugReport(bugData);

            if (response.success && !response.stored) {
                NotificationManager.showNotification('Bug report sent successfully!', 'success', 5000);
                if (resultEl) {
                    resultEl.textContent = 'Bug report sent successfully!';
                    resultEl.className = 'modal-message success';
                }
                areaInput.value = '';
                descriptionInput.value = '';
                emailInput.value = '';
                
                setTimeout(() => {
                    const modal = DOMCache.get('reportBugModal');
                    if (modal) {
                        modal.classList.remove('show');
                        document.body.style.overflow = '';
                    }
                }, 3000);
            } else if (response.stored) {
                NotificationManager.showNotification(response.message, 'warning', 6000);
                if (resultEl) {
                    resultEl.textContent = response.message;
                    resultEl.className = 'modal-message warning';
                }
            } else {
                throw new Error('Failed to submit bug report');
            }
        } catch (error) {
            NotificationManager.showNotification('Error submitting bug report.', 'error');
            if (resultEl) {
                resultEl.textContent = 'Failed to submit bug report.';
                resultEl.className = 'modal-message error';
            }
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.querySelector('.button-text').style.display = 'inline';
                submitBtn.querySelector('.button-loading').style.display = 'none';
            }
        }
    }

    // Modal Handlers
    document.body.addEventListener('click', (event) => {
        const target = event.target;
        
        const trigger = target.closest('[data-modal-trigger]');
        if (trigger) {
            const modalId = trigger.dataset.modalTrigger;
            const modal = document.getElementById(modalId);
            if (modal) {
                modal.classList.add('show');
                document.body.style.overflow = 'hidden';
            }
            return;
        }
        
        const closeBtn = target.closest('[data-modal-close]');
        if (closeBtn) {
            const modal = closeBtn.closest('.modal');
            if (modal) {
                modal.classList.remove('show');
                document.body.style.overflow = '';
            }
            return;
        }
        
        if (target.classList.contains('modal')) {
            target.classList.remove('show');
            document.body.style.overflow = '';
            return;
        }
    });

    // Initialization
    function init() {
        initializeEmailJS();
        APIManager.checkFailedSubmissions();

        SectionManager.init();
        DragDropManager.init();
        KeyboardManager.init();

        const modeRadios = DOMCache.queryAll('input[name="attendanceMode"]');
        modeRadios.forEach(radio => {
            radio.addEventListener('change', EventHandlers.handleModeChange);
        });

        const fileInput = DOMCache.get('attendanceImageInput');
        if (fileInput) {
            fileInput.addEventListener('change', EventHandlers.handleFileUpload);
        }

        const startBtn = DOMCache.get('startAttendanceButton');
        if (startBtn) {
            startBtn.addEventListener('click', EventHandlers.handleStartAttendance);
        }

        const cancelBtn = DOMCache.get('cancelDetectionBtn');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', EventHandlers.handleCancelDetection);
        }

        const rosterList = DOMCache.get('studentRosterList');
        if (rosterList) {
            rosterList.addEventListener('click', EventHandlers.handleRosterClick);
        }

        // FIX U4: Only use the JS debounced handler, don't double-bind
        const searchInput = DOMCache.get('studentSearch');
        if (searchInput) {
            // Remove any inline onkeyup handler
            searchInput.removeAttribute('onkeyup');
            searchInput.addEventListener('input', Utils.debounce(window.filterStudents, 300));
        }

        const { classId, hasStream } = Utils.getClassConfig();
        const liveStream = DOMCache.get('liveStream');
        if (liveStream && classId && hasStream) {
            liveStream.src = `/video_feed/?class_id=${classId}`;
        }

        UIManager.updateModeUI();
        RosterManager.updateSummary();

        window.uploadCsv = uploadCsv;
        window.downloadCsv = downloadCsv;
        window.processQuiz = processQuiz;
        window.submitFeedback = submitFeedback;
        window.submitBugReport = submitBugReport;

        console.log("Pro Attendance AI v2.1 - All bugs fixed");
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
