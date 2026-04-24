/* static/Attendance/js/instructor_dashboard.js */

document.addEventListener('DOMContentLoaded', () => {
    const classListElement = document.getElementById('class-list');
    
    // Get API URLs and CSRF token from the global window object set in the HTML template
    const API_DETAIL_CLASS_BASE_URL = window.apiUrls?.detailClassBase; // e.g., "/api/classes/"
    const CSRF_TOKEN = window.csrfToken;

    if (!classListElement) {
        console.error("Critical element #class-list not found!");
        return;
    }

    // --- Event Listener for Deleting a Class ---
    classListElement.addEventListener('click', async (event) => {
        const deleteButton = event.target.closest('.btn-delete');
        if (!deleteButton) return;

        const classCard = deleteButton.closest('.course-card');
        const classId = classCard?.dataset.classId;
        const className = classCard?.querySelector('h3')?.textContent || 'this class';

        if (!classId) {
            alert("Error: Could not find the class ID to delete.");
            return;
        }

        if (confirm(`Are you sure you want to delete the class "${className}"? This action cannot be undone.`)) {
            deleteButton.disabled = true;
            const deleteUrl = `${API_DETAIL_CLASS_BASE_URL}${classId}/`;

            try {
                const response = await fetch(deleteUrl, {
                    method: 'DELETE',
                    headers: { 'X-CSRFToken': CSRF_TOKEN }
                });

                if (!response.ok) {
                    const errorData = await response.json().catch(() => ({}));
                    throw new Error(errorData.error || `Failed to delete class (HTTP ${response.status})`);
                }

                // On success (status 204 No Content), animate and remove the card
                classCard.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                classCard.style.opacity = '0';
                classCard.style.transform = 'scale(0.95)';
                setTimeout(() => {
                    classCard.remove();
                    // Check if the list is now empty to show the placeholder
                    if (classListElement.querySelectorAll('.course-card').length === 0) {
                        const placeholder = classListElement.querySelector('.course-list-empty-placeholder');
                        if (placeholder) placeholder.classList.add('is-visible');
                    }
                }, 300);

            } catch (error) {
                alert(`Error: ${error.message}`);
                deleteButton.disabled = false;
            }
        }
    });
});
