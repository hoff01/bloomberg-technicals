(function () {
    const STORAGE_KEY = 'pricing-dashboard-theme';
    const CLASS_DARK = 'theme-dark';

    function getSavedTheme() {
        try {
            return localStorage.getItem(STORAGE_KEY);
        } catch (err) {
            return null;
        }
    }

    function setSavedTheme(theme) {
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (err) {
            // Ignore storage errors (private mode, blocked storage).
        }
    }

    function getThemeToggleElements() {
        return Array.from(document.querySelectorAll('[data-theme-toggle]'));
    }

    function applyTheme(theme) {
        const isDark = theme === 'dark';
        document.body.classList.toggle(CLASS_DARK, isDark);
        document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
        getThemeToggleElements().forEach((el) => {
            el.classList.toggle('active', isDark);
            if (el.matches('button, [role="button"]')) {
                el.setAttribute('aria-pressed', isDark ? 'true' : 'false');
            }
        });
        window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    }

    function resolveInitialTheme() {
        const saved = getSavedTheme();
        if (saved === 'dark' || saved === 'light') return saved;
        return 'light';
    }

    function toggleTheme() {
        const next = document.body.classList.contains(CLASS_DARK) ? 'light' : 'dark';
        setSavedTheme(next);
        applyTheme(next);
    }

    function bindThemeToggles() {
        getThemeToggleElements().forEach((el) => {
            el.addEventListener('click', toggleTheme);
        });
    }

    function initTheme() {
        applyTheme(resolveInitialTheme());
        bindThemeToggles();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTheme);
    } else {
        initTheme();
    }
})();
