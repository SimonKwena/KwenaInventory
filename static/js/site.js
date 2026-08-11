/* The app is deployed under a fixed URL prefix (e.g. /inventory/). Derive it
   from the current path so AJAX calls and redirects stay correct regardless of
   where the app is mounted. */
function appPrefix() {
    const base = document.querySelector('base');
    let path = base ? base.getAttribute('href') : null;
    if (!path) {
        path = window.location.pathname;
    }
    // Take the leading "/segment/" of the path (e.g. "/inventory/").
    const m = path.match(/^\/[^\/]+\//);
    return m ? m[0] : '/';
}

const APP_PREFIX = appPrefix();

function appUrl(rel) {
    // rel is like "live/version/" or "item-lookup/?sku=x"
    return APP_PREFIX + rel.replace(/^\//, '');
}

/* Read the CSRF token from the cookie so AJAX POSTs are accepted. */
function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
}

/* Update the cart badge wherever it appears (nav, catalog icon, etc.). */
function updateCartBadge(counts) {
    const total = (counts && counts.total) || 0;
    document.querySelectorAll('[data-nav-badge="cart"]').forEach(function (badge) {
        if (total > 0) {
            badge.textContent = total;
            badge.removeAttribute('hidden');
        } else {
            badge.setAttribute('hidden', '');
        }
    });
}

/* Lightweight toast used for "added to cart" feedback (survives live refresh). */
function showToast(message, opts) {
    opts = opts || {};
    let bar = document.getElementById('toast-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'toast-bar';
        bar.className = 'toast-bar';
        document.body.appendChild(bar);
    }
    const toast = document.createElement('div');
    toast.className = 'toast' + (opts.kind ? ' toast-' + opts.kind : '');
    toast.setAttribute('role', 'status');

    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    toast.appendChild(text);

    if (opts.actionLabel && opts.actionHref) {
        const link = document.createElement('a');
        link.className = 'toast-action';
        link.href = opts.actionHref;
        link.textContent = opts.actionLabel;
        toast.appendChild(link);
    }

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    toast.appendChild(close);

    bar.appendChild(toast);
    close.addEventListener('click', function () { toast.remove(); });
    setTimeout(function () { toast.remove(); }, opts.duration || 5000);
}

/* Toggle skeleton loading state on a form or region. */
function setSkeletonLoading(form, loading) {
    if (!form) {
        return;
    }
    const region = form.closest('[data-region]');
    if (region) {
        region.setAttribute('data-loading', loading ? 'true' : 'false');
    }
    form.querySelectorAll('button, input, select, textarea').forEach(function (el) {
        el.disabled = loading;
    });
    if (loading) {
        form.classList.add('is-loading');
    } else {
        form.classList.remove('is-loading');
    }
}

/* ---------- Browse "Add to cart" (event-delegated, survives live refresh) ---------- */
function cartQtyFor(itemId) {
    const input = document.querySelector('.cart-qty[data-item="' + itemId + '"]');
    let qty = 1;
    if (input) {
        const parsed = parseInt(input.value, 10);
        if (!isNaN(parsed) && parsed > 0) {
            qty = parsed;
        }
    }
    return qty;
}

function flashButton(btn, label) {
    const original = btn.textContent;
    btn.textContent = label;
    btn.disabled = true;
    setTimeout(function () {
        btn.textContent = original;
        btn.disabled = false;
    }, 1200);
}

// "Add to cart" forms: copy the tile qty into the hidden quantity field, then
// submit via AJAX for a snappy experience. If anything goes wrong, we fall
// back to the form's normal (non-JS) POST so it always works.
document.addEventListener('submit', function (event) {
    const form = event.target.closest('.cart-add-form');
    if (!form) {
        return;
    }
    const qtyInput = form.querySelector('.cart-qty');
    if (qtyInput) {
        const hidden = form.querySelector('input[name="quantity"]');
        if (hidden) {
            hidden.value = qtyInput.value;
        }
    }
    event.preventDefault();
    setSkeletonLoading(form, true);
    const btn = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    fetch(form.getAttribute('action'), {
        method: 'POST',
        headers: { 'x-requested-with': 'XMLHttpRequest' },
        body: formData,
        credentials: 'same-origin',
    })
        .then(function (resp) {
            if (!resp.ok) {
                throw new Error('HTTP ' + resp.status);
            }
            return resp.text().then(function (text) {
                try {
                    return JSON.parse(text);
                } catch (e) {
                    throw new Error('Unexpected response');
                }
            });
        })
        .then(function (data) {
            setSkeletonLoading(form, false);
            if (data && data.ok) {
                updateCartBadge(data.counts);
                flashButton(btn, 'Added');
                const label = formData.get('action') === 'check_out' ? 'Check out' : 'Book ahead';
                showToast(
                    'Added to your ' + label + ' cart. Go to Home to fill in the form and submit.',
                    { actionLabel: 'Go to Home', actionHref: appUrl(''), duration: 6000 }
                );
            } else {
                form.submit();
            }
        })
        .catch(function () {
            setSkeletonLoading(form, false);
            form.submit();
        });
});

// Keep the hidden quantity inputs in each submit form in sync with the
// editable quantity fields on the home cart (delegated, survives refresh).
document.addEventListener('input', function (event) {
    const input = event.target.closest('.cart-home-qty');
    if (!input) {
        return;
    }
    const item = input.closest('.cart-item');
    if (!item) {
        return;
    }
    const qty = input.value;
    document.querySelectorAll('.cart-submit-qty[data-action="' + item.dataset.action + '"][data-item="' + item.dataset.item + '"]')
        .forEach(function (hidden) { hidden.value = qty; });
});

/* Live relative times (e.g. "Overdue by 5 minutes"). The server renders the
   initial value, but it must keep ticking as time passes — a data-version
   change alone never fires while an item simply sits overdue. We recompute
   client-side from the absolute due instant so it stays correct in any
   timezone. */
function humanizeDelta(ms) {
    const sec = Math.floor(ms / 1000);
    const min = Math.floor(sec / 60);
    if (min < 1) {
        return "less than a minute";
    }
    if (min < 60) {
        return min + (min === 1 ? " minute" : " minutes");
    }
    const hr = Math.floor(min / 60);
    const remMin = min % 60;
    if (hr < 24) {
        if (remMin === 0) {
            return hr + (hr === 1 ? " hour" : " hours");
        }
        return hr + (hr === 1 ? " hour " : " hours ") + remMin + (remMin === 1 ? " minute" : " minutes");
    }
    const day = Math.floor(hr / 24);
    const remHr = hr % 24;
    if (remHr === 0) {
        return day + (day === 1 ? " day" : " days");
    }
    return day + (day === 1 ? " day " : " days ") + remHr + (remHr === 1 ? " hour" : " hours");
}

function tickRelativeTimes() {
    document.querySelectorAll("[data-due]").forEach(function (li) {
        const due = new Date(li.getAttribute("data-due"));
        if (isNaN(due.getTime())) {
            return;
        }
        const diffMs = due.getTime() - Date.now();
        const overdue = diffMs < 0;
        const text = (overdue ? "Overdue by " : "Due in ") + humanizeDelta(Math.abs(diffMs));
        const strong = li.querySelector("[data-time-left]");
        if (strong) {
            strong.textContent = text;
            strong.classList.toggle("is-overdue", overdue);
        }
        const dot = li.querySelector(".status-dot");
        if (dot) {
            dot.classList.toggle("is-low", overdue);
            dot.classList.toggle("is-ok", !overdue);
        }
    });
}

/* One-time bindings for elements that live outside #main and survive live
   refreshes (theme toggle, top nav, toasts, catalog group collapse). */
function initGlobal() {
    /* ---------- Theme toggle (persisted) ---------- */
    const themeToggle = document.getElementById('theme-toggle');
    const root = document.documentElement;
    const storedTheme = localStorage.getItem('gearroom-theme');
    const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
    if (storedTheme) {
        root.setAttribute('data-theme', storedTheme);
    } else if (prefersLight) {
        root.setAttribute('data-theme', 'light');
    }

    if (themeToggle) {
        themeToggle.addEventListener('click', function () {
            const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
            root.setAttribute('data-theme', next);
            localStorage.setItem('gearroom-theme', next);
        });
    }

    /* ---------- Mobile navigation ---------- */
    const navToggle = document.getElementById('nav-toggle');
    const topbar = document.getElementById('topbar');
    if (navToggle && topbar) {
        navToggle.addEventListener('click', function () {
            const open = topbar.classList.toggle('nav-open');
            navToggle.setAttribute('aria-expanded', String(open));
            navToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
        });
        topbar.querySelectorAll('.nav-menu a').forEach(function (link) {
            link.addEventListener('click', function () {
                topbar.classList.remove('nav-open');
                navToggle.setAttribute('aria-expanded', 'false');
            });
        });
    }

    /* ---------- Account dropdown ---------- */
    const accountDropdown = document.getElementById('account-dropdown');
    const accountToggle = document.getElementById('account-toggle');
    const accountMenu = document.getElementById('account-menu');
    function closeAccountMenu() {
        if (!accountDropdown || !accountDropdown.classList.contains('is-open')) {
            return;
        }
        accountDropdown.classList.remove('is-open');
        accountToggle.setAttribute('aria-expanded', 'false');
        accountMenu.hidden = true;
    }
    if (accountToggle && accountMenu) {
        function openAccountMenu() {
            accountDropdown.classList.add('is-open');
            accountToggle.setAttribute('aria-expanded', 'true');
            accountMenu.hidden = false;
        }
        accountToggle.addEventListener('click', function (event) {
            event.stopPropagation();
            const open = accountDropdown.classList.toggle('is-open');
            accountToggle.setAttribute('aria-expanded', String(open));
            accountMenu.hidden = !open;
        });
        accountMenu.querySelectorAll('[role="menuitem"]').forEach(function (item) {
            item.addEventListener('click', function () {
                closeAccountMenu();
            });
        });
        document.addEventListener('click', function (event) {
            if (accountDropdown && !accountDropdown.contains(event.target)) {
                closeAccountMenu();
            }
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                closeAccountMenu();
            }
        });
    }

    /* ---------- Manage dropdown (admin tools) ---------- */
    const manageDropdown = document.getElementById('manage-dropdown');
    const manageToggle = document.getElementById('manage-toggle');
    const manageMenu = document.getElementById('manage-menu');
    if (manageToggle && manageMenu) {
        function openManageMenu() {
            manageDropdown.classList.add('is-open');
            manageToggle.setAttribute('aria-expanded', 'true');
            manageMenu.hidden = false;
        }
        function closeManageMenu() {
            if (!manageDropdown || !manageDropdown.classList.contains('is-open')) {
                return;
            }
            manageDropdown.classList.remove('is-open');
            manageToggle.setAttribute('aria-expanded', 'false');
            manageMenu.hidden = true;
        }
        manageToggle.addEventListener('click', function (event) {
            event.stopPropagation();
            const open = manageDropdown.classList.toggle('is-open');
            manageToggle.setAttribute('aria-expanded', String(open));
            manageMenu.hidden = !open;
        });
        // Open on hover/focus for pointer devices, while click still toggles.
        if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
            manageDropdown.addEventListener('mouseenter', openManageMenu);
            manageDropdown.addEventListener('focusin', openManageMenu);
            manageDropdown.addEventListener('mouseleave', closeManageMenu);
            manageDropdown.addEventListener('focusout', function (event) {
                if (!manageDropdown.contains(event.relatedTarget)) {
                    closeManageMenu();
                }
            });
        }
        manageMenu.querySelectorAll('a').forEach(function (item) {
            item.addEventListener('click', function () {
                closeManageMenu();
                if (topbar && navToggle) {
                    topbar.classList.remove('nav-open');
                    navToggle.setAttribute('aria-expanded', 'false');
                }
            });
        });
        document.addEventListener('click', function (event) {
            if (manageDropdown && !manageDropdown.contains(event.target)) {
                closeManageMenu();
            }
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                closeManageMenu();
            }
        });
    }

    /* ---------- Auto-dismiss toasts ---------- */
    document.querySelectorAll('#message-bar .message').forEach(function (msg) {
        const closeBtn = msg.querySelector('.message-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', function () {
                msg.remove();
            });
        }
        setTimeout(function () {
            msg.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
            msg.style.opacity = '0';
            msg.style.transform = 'translateY(-8px)';
            setTimeout(function () {
                msg.remove();
            }, 400);
        }, 6000);
    });

    /* ---------- Catalog group collapse (event delegation survives swaps) ---------- */
    document.addEventListener('click', function (event) {
        const toggle = event.target.closest('.group-toggle');
        if (!toggle) {
            return;
        }
        const targetId = toggle.dataset.target;
        const section = document.getElementById(targetId);
        if (!section) {
            return;
        }
        const collapsed = section.classList.toggle('collapsed');
        toggle.setAttribute('aria-expanded', String(!collapsed));
        const toggleAllBtn = document.getElementById('catalog-toggle-all');
        if (toggleAllBtn) {
            const anyCollapsed = document.querySelectorAll('.group-body.collapsed').length > 0;
            toggleAllBtn.textContent = anyCollapsed ? 'Expand all' : 'Collapse all';
        }
    });
}

/* Page-specific bindings for elements inside #main. Safe to re-run after a
   live refresh, because the old #main (and its listeners) are replaced. */
function gearroomInit() {
    /* ---------- Catalog expand / collapse all ---------- */
    const toggleAllBtn = document.getElementById('catalog-toggle-all');
    if (toggleAllBtn) {
        const catalogBodies = function () {
            return document.querySelectorAll('.group-body');
        };
        const syncToggleAllLabel = function () {
            const anyCollapsed = catalogBodies().length > 0 &&
                document.querySelectorAll('.group-body.collapsed').length > 0;
            toggleAllBtn.textContent = anyCollapsed ? 'Expand all' : 'Collapse all';
        };
        toggleAllBtn.addEventListener('click', function () {
            const anyCollapsed = document.querySelectorAll('.group-body.collapsed').length > 0;
            catalogBodies().forEach(function (body) {
                body.classList.toggle('collapsed', !anyCollapsed);
            });
            document.querySelectorAll('.group-toggle').forEach(function (toggle) {
                toggle.setAttribute('aria-expanded', String(anyCollapsed));
            });
            syncToggleAllLabel();
        });
        syncToggleAllLabel();
    }

    /* ---------- Catalog search / filter ---------- */
    const catalogSidebar = document.getElementById('catalog-sidebar');
    const catalogSearch = catalogSidebar ? catalogSidebar.querySelector('.catalog-sidebar-search input') : null;
    const catalogCategoryInputs = document.querySelectorAll('[data-filter="category"]');
    const catalogLocationFilter = document.getElementById('catalog-location-filter');
    const catalogAvailabilityFilter = document.getElementById('catalog-availability-filter');
    const catalogResetBtn = document.getElementById('catalog-filter-reset');
    const catalogStatusEl = document.getElementById('catalog-filter-status');

    function getCatalogFilters() {
        const categories = [];
        catalogCategoryInputs.forEach(function (cb) {
            if (cb.checked) categories.push(cb.value);
        });
        return {
            search: (catalogSearch ? catalogSearch.value : '').trim().toLowerCase(),
            categories: categories,
            location: catalogLocationFilter ? catalogLocationFilter.value : '',
            availability: catalogAvailabilityFilter ? catalogAvailabilityFilter.value : ''
        };
    }

    function readCatalogFiltersFromUrl() {
        const params = new URLSearchParams(window.location.search);
        return {
            search: (params.get('q') || '').trim().toLowerCase(),
            categories: params.get('categories') ? params.get('categories').split(',').filter(Boolean) : [],
            location: params.get('location') || '',
            availability: params.get('availability') || ''
        };
    }

    function writeCatalogFiltersToUrl(f) {
        const params = new URLSearchParams(window.location.search);
        if (f.search) {
            params.set('q', f.search);
        } else {
            params.delete('q');
        }
        if (f.categories.length) {
            params.set('categories', f.categories.join(','));
        } else {
            params.delete('categories');
        }
        if (f.location) {
            params.set('location', f.location);
        } else {
            params.delete('location');
        }
        if (f.availability) {
            params.set('availability', f.availability);
        } else {
            params.delete('availability');
        }
        const qs = params.toString();
        const newUrl = window.location.pathname + (qs ? '?' + qs : '');
        if (newUrl !== window.location.pathname + window.location.search) {
            history.replaceState(null, '', newUrl);
        }
    }

    function applyCatalogFiltersFromUrl() {
        const f = readCatalogFiltersFromUrl();
        if (catalogSearch) catalogSearch.value = f.search;
        catalogCategoryInputs.forEach(function (cb) {
            cb.checked = f.categories.indexOf(cb.value) !== -1;
        });
        if (catalogLocationFilter) catalogLocationFilter.value = f.location;
        if (catalogAvailabilityFilter) catalogAvailabilityFilter.value = f.availability;
        applyCatalogFilters();
    }

    function applyCatalogFilters() {
        const f = getCatalogFilters();
        const cards = document.querySelectorAll('.product-card');
        let totalVisible = 0;
        cards.forEach(function (card) {
            const location = card.getAttribute('data-location') || '';
            const available = parseInt(card.getAttribute('data-available') || '0', 10);
            const category = card.getAttribute('data-category') || '';
            const search = card.getAttribute('data-search') || '';
            let match = true;
            if (f.categories.length && f.categories.indexOf(category) === -1) match = false;
            if (f.location && location !== f.location) match = false;
            if (f.availability === 'available' && available <= 0) match = false;
            if (f.availability === 'out' && available > 0) match = false;
            if (f.search && search.toLowerCase().indexOf(f.search) === -1) match = false;
            card.style.display = match ? '' : 'none';
            if (match) totalVisible++;
        });
        const emptyEl = document.querySelector('.catalog-empty');
        if (emptyEl) {
            emptyEl.style.display = totalVisible ? 'none' : '';
        }
        if (catalogStatusEl) {
            const noun = totalVisible === 1 ? 'item' : 'items';
            catalogStatusEl.textContent = totalVisible + ' ' + noun + (totalVisible ? ' shown' : '');
        }
    }

    if (catalogSearch || catalogLocationFilter) {
        if (catalogSearch) {
            catalogSearch.addEventListener('input', function () {
                applyCatalogFilters();
                writeCatalogFiltersToUrl(getCatalogFilters());
            });
        }
        catalogCategoryInputs.forEach(function (cb) {
            cb.addEventListener('change', function () {
                applyCatalogFilters();
                writeCatalogFiltersToUrl(getCatalogFilters());
            });
        });
        if (catalogLocationFilter) {
            catalogLocationFilter.addEventListener('change', function () {
                applyCatalogFilters();
                writeCatalogFiltersToUrl(getCatalogFilters());
            });
        }
        if (catalogAvailabilityFilter) {
            catalogAvailabilityFilter.addEventListener('change', function () {
                applyCatalogFilters();
                writeCatalogFiltersToUrl(getCatalogFilters());
            });
        }
        if (catalogResetBtn) {
            catalogResetBtn.addEventListener('click', function () {
                if (catalogSearch) catalogSearch.value = '';
                catalogCategoryInputs.forEach(function (cb) { cb.checked = false; });
                if (catalogLocationFilter) catalogLocationFilter.value = '';
                if (catalogAvailabilityFilter) catalogAvailabilityFilter.value = '';
                applyCatalogFilters();
                writeCatalogFiltersToUrl(getCatalogFilters());
            });
        }
        const catalogSearchForm = catalogSearch ? catalogSearch.closest('form') : null;
        if (catalogSearchForm) {
            catalogSearchForm.addEventListener('submit', function () {
                const f = getCatalogFilters();
                f.search = (catalogSearch ? catalogSearch.value : '').trim().toLowerCase();
                writeCatalogFiltersToUrl(f);
                catalogSearchForm.action = window.location.pathname + window.location.search;
            });
        }
        applyCatalogFiltersFromUrl();
    }

    window.addEventListener('popstate', function () {
        if (catalogSearch || catalogLocationFilter) {
            applyCatalogFiltersFromUrl();
        }
    });

    // Keep the visible quantity input in sync with the hidden field the form
    // actually submits, and clamp it to the current stock limit.
    document.querySelectorAll('.catalog-qty').forEach(function (input) {
        input.addEventListener('input', function () {
            const form = input.closest('form');
            if (!form) return;
            const hidden = form.querySelector('.catalog-qty-hidden');
            if (hidden) hidden.value = input.value || '1';
            const max = parseInt(input.max || '1', 10);
            if (parseInt(input.value, 10) > max) {
                input.value = max;
                if (hidden) hidden.value = String(max);
            }
        });
    });

    // Catalog add-to-cart via AJAX: stay on the catalog page and show a toast
    // message instead of navigating away.
    document.querySelectorAll('.catalog-add-form').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            const btn = form.querySelector('button[type="submit"]');
            if (btn) {
                btn.disabled = true;
            }
            const formData = new FormData(form);
            fetch(form.getAttribute('action'), {
                method: 'POST',
                headers: { 'x-requested-with': 'XMLHttpRequest' },
                body: formData,
                credentials: 'same-origin',
            })
                .then(function (resp) { return resp.ok ? resp.json() : null; })
                .then(function (data) {
                    if (data && data.message) {
                        showToast(data.message, { kind: 'success' });
                    }
                    if (data && data.counts) {
                        updateCartBadge(data.counts);
                    }
                })
                .catch(function () {
                    showToast('Could not add item. Please try again.', { kind: 'error' });
                })
                .finally(function () {
                    if (btn) {
                        btn.disabled = false;
                    }
                });
        });
    });

    /* ---------- All items: full filter system + "+N more" ---------- */
    const allItemsTable = document.getElementById('all-items-table');
    if (allItemsTable) {
        const rows = Array.prototype.slice.call(
            allItemsTable.querySelectorAll('tbody tr.all-item-row')
        );
        const moreBtn = document.getElementById('all-items-more');
        const searchInput = document.getElementById('item-search');
        const locationSelect = document.getElementById('filter-location');
        const categorySelect = document.getElementById('filter-category');
        const availabilitySelect = document.getElementById('filter-availability');
        const resetBtn = document.getElementById('item-filter-reset');
        const statusEl = document.getElementById('item-filter-status');
        const PREVIEW_COUNT = 8;
        let expanded = false;

        const dataRows = rows.filter(function (row) {
            return row.querySelector('a');
        });
        let currentVisible = dataRows.slice();

        function getFilters() {
            return {
                search: (searchInput ? searchInput.value : '').trim().toLowerCase(),
                location: locationSelect ? locationSelect.value : '',
                category: categorySelect ? categorySelect.value : '',
                availability: availabilitySelect ? availabilitySelect.value : ''
            };
        }

        function rowMatches(row, f) {
            if (f.location && row.getAttribute('data-location') !== f.location) {
                return false;
            }
            if (f.category && (row.getAttribute('data-category') || '') !== f.category) {
                return false;
            }
            if (f.availability === 'available' && !(parseInt(row.getAttribute('data-available'), 10) > 0)) {
                return false;
            }
            if (f.availability === 'out' && parseInt(row.getAttribute('data-available'), 10) > 0) {
                return false;
            }
            if (f.search) {
                const haystack = (row.getAttribute('data-search') || '').toLowerCase();
                if (haystack.indexOf(f.search) === -1) {
                    return false;
                }
            }
            return true;
        }

        function applyFilters() {
            const f = getFilters();
            const visible = [];
            dataRows.forEach(function (row) {
                const match = rowMatches(row, f);
                row.style.display = match ? '' : 'none';
                if (match) {
                    visible.push(row);
                }
            });

            let emptyRow = allItemsTable.querySelector('tbody tr.item-filter-empty');
            if (!visible.length) {
                if (!emptyRow) {
                    emptyRow = document.createElement('tr');
                    emptyRow.className = 'item-filter-empty';
                    emptyRow.innerHTML = '<td colspan="8" class="loc-empty">No items match your filters.</td>';
                    allItemsTable.querySelector('tbody').appendChild(emptyRow);
                }
                emptyRow.style.display = '';
            } else if (emptyRow) {
                emptyRow.style.display = 'none';
            }

            if (statusEl) {
                statusEl.textContent = visible.length +
                    (visible.length === 1 ? ' item' : ' items') +
                    (visible.length === dataRows.length ? '' : ' shown');
            }

            currentVisible = visible;
            applyPreview(visible);
        }

        function applyPreview(visibleRows) {
            visibleRows = visibleRows || currentVisible;
            if (visibleRows.length <= PREVIEW_COUNT) {
                if (moreBtn) {
                    moreBtn.style.display = 'none';
                }
                return;
            }
            visibleRows.forEach(function (row, index) {
                row.style.display = expanded || index < PREVIEW_COUNT ? '' : 'none';
            });
            if (moreBtn) {
                moreBtn.style.display = expanded ? 'none' : '';
                const hidden = visibleRows.length - PREVIEW_COUNT;
                moreBtn.textContent = '+' + hidden + ' more';
            }
        }

        applyFilters();

        if (moreBtn) {
            moreBtn.addEventListener('click', function () {
                expanded = true;
                applyPreview();
            });
        }

        [searchInput, locationSelect, categorySelect, availabilitySelect].forEach(function (el) {
            if (!el) {
                return;
            }
            const evt = el.tagName === 'INPUT' ? 'input' : 'change';
            el.addEventListener(evt, function () {
                expanded = false;
                applyFilters();
            });
        });

        if (resetBtn) {
            resetBtn.addEventListener('click', function () {
                expanded = false;
                if (searchInput) {
                    searchInput.value = '';
                }
                if (locationSelect) {
                    locationSelect.value = '';
                }
                if (categorySelect) {
                    categorySelect.value = '';
                }
                if (availabilitySelect) {
                    availabilitySelect.value = '';
                }
                applyFilters();
            });
        }
    }

    /* ---------- Collapsible card sections (items admin page) ---------- */
    document.querySelectorAll('.collapse-toggle').forEach(function (toggle) {
        const body = toggle.parentElement.querySelector(':scope > .collapse-body');
        if (!body) {
            return;
        }
        const setExpanded = function (expanded) {
            toggle.setAttribute('aria-expanded', String(expanded));
            if (expanded) {
                body.removeAttribute('hidden');
            } else {
                body.setAttribute('hidden', '');
            }
        };
        setExpanded(toggle.getAttribute('aria-expanded') === 'true');
        toggle.addEventListener('click', function () {
            setExpanded(toggle.getAttribute('aria-expanded') !== 'true');
        });
    });

    /* ---------- Password reveal ---------- */
    const passwordInput = document.querySelector('#id_password');
    const toggleButton = document.querySelector('#password-toggle');
    if (passwordInput && toggleButton) {
        toggleButton.addEventListener('click', function () {
            const isPassword = passwordInput.type === 'password';
            passwordInput.type = isPassword ? 'text' : 'password';
            toggleButton.textContent = isPassword ? 'Hide password' : 'Show password';
        });
    }

    const expectedReturnField = document.querySelector('#expected-return-field');
    const takenAtField = document.querySelector('#taken-at-field');
    const transactionTypeRadios = document.querySelectorAll('input[name="transaction_type"]');
    const addItemRow = document.querySelector('#add-item-row');
    const itemRowsContainer = document.querySelector('#item-rows');
    const condAllRow = document.querySelector('#condition-all-row');
    const condAllSelect = document.querySelector('#condition-all');
    const checkinLoanField = document.querySelector('#checkin-loan-field');
    const slipInputRow = document.querySelector('#slip-input-row');
    const notesLabel = document.querySelector('#notes-label');
    const notesTextarea = document.querySelector('#home-action-form textarea[name="notes"]');

    function getTransactionType() {
        const checked = document.querySelector('input[name="transaction_type"]:checked');
        return checked ? checked.value : 'book_ahead';
    }

    function parseLoanItems(opt) {
        // data-items is "itemId:qty:locId|itemId:qty:locId".
        const raw = opt.getAttribute('data-items') || '';
        return raw.split('|').filter(Boolean).map(function (part) {
            const bits = part.split(':');
            return { itemId: bits[0] || '', qty: bits[1] || '1', locId: bits[2] || '' };
        });
    }

    function filterCheckinLoans() {
        if (!checkinLoanField) {
            return;
        }
        const select = checkinLoanField.querySelector('select[name="source_request_id"]');
        if (!select) {
            return;
        }
        // Collect the item ids currently chosen in the form's item rows.
        const chosen = new Set();
        if (itemRowsContainer) {
            itemRowsContainer.querySelectorAll('select[name="item_ids"]').forEach(function (sel) {
                if (sel.value) {
                    chosen.add(sel.value);
                }
            });
        }
        // Enable only loans that contain a chosen item; disable the rest.
        let firstEnabled = '';
        select.querySelectorAll('option[data-items]').forEach(function (opt) {
            const items = parseLoanItems(opt);
            const match = chosen.size === 0 || items.some(function (it) {
                return chosen.has(it.itemId);
            });
            opt.disabled = !match;
            if (match && opt.value && !firstEnabled) {
                firstEnabled = opt.value;
            }
        });
        // If the current selection is now invalid, clear or reset it.
        if (select.value && select.selectedOptions.length && select.selectedOptions[0].disabled) {
            select.value = firstEnabled || '';
        }
    }

    function filterItemsByLocation(row) {
        const locSel = row.querySelector('select[name="location_ids"]');
        const itemSel = row.querySelector('select[name="item_ids"]');
        if (!locSel || !itemSel) {
            return;
        }
        const locId = locSel.value;
        const currentItemId = itemSel.value;
        itemSel.querySelectorAll('option').forEach(function (opt) {
            if (!opt.value) {
                opt.style.display = '';
                return;
            }
            const match = !!locId && opt.getAttribute('data-location-id') === locId;
            opt.style.display = match ? '' : 'none';
        });
        if (currentItemId) {
            const currentOpt = itemSel.querySelector('option[value="' + currentItemId + '"]');
            if (currentOpt && currentOpt.style.display === 'none') {
                itemSel.selectedIndex = 0;
            }
        }
        itemSel.disabled = !locId;
        if (!locId) {
            itemSel.selectedIndex = 0;
        }
        const condSel = row.querySelector('select[name="condition_ids"]');
        if (condSel) {
            condSel.disabled = !itemSel.value;
            if (!itemSel.value) {
                condSel.selectedIndex = 0;
            }
        }
    }

    function syncRemoveButtonStates() {
        if (!itemRowsContainer) {
            return;
        }
        const rows = itemRowsContainer.querySelectorAll('.item-row');
        const showRemove = rows.length > 1;
        rows.forEach(function (row) {
            const btn = row.querySelector('.remove-row');
            if (btn) {
                btn.style.display = showRemove ? '' : 'none';
            }
        });
    }

    function applyLoanToForm() {
        if (!checkinLoanField || !itemRowsContainer) {
            return;
        }
        const select = checkinLoanField.querySelector('select[name="source_request_id"]');
        if (!select || !select.value) {
            return;
        }
        const opt = select.selectedOptions[0];
        if (!opt) {
            return;
        }
        const items = parseLoanItems(opt);
        if (!items.length) {
            return;
        }
        // Capture the item/location/condition dropdown options before we wipe.
        const sampleItem = document.querySelector('#item-rows select[name="item_ids"]');
        const sampleLoc = document.querySelector('#item-rows select[name="location_ids"]');
        const sampleCond = document.querySelector('#item-rows select[name="condition_ids"]');
        const itemOptions = sampleItem ? sampleItem.innerHTML : '';
        const locOptions = sampleLoc ? sampleLoc.innerHTML : '';
        const condOptions = sampleCond ? sampleCond.innerHTML : '<option value="">No update</option>';
        // Replace the item rows with one pre-filled row per loan line, so the
        // member only needs to set the condition before submitting.
        itemRowsContainer.innerHTML = '';
        items.forEach(function (it) {
            const row = document.createElement('div');
            row.className = 'item-row';
            row.innerHTML =
                '<div><label class="label">Location</label>' +
                '<select name="location_ids">' + locOptions + '</select></div>' +
                '<div><label class="label">Item</label>' +
                '<select name="item_ids">' + itemOptions + '</select></div>' +
                '<div><label class="label">Quantity</label><input type="number" name="quantities" value="1" min="1"></div>' +
                '<div class="condition-field"><label class="label">Condition</label><select name="condition_ids">' + condOptions + '</select></div>' +
                '<div class="item-row-remove"><button type="button" class="button-link danger btn-sm remove-row" aria-label="Remove this item row" title="Remove this item row"><span aria-hidden="true">&times;</span></button></div>';
            itemRowsContainer.appendChild(row);
            const locSel = row.querySelector('select[name="location_ids"]');
            const itemSel = row.querySelector('select[name="item_ids"]');
            const qtyInput = row.querySelector('input[name="quantities"]');
            if (locSel) {
                locSel.value = it.locId || '';
            }
            if (itemSel) {
                itemSel.value = it.itemId;
            }
            if (qtyInput) {
                qtyInput.value = it.qty || '1';
            }
            filterItemsByLocation(row);
        });
        syncRemoveButtonStates();
        // Fill the slip code input from the loan's reference code so the form
        // can submit (the slip code is required for member check-ins). The
        // reference code is "KW-0042"; the input accepts just the 4 digits.
        const code = opt.getAttribute('data-code') || '';
        const slipInput = document.querySelector('#quick-view-slip');
        if (slipInput && code) {
            const digits = code.replace(/^KW-/, '').trim();
            slipInput.value = digits;
        }
        // Also set the hidden source_request_id for admin check-ins so the
        // server knows exactly which loan was returned.
        const sourceHidden = document.querySelector('#home-source-request-id');
        if (sourceHidden && select.value) {
            sourceHidden.value = select.value;
        }
        // A loan was chosen, so lock the item/quantity/location/dates; only
        // Condition and Notes remain editable. (Do NOT call updateActionFields
        // here — it would recurse back into applyLoanToForm and the lock below
        // would never run.)
        setLoanLock(true);
    }

    // When a loan is picked from the dropdown the item/quantity/location/dates
    // are derived from that loan, so they are locked; only Condition and Notes
    // (and the loan picker itself) stay editable. locked=false re-enables everything.
    // We avoid `disabled` for the data fields because disabled controls are NOT
    // submitted. Instead we grey them out (via a `locked` class), neutralize the
    // visible control's name (so it isn't submitted), and mirror its value into a
    // hidden input that carries the real name — so the server still receives it once.
    function setLoanLock(locked) {
        if (!itemRowsContainer) {
            return;
        }
        itemRowsContainer.querySelectorAll('.item-row').forEach(function (row) {
            const fields = [
                row.querySelector('select[name="item_ids"]'),
                row.querySelector('input[name="quantities"]'),
                row.querySelector('select[name="location_ids"]'),
            ];
            fields.forEach(function (field) {
                if (!field) {
                    return;
                }
                field.classList.toggle('locked', locked);
                const realName = field.getAttribute('data-real-name') || field.name;
                field.setAttribute('data-real-name', realName);
                const old = field.parentNode.querySelector('input[type="hidden"][data-mirror="' + realName + '"]');
                if (old) {
                    old.remove();
                }
                if (locked) {
                    field.name = '_locked_' + realName;
                    field.setAttribute('tabindex', '-1');
                    const mirror = document.createElement('input');
                    mirror.type = 'hidden';
                    mirror.name = realName;
                    mirror.value = field.value;
                    mirror.setAttribute('data-mirror', realName);
                    field.parentNode.appendChild(mirror);
                } else {
                    field.name = realName;
                    field.removeAttribute('tabindex');
                }
            });
        });
        [expectedReturnField, takenAtField].forEach(function (wrap) {
            if (!wrap) {
                return;
            }
            const field = wrap.querySelector('input');
            if (!field) {
                return;
            }
            field.classList.toggle('locked', locked);
            const realName = field.getAttribute('data-real-name') || field.name;
            field.setAttribute('data-real-name', realName);
            const old = wrap.querySelector('input[type="hidden"][data-mirror="' + realName + '"]');
            if (old) {
                old.remove();
            }
            if (locked) {
                field.name = '_locked_' + realName;
                field.setAttribute('tabindex', '-1');
                const mirror = document.createElement('input');
                mirror.type = 'hidden';
                mirror.name = realName;
                mirror.value = field.value;
                mirror.setAttribute('data-mirror', realName);
                wrap.appendChild(mirror);
            } else {
                field.name = realName;
                field.removeAttribute('tabindex');
            }
        });
        const addBtn = document.querySelector('#add-item-row');
        if (addBtn) {
            addBtn.classList.toggle('locked', locked);
            addBtn.disabled = locked;
        }
        if (itemRowsContainer) {
            itemRowsContainer.querySelectorAll('.remove-row').forEach(function (btn) {
                btn.disabled = locked;
                btn.style.visibility = locked ? 'hidden' : '';
            });
            syncRemoveButtonStates();
        }
    }

    // Condition is required (and flagged with a *) only for check-ins.
    function applyConditionRequirement(required) {
        if (!itemRowsContainer) {
            return;
        }
        itemRowsContainer.querySelectorAll('.item-row').forEach(function (row) {
            const condField = row.querySelector('.condition-field');
            const condLabel = condField ? condField.querySelector('label') : null;
            const condSel = row.querySelector('select[name="condition_ids"]');
            if (condLabel) {
                condLabel.innerHTML = required
                    ? 'Condition <span class="required-star">*</span>'
                    : 'Condition';
            }
            if (condSel) condSel.required = required;
        });
    }

    function updateActionFields() {
        const action = getTransactionType();
        if (checkinLoanField) {
            // The scan area (and, for returns, the loan picker) shows for both
            // check-in and check-out; book-aheads have no gear to scan back in.
            const showScanArea = action === 'check_in' || action === 'check_out';
            checkinLoanField.style.display = showScanArea ? '' : 'none';
            // The loan dropdown only applies when returning gear.
            const loanSelect = checkinLoanField.querySelector('.loan-select');
            const loanLabel = checkinLoanField.querySelector('.loan-select-label');
            if (loanSelect) {
                loanSelect.style.display = action === 'check_in' ? '' : 'none';
                // The loan is required when a member checks in (so the return is
                // filed against the right loan). On the staff/admin quick action,
                // staff may check gear in directly (scan/choose items), so the
                // loan picker is optional there.
                const actionForm = document.querySelector('#home-action-form');
                const isAdminQuickAction = !!actionForm && actionForm.getAttribute('data-admin-quick-action') === 'true';
                loanSelect.required = action === 'check_in' && !isAdminQuickAction;
                if (action !== 'check_in') {
                    loanSelect.value = '';
                }
            }
            if (loanLabel) {
                loanLabel.style.display = action === 'check_in' ? '' : 'none';
            }
            if (action !== 'check_in') {
                const select = checkinLoanField.querySelector('select[name="source_request_id"]');
                if (select) {
                    select.value = '';
                }
                // Leaving check-in unlocks any rows locked by a chosen loan.
                setLoanLock(false);
            }
        }
        filterCheckinLoans();
        if (expectedReturnField) {
            const hide = action === 'check_in';
            expectedReturnField.style.display = hide ? 'none' : '';
            const field = expectedReturnField.querySelector('input');
            if (field) {
                field.required = !hide;
                if (hide) {
                    field.value = '';
                }
            }
        }
        if (takenAtField) {
            const show = action === 'book_ahead';
            takenAtField.style.display = show ? '' : 'none';
            const field = takenAtField.querySelector('input');
            if (field) {
                field.required = show;
                if (!show) {
                    field.value = '';
                }
            }
        }
        if (itemRowsContainer) {
            // Condition is only relevant (and required) for check-ins; hide it
            // for book-aheads and check-outs.
            const isCheckIn = action === 'check_in';
            itemRowsContainer.classList.toggle('hide-condition', !isCheckIn);
            applyConditionRequirement(isCheckIn);
        }
        if (condAllRow) {
            const isCheckIn = action === 'check_in';
            condAllRow.style.display = isCheckIn ? '' : 'none';
            if (!isCheckIn && condAllSelect) {
                condAllSelect.value = '';
            }
        }
        // The staff slip-code input shows only for check-out / check-in actions.
        if (slipInputRow) {
            slipInputRow.style.display = (action === 'check_out' || action === 'check_in') ? '' : 'none';
            // On the staff quick action the slip is required when checking in
            // (a return must be filed against a loan) but optional on check-out.
            const slipInput = slipInputRow.querySelector('input[name="slip_code"]');
            const slipLabel = slipInputRow.querySelector('label[for="quick-view-slip"]');
            const slipRequired = action === 'check_in';
            if (slipInput) {
                slipInput.required = slipRequired;
            }
            if (slipLabel) {
                let star = slipLabel.querySelector('.required-star');
                if (slipRequired && !star) {
                    star = document.createElement('span');
                    star.className = 'required-star';
                    star.textContent = '*';
                    slipLabel.appendChild(document.createTextNode(' '));
                    slipLabel.appendChild(star);
                } else if (!slipRequired && star) {
                    star.remove();
                    // Trim any trailing space we may have appended.
                    if (slipLabel.lastChild && slipLabel.lastChild.nodeType === Node.TEXT_NODE) {
                        slipLabel.lastChild.textContent = slipLabel.lastChild.textContent.replace(/\s+$/, '');
                    }
                }
            }
        }
        // The free-text field is "Purpose" for book-ahead / check-out requests
        // (required) and "Notes" for check-ins (optional — capture return context).
        if (notesLabel) {
            if (action === 'check_in') {
                notesLabel.innerHTML = 'Notes <span class="optional-note">(optional)</span>';
                if (notesTextarea) notesTextarea.required = false;
            } else {
                notesLabel.innerHTML = 'Purpose <span class="required-star">*</span>';
                if (notesTextarea) notesTextarea.required = true;
            }
        }
    }

    if (transactionTypeRadios.length) {
        transactionTypeRadios.forEach((radio) => {
            if (!radio.__qaBound) {
                radio.__qaBound = true;
                radio.addEventListener('change', updateActionFields);
            }
        });
        updateActionFields();
    }

        if (itemRowsContainer) {
            if (!itemRowsContainer.__qaBound) {
                itemRowsContainer.__qaBound = true;
            itemRowsContainer.addEventListener('change', function (event) {
                if (event.target && event.target.matches('select[name="item_ids"]')) {
                    filterCheckinLoans();
                }
                if (event.target && event.target.matches('select[name="location_ids"]')) {
                    const row = event.target.closest('.item-row');
                    if (row) {
                        filterItemsByLocation(row);
                    }
                }
            });
            itemRowsContainer.addEventListener('click', function (event) {
                const btn = event.target.closest('.remove-row');
                if (!btn) {
                    return;
                }
                const row = btn.closest('.item-row');
                if (!row) {
                    return;
                }
                const allRows = itemRowsContainer.querySelectorAll('.item-row');
                if (allRows.length <= 1) {
                    row.querySelectorAll('select, input').forEach(function (f) {
                        if (f.tagName === 'SELECT') {
                            f.selectedIndex = 0;
                        } else if (f.tagName === 'INPUT' && f.type === 'number') {
                            f.value = f.getAttribute('data-default') || '1';
                        }
                    });
                    filterItemsByLocation(row);
                    if (condAllSelect) {
                        condAllSelect.value = '';
                    }
                    return;
                }
                row.remove();
                syncRemoveButtonStates();
                if (typeof filterCheckinLoans === 'function') {
                    filterCheckinLoans();
                }
            });
            }
            itemRowsContainer.querySelectorAll('.item-row').forEach(filterItemsByLocation);
            syncRemoveButtonStates();
        }

        if (condAllSelect) {
            if (!condAllSelect.__qaBound) {
                condAllSelect.__qaBound = true;
            condAllSelect.addEventListener('change', function () {
                const val = condAllSelect.value;
                if (!val) {
                    return;
                }
                if (itemRowsContainer) {
                    itemRowsContainer.querySelectorAll('select[name="condition_ids"]').forEach(function (sel) {
                        sel.value = val;
                    });
                }
                condAllSelect.value = '';
            });
            }
        }


    const sourceRequestSelect = document.querySelector('#source-request-id');
    if (sourceRequestSelect) {
        sourceRequestSelect.addEventListener('change', function () {
            if (sourceRequestSelect.value) {
                applyLoanToForm();
            } else {
                setLoanLock(false);
                if (itemRowsContainer) {
                    resetItemRows();
                }
                const slipInput = document.querySelector('#quick-view-slip');
                if (slipInput) {
                    slipInput.value = '';
                }
                const sourceHidden = document.querySelector('#home-source-request-id');
                if (sourceHidden) {
                    sourceHidden.value = '';
                }
            }
            // Refresh field visibility/required state. This no longer recurses
            // into applyLoanToForm, so the lock set above is preserved.
            if (typeof updateActionFields === 'function') {
                updateActionFields();
            }
            filterCheckinLoans();
        });
    }

    function resetItemRows() {
        if (!itemRowsContainer) {
            return;
        }
        const rows = itemRowsContainer.querySelectorAll('.item-row');
        if (rows.length <= 1) {
            rows.forEach(function (row) {
                const select = row.querySelector('select[name="item_ids"]');
                const quantity = row.querySelector('input[name="quantities"]');
                const location = row.querySelector('select[name="location_ids"]');
                const condition = row.querySelector('select[name="condition_ids"]');
                if (select) select.selectedIndex = 0;
                if (quantity) quantity.value = '1';
                if (location) location.selectedIndex = 0;
                if (condition) condition.selectedIndex = 0;
                filterItemsByLocation(row);
            });
            if (condAllSelect) {
                condAllSelect.value = '';
            }
            return;
        }
        rows.forEach((row, index) => {
            if (index === 0) {
                const select = row.querySelector('select[name="item_ids"]');
                const quantity = row.querySelector('input[name="quantities"]');
                const location = row.querySelector('select[name="location_ids"]');
                const condition = row.querySelector('select[name="condition_ids"]');
                if (select) select.selectedIndex = 0;
                if (quantity) quantity.value = '1';
                if (location) location.selectedIndex = 0;
                if (condition) condition.selectedIndex = 0;
                filterItemsByLocation(row);
                return;
            }
            row.remove();
        });
        if (condAllSelect) {
            condAllSelect.value = '';
        }
        syncRemoveButtonStates();
    }

    const homeForm = document.querySelector('#home-action-form');
    const clearHomeForm = document.querySelector('#clear-home-form');

    if (clearHomeForm && homeForm) {
        if (!clearHomeForm.__qaBound) {
            clearHomeForm.__qaBound = true;
        clearHomeForm.addEventListener('click', function () {
            homeForm.reset();
            resetItemRows();
            setLoanLock(false);
            // Clear any slip-applied locks/mirrors left on item rows.
            homeForm.querySelectorAll('.item-row.locked, .item-row.prefilled').forEach(function (row) {
                row.classList.remove('locked', 'prefilled');
                row.querySelectorAll('.locked').forEach(function (f) {
                    f.classList.remove('locked');
                    f.disabled = false;
                });
                row.querySelectorAll('input[type="hidden"][data-mirror]').forEach(function (m) {
                    m.remove();
                });
                const removeBtn = row.querySelector('.remove-row');
                if (removeBtn) {
                    removeBtn.disabled = false;
                    removeBtn.style.visibility = '';
                }
            });
            if (checkinLoanField) {
                const loanSel = checkinLoanField.querySelector('select[name="source_request_id"]');
                if (loanSel) {
                    loanSel.disabled = false;
                    loanSel.required = false;
                }
            }
            if (typeof updateActionFields === 'function') {
                updateActionFields();
            }
            syncRemoveButtonStates();
        });
        }
    }
    if (addItemRow && itemRowsContainer) {
        if (!addItemRow.__qaBound) {
            addItemRow.__qaBound = true;
            addItemRow.addEventListener('click', function () {
            const firstRow = itemRowsContainer.querySelector('.item-row');
            if (!firstRow) {
                return;
            }
            const clone = firstRow.cloneNode(true);
            const select = clone.querySelector('select[name="item_ids"]');
            const quantity = clone.querySelector('input[name="quantities"]');
            const location = clone.querySelector('select[name="location_ids"]');
            const condition = clone.querySelector('select[name="condition_ids"]');
            if (select) {
                select.selectedIndex = 0;
            }
            if (quantity) {
                quantity.value = '1';
            }
            if (location) {
                location.selectedIndex = 0;
            }
            if (condition) {
                condition.selectedIndex = 0;
            }
            filterItemsByLocation(clone);
            itemRowsContainer.appendChild(clone);
            syncRemoveButtonStates();
        });
        }
    }

    /* ---------- Camera QR scanners ---------- */
    const cameraScanners = (window.__cameraScanners = window.__cameraScanners || []);
    // Release any stream whose video element has been removed from the DOM
    // (e.g. by a live region swap) before we rebuild the registry below.
    cameraScanners.forEach(function (s) {
        if (s && typeof s.stop === 'function' && s.video && !document.contains(s.video)) {
            try {
                s.stop();
            } catch (err) {
                /* ignore */
            }
        }
    });
    cameraScanners.length = 0;

    /* ---------- Slip code helpers (scanned from a desk slip QR) ---------- */

    // Slip codes look like KW-0042 (prefix + dash + number).
    function isSlipCode(value) {
        return /^KW-\d+$/i.test((value || '').trim());
    }

    // Switch the quick-action form to check-in and pre-select the loan whose
    // slip code matches. Returns true if a matching loan was found.
    function applySlipCode(code, statusText) {
        const sourceSelect = document.querySelector('#source-request-id');
        if (!sourceSelect) {
            return false;
        }
        const wanted = (code || '').trim().toLowerCase();
        let match = null;
        sourceSelect.querySelectorAll('option[data-code]').forEach(function (opt) {
            if (!match && opt.getAttribute('data-code').trim().toLowerCase() === wanted) {
                match = opt;
            }
        });
        if (!match) {
            if (statusText) {
                statusText.textContent = `No active loan for slip ${code}`;
            }
            return false;
        }
        // Ensure the loan field is visible by selecting the check-in action.
        const checkInRadio = document.querySelector('input[name="transaction_type"][value="check_in"]');
        if (checkInRadio) {
            checkInRadio.checked = true;
            checkInRadio.dispatchEvent(new Event('change', { bubbles: true }));
        }
        sourceSelect.value = match.value;
        sourceSelect.dispatchEvent(new Event('change', { bubbles: true }));
        if (statusText) {
            statusText.textContent = `Slip ${code} matched — choose the items to return.`;
        }
        return true;
    }

    // Unified slip-code handler for the quick-action scanner. On the member
    // quick-action form a slip means "return this loan" (handled by
    // applySlipCode). On the admin quick-action form, scanning a book-ahead's
    // slip hands the reservation over at pickup (the reserved stock becomes a
    // live check-out) when it hasn't been handed over yet.
    async function handleSlipCode(code, statusText, inputField) {
        const form = inputField.closest('form');
        if (form && form.getAttribute('data-admin-quick-action') === 'true') {
            // Admin context: look the request up by slip code and pre-fill the
            // quick-action form so the desk can act on it directly.
            let data = null;
            try {
                const resp = await fetch(appUrl('requests/lookup-code/?code=' + encodeURIComponent(code)));
                data = await resp.json();
            } catch (err) {
                data = null;
            }
            if (!data || !data.found) {
                if (statusText) {
                    statusText.textContent = `No request found for slip ${code}.`;
                }
                return;
            }
            if (data.voided) {
                if (statusText) {
                    statusText.textContent = `Slip ${code} has been voided.`;
                }
                return;
            }
            if (data.transaction_type !== 'book_ahead' && data.approval_status !== 'approved') {
                if (statusText) {
                    statusText.textContent = `Slip ${code} must be approved before it can be processed.`;
                }
                return;
            }
            if (data.returned) {
                if (statusText) {
                    statusText.textContent = `Slip ${code} has already been returned.`;
                }
                return;
            }

            // Pre-fill the quick-action form's item rows from the request so the
            // exact loan (and its items) is what gets handed over / checked in.
            function fillReservationRows() {
                const rowsContainer = form.querySelector('#item-rows');
                if (!rowsContainer || !data.items || !data.items.length) {
                    return;
                }
                const sampleItem = rowsContainer.querySelector('select[name="item_ids"]');
                const sampleLoc = rowsContainer.querySelector('select[name="location_ids"]');
                const sampleCond = rowsContainer.querySelector('select[name="condition_ids"]');
                const itemOptions = sampleItem ? sampleItem.innerHTML : '';
                const locOptions = sampleLoc ? sampleLoc.innerHTML : '';
                const condOptions = sampleCond ? sampleCond.innerHTML : '<option value="">No update</option>';
                rowsContainer.innerHTML = '';
                data.items.forEach(function (it) {
                    const row = document.createElement('div');
                    row.className = 'item-row';
                    row.innerHTML =
                        '<div><label class="label">Location <span class="required-star">*</span></label>' +
                        '<select name="location_ids" required>' + locOptions + '</select></div>' +
                        '<div><label class="label">Item <span class="required-star">*</span></label>' +
                        '<select name="item_ids" required>' + itemOptions + '</select></div>' +
                        '<div><label class="label">Quantity <span class="required-star">*</span></label><input type="number" name="quantities" value="1" min="1" required></div>' +
                        '<div class="condition-field"><label class="label">Condition</label><select name="condition_ids">' + condOptions + '</select></div>' +
                        '<div class="item-row-remove"><button type="button" class="button-link danger btn-sm remove-row" aria-label="Remove this item row" title="Remove this item row"><span aria-hidden="true">&times;</span></button></div>';
                    rowsContainer.appendChild(row);
                    const locSel = row.querySelector('select[name="location_ids"]');
                    const itemSel = row.querySelector('select[name="item_ids"]');
                    const qtyInput = row.querySelector('input[name="quantities"]');
                    if (locSel) locSel.value = it.location_id ? String(it.location_id) : '';
                    if (itemSel) itemSel.value = String(it.item_id);
                    if (qtyInput) qtyInput.value = it.quantity || '1';
                    filterItemsByLocation(row);
                });
                syncRemoveButtonStates();
                if (typeof updateActionFields === 'function') {
                    updateActionFields();
                }
                // Pre-fill the expected return (datetime-local needs YYYY-MM-DDTHH:MM)
                // and the Purpose/Notes from the looked-up request.
                const returnInput = form.querySelector('input[name="expected_return"]');
                if (returnInput && data.expected_return) {
                    let dt = String(data.expected_return).replace('Z', '');
                    if (dt.includes('+')) {
                        dt = dt.split('+')[0];
                    }
                    if (dt.includes('T') && dt.split('T')[1].includes(':')) {
                        const parts = dt.split('T')[1].split(':');
                        returnInput.value = dt.split('T')[0] + 'T' + parts[0] + ':' + (parts[1] || '00');
                    } else {
                        returnInput.value = dt;
                    }
                }
                const notesInput = form.querySelector('textarea[name="notes"]');
                // Notes/Purpose are copied from the request for check-outs and
                // handovers, but not for check-ins (a return captures its own
                // context via the Condition field).
                if (notesInput && data.notes && getTransactionType() !== 'check_in') {
                    notesInput.value = data.notes;
                }
                // Lock the item/quantity/location fields so staff can't change
                // what the slip loaded, but keep them submittable: the visible
                // control is disabled and a hidden input carries the real name +
                // value. Condition stays editable so staff can record the return
                // state.
                 rowsContainer.querySelectorAll('.item-row').forEach(function (row) {
                     row.classList.add('prefilled');
                     ['item_ids', 'quantities', 'location_ids'].forEach(function (name) {
                         const field = row.querySelector('[name="' + name + '"]');
                         if (!field) {
                             return;
                         }
                         const realName = field.getAttribute('data-real-name') || field.name;
                         field.setAttribute('data-real-name', realName);
                         field.classList.add('locked');
                         field.disabled = true;
                         const old = field.parentNode.querySelector('input[type="hidden"][data-mirror="' + realName + '"]');
                         if (old) {
                             old.remove();
                         }
                         const mirror = document.createElement('input');
                         mirror.type = 'hidden';
                         mirror.name = realName;
                         mirror.value = field.value;
                         mirror.setAttribute('data-mirror', realName);
                         field.parentNode.appendChild(mirror);
                     });
                     const removeBtn = row.querySelector('.remove-row');
                     if (removeBtn) {
                         removeBtn.disabled = true;
                         removeBtn.style.visibility = 'hidden';
                     }
                 });
            }

            function selectAction(type) {
                const radio = form.querySelector('input[name="transaction_type"][value="' + type + '"]');
                if (radio) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }

            // Decide the action from the loan's history: a book-ahead that has
            // NOT been handed over yet is handed over at pickup (its reserved
            // stock becomes a live check-out). Everything else that is still out
            // — a direct check-out (stock leaves at approval, so handed_over is
            // never set on it) or an already handed-over book-ahead — is a live
            // loan and must be returned via check-in, not check-out.
            const needsHandOver = data.transaction_type === 'book_ahead' && !data.handed_over;
            const handOverInput = form.querySelector('#home-hand-over-id');
            const sourceInput = form.querySelector('#home-source-request-id');
            if (needsHandOver) {
                selectAction('check_out');
                fillReservationRows();
                if (sourceInput) sourceInput.value = '';
                if (handOverInput) handOverInput.value = String(data.pk || '');
                if (statusText) {
                    statusText.textContent = `Loaded reservation ${code} for ${data.person_name || 'member'} — review and submit to hand over.`;
                }
            } else {
                selectAction('check_in');
                fillReservationRows();
                if (handOverInput) handOverInput.value = '';
                if (sourceInput) sourceInput.value = String(data.pk || '');
                if (statusText) {
                    statusText.textContent = `Loaded slip ${code} for ${data.person_name || 'member'} — review and submit to check in.`;
                }
            }
            // The form is pre-filled for staff to review; submission is left to
            // the Submit action button (or a follow-up scan), so the populated
            // fields stay visible instead of being submitted instantly.
            // The slip code was only used to look the loan up; clear the
            // asset_tag field so it is not submitted as an item SKU.
            const assetTagInput = form.querySelector('#home-scan-asset');
            if (assetTagInput) {
                assetTagInput.value = '';
            }
            return;
        }
        // Member context: switch to check-in and pre-select the matching loan.
        applySlipCode(code, statusText);
    }

    // Collapse the scan field (text input + open camera) once a scan lands.
    function closeScanField() {
        const section = document.querySelector('#scan-section');
        if (section) {
            section.style.display = 'none';
        }
        const toggle = document.querySelector('#scan-toggle');
        if (toggle) {
            toggle.textContent = 'Use QR / SKU scan';
        }
        const panel = document.querySelector('#home-camera-panel');
        if (panel) {
            panel.style.display = 'none';
        }
    }

    function createCameraScanner(config) {
        const openButtons = document.querySelectorAll(config.openButtonSelector);
        const stopButton = document.querySelector(config.stopButtonSelector);
        const panel = document.querySelector(config.panelSelector);
        const video = document.querySelector(config.videoSelector);
        const statusText = document.querySelector(config.statusSelector);
        const inputField = document.querySelector(config.inputSelector);
        const bulkInput = config.bulkInputSelector ? document.querySelector(config.bulkInputSelector) : null;
        const bulkList = config.bulkListSelector ? document.querySelector(config.bulkListSelector) : null;

        if (!openButtons.length || !stopButton || !panel || !video || !statusText || !inputField) {
            return;
        }

        let detector = null;
        let jsQRFallback = false;
        let mediaStream = null;
        let scanning = false;
        const scanCanvas = document.createElement('canvas');
        const scanContext = scanCanvas.getContext('2d', { willReadFrequently: true });

        function setupDetector() {
            detector = null;
            jsQRFallback = false;
            if (window.BarcodeDetector) {
                try {
                    detector = new BarcodeDetector({ formats: ['qr_code'] });
                    return;
                } catch (error) {
                    detector = null;
                }
            }
            jsQRFallback = typeof window.jsQR === 'function';
        }

        async function startCamera() {
            if (scanning) {
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                statusText.textContent =
                    'Camera needs a secure (HTTPS) connection. Open the site using https:// instead of http://';
                return;
            }

            setupDetector();
            if (!detector && !jsQRFallback) {
                statusText.textContent =
                    'QR scanning is not available in this browser. You can still type the SKU.';
            }

            try {
                mediaStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'environment' },
                    audio: false,
                });
                video.srcObject = mediaStream;
                try {
                    await video.play();
                } catch (playErr) {
                    /* Autoplay may be blocked; the feed usually still renders. */
                }
                panel.style.display = 'block';
                statusText.textContent = 'Scanning for QR code...';
                scanning = true;
                requestAnimationFrame(scanFrame);
            } catch (error) {
                const name = error && error.name;
                if (name === 'NotAllowedError' || name === 'SecurityError') {
                    statusText.textContent =
                        'Camera permission was denied. Allow camera access and try again.';
                } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
                    statusText.textContent = 'No camera was found on this device.';
                } else {
                    statusText.textContent =
                        'Unable to access the camera. Allow camera access and try again.';
                }
            }
        }

        function stopCamera() {
            scanning = false;
            if (mediaStream) {
                mediaStream.getTracks().forEach((track) => track.stop());
                mediaStream = null;
            }
            if (video) {
                video.srcObject = null;
            }
            panel.style.display = 'none';
            statusText.textContent = 'Camera stopped.';
        }

        async function scanFrame() {
            if (!scanning || !mediaStream) {
                return;
            }
            if (!document.contains(video)) {
                stopCamera();
                return;
            }
            try {
                if (detector) {
                    const results = await detector.detect(video);
                    if (results && results.length) {
                        handleScannedCode(results[0].rawValue.trim());
                    }
                } else if (jsQRFallback && video.videoWidth && video.readyState >= 2) {
                    if (scanCanvas.width !== video.videoWidth || scanCanvas.height !== video.videoHeight) {
                        scanCanvas.width = video.videoWidth;
                        scanCanvas.height = video.videoHeight;
                    }
                    scanContext.drawImage(video, 0, 0, scanCanvas.width, scanCanvas.height);
                    const imageData = scanContext.getImageData(0, 0, scanCanvas.width, scanCanvas.height);
                    const result = window.jsQR(imageData.data, imageData.width, imageData.height, {
                        inversionAttempts: 'dontInvert',
                    });
                    if (result && result.data) {
                        handleScannedCode(result.data.trim());
                    }
                }
            } catch (error) {
                console.warn('QR scan error:', error);
            }
            if (scanning) {
                requestAnimationFrame(scanFrame);
            }
        }

        async function handleScannedCode(code) {
            if (!code) {
                return;
            }
            inputField.value = code;
            if (bulkInput && bulkList) {
                const currentCodes = bulkInput.value
                    .split(',')
                    .map((value) => value.trim())
                    .filter((value) => value);
                if (!currentCodes.includes(code)) {
                    currentCodes.push(code);
                    bulkInput.value = currentCodes.join(',');
                    bulkList.textContent = '';
                    currentCodes.forEach((item) => {
                        const chip = document.createElement('div');
                        chip.className = 'scan-chip';
                        chip.textContent = item;
                        bulkList.appendChild(chip);
                    });
                    statusText.textContent = `Scanned: ${code}`;
                } else {
                    statusText.textContent = `Already scanned ${code}`;
                }
                return;
            }

            scanning = false;
            let sku = code;
            let itemId = null;
            let locationId = null;

            // A slip QR encodes ".../return-by-code/?code=KW-XXXX". When scanned
            // on the member's quick-action form, switch to check-in and pre-select
            // the matching loan so the return is filed against the right slip.
            try {
                const parsed = new URL(code);
                if (parsed.pathname.indexOf('/return-by-code/') !== -1) {
                    const slipCode = parsed.searchParams.get('code');
                    if (slipCode) {
                        handleSlipCode(slipCode, statusText, inputField);
                        // The slip code is NOT an asset tag — leave the asset
                        // field empty so it isn't submitted and rejected by the
                        // server; the loan select + item rows carry the data.
                        inputField.value = '';
                        stopCamera();
                        closeScanField();
                        return;
                    }
                }
            } catch (err) {
                /* Not a URL — fall through to SKU/raw handling below. */
            }

            // A raw slip code (e.g. "KW-0042") typed or scanned also maps to the
            // matching loan on the quick-action form.
            if (isSlipCode(code)) {
                handleSlipCode(code, statusText, inputField);
                // The slip code is not an asset tag; keep the asset field empty
                // so it isn't submitted and rejected by the server.
                inputField.value = '';
                stopCamera();
                closeScanField();
                return;
            }

            try {
                const parsed = new URL(code);
                if (parsed.pathname.startsWith('/items/')) {
                    const urlSku = parsed.searchParams.get('sku');
                    const pk = parsed.pathname.split('/').filter(Boolean)[1];
                    if (urlSku) {
                        sku = urlSku;
                    } else if (pk) {
                        try {
                            const resp = await fetch(appUrl('item-lookup/?pk=' + pk));
                            const data = await resp.json();
                            if (data && data.found) {
                                sku = data.sku || code;
                                itemId = data.id;
                                locationId = data.location_id;
                            }
                        } catch (err) {
                            /* Leave value as the URL if lookup fails. */
                        }
                    }
                }
            } catch (err) {
                /* Not a URL — use the raw value (e.g. a typed/scanning SKU). */
            }
            inputField.value = sku;
            statusText.textContent = `Scanned: ${sku}`;
            if (!itemId) {
                try {
                    const resp = await fetch(appUrl('item-lookup/?sku=' + encodeURIComponent(sku)));
                    const data = await resp.json();
                    if (data && data.found) {
                        itemId = data.id;
                        locationId = data.location_id;
                    }
                } catch (err) {
                    /* No item match — leave the field populated with the SKU. */
                }
            }
            if (itemId) {
                document.querySelectorAll('select[name="item_ids"]').forEach(function (sel) {
                    const matches = Array.prototype.slice.call(sel.options)
                        .some(function (opt) { return opt.value === String(itemId); });
                    if (matches) {
                        sel.value = String(itemId);
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                });
                if (locationId) {
                    document.querySelectorAll('select[name="location_ids"]').forEach(function (sel) {
                        const matches = Array.prototype.slice.call(sel.options)
                            .some(function (opt) { return opt.value === String(locationId); });
                        if (matches) {
                            sel.value = String(locationId);
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    });
                }
            }
            inputField.dispatchEvent(new Event('input', { bubbles: true }));
            stopCamera();
            // A completed scan (item or slip) should also collapse the scan field
            // so the member is left looking at the populated quick-action form.
            closeScanField();
        }

        openButtons.forEach(function (openButton) {
            openButton.addEventListener('click', function () {
                startCamera();
            });
        });

        stopButton.addEventListener('click', function () {
            stopCamera();
        });

        cameraScanners.push({
            stop: stopCamera,
            isActive: function () {
                return scanning || !!mediaStream;
            },
            panel: panel,
            video: video,
        });
    }

    createCameraScanner({
        openButtonSelector: '.camera-scan-home-open',
        stopButtonSelector: '#stop-camera-home',
        panelSelector: '#home-camera-panel',
        videoSelector: '#home-scan-video',
        statusSelector: '#home-scan-status',
        inputSelector: '#home-scan-asset',
    });

    const maintenanceForm = document.querySelector('#maintenance-form');
    const clearMaintenanceForm = document.querySelector('#clear-maintenance-form');
    const clearMaintenanceAsset = document.querySelector('#clear-maintenance-asset');

    if (clearMaintenanceForm && maintenanceForm) {
        clearMaintenanceForm.addEventListener('click', function () {
            maintenanceForm.reset();
            if (clearMaintenanceAsset) {
                clearMaintenanceAsset.click();
            }
            const panel = document.querySelector('#maintenance-camera-panel');
            if (panel) {
                panel.style.display = 'none';
            }
        });
    }

    if (clearMaintenanceAsset) {
        clearMaintenanceAsset.addEventListener('click', function () {
            const input = document.querySelector('#maintenance-scan-asset');
            if (input) {
                input.value = '';
            }
        });
    }

    const toggleManageItems = document.querySelector('#toggle-manage-items');
    const manageItemsPanel = document.querySelector('#manage-maintenance-items');
    if (toggleManageItems && manageItemsPanel) {
        const syncManageButton = function () {
            const open = manageItemsPanel.style.display !== 'none';
            toggleManageItems.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggleManageItems.textContent = open ? 'Close' : 'Manage items';
        };
        toggleManageItems.addEventListener('click', function () {
            manageItemsPanel.style.display = manageItemsPanel.style.display !== 'none' ? 'none' : 'block';
            window.__managePanelOpen = manageItemsPanel.style.display !== 'none';
            syncManageButton();
        });
        const closeManageItems = document.querySelector('#close-manage-items');
        if (closeManageItems) {
            closeManageItems.addEventListener('click', function () {
                manageItemsPanel.style.display = 'none';
                window.__managePanelOpen = false;
                syncManageButton();
            });
        }
    }

    // Confirm before writing an item off from maintenance (destructive: removes
    // the units from total stock). The Return action needs no confirmation.
    document.querySelectorAll('button[data-action="writeoff"]').forEach(function (btn) {
        btn.addEventListener('click', function (event) {
            const itemName = btn.getAttribute('data-item-name') || 'this item';
            if (!window.confirm('Write off ' + itemName + '? The units will be removed from stock and cannot be returned.')) {
                event.preventDefault();
            }
        });
    });

    /* ---------- Generic data-confirm for destructive actions ---------- */
    document.addEventListener('click', function (event) {
        const el = event.target.closest('[data-confirm]');
        if (!el) {
            return;
        }
        const msg = el.getAttribute('data-confirm');
        if (msg && !window.confirm(msg)) {
            event.preventDefault();
            event.stopPropagation();
        }
    });

    /* ---------- Quick action keyboard shortcuts ---------- */
    if (homeForm) {
        homeForm.addEventListener('keydown', function (event) {
            if ((event.ctrlKey || event.metaKey) && event.key === 'k') {
                event.preventDefault();
                const slipInput = document.querySelector('#quick-view-slip');
                const assetInput = document.querySelector('#home-scan-asset');
                if (slipInput && slipInput.offsetParent !== null) {
                    slipInput.focus();
                } else if (assetInput) {
                    assetInput.focus();
                }
            }
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
                event.preventDefault();
                const submitBtn = homeForm.querySelector('button[type="submit"]');
                if (submitBtn && !submitBtn.disabled) {
                    submitBtn.click();
                }
            }
        });
    }

    createCameraScanner({
        openButtonSelector: '#camera-scan-maintenance',
        stopButtonSelector: '#camera-scan-maintenance',
        panelSelector: '#maintenance-camera-panel',
        videoSelector: '#maintenance-scan-video',
        statusSelector: '#maintenance-scan-status',
        inputSelector: '#maintenance-scan-asset',
    });

    /* ---------- Staff quick action: slip-code input above the camera button ---------- */
    // The slip field lives inside #home-action-form (above the "Open camera
    // scan" button). Typed codes are written into the form's asset field so
    // handleSlipCode routes through the admin flow and fills/ submits the form.
    const slipInput = document.querySelector('#quick-view-slip');
    if (slipInput) {
        const statusText = document.querySelector('#quick-view-status');
        const homeAsset = document.querySelector('#home-scan-asset');
        const clearSlip = document.querySelector('#quick-view-clear-slip');

        function setStatus(msg) {
            if (statusText) {
                statusText.textContent = msg;
            }
        }

        function runSlipLookup() {
            // The field only holds the 4 digits; the "KW-" prefix is shown
            // separately, so reconstruct the full slip code for the lookup.
            const digits = (slipInput.value || '').replace(/\D/g, '').slice(0, 4);
            if (digits.length !== 4 || !homeAsset) {
                if (statusText) {
                    statusText.textContent = 'Enter the 4-digit slip number.';
                }
                return;
            }
            const code = 'KW-' + digits;
            homeAsset.value = code;
            handleSlipCode(code, statusText, homeAsset);
        }

        // Restrict input to up to 4 digits as the user types.
        slipInput.addEventListener('input', function () {
            const cleaned = (slipInput.value || '').replace(/\D/g, '').slice(0, 4);
            if (cleaned !== slipInput.value) {
                slipInput.value = cleaned;
            }
        });

        if (clearSlip) {
            clearSlip.addEventListener('click', function () {
                slipInput.value = '';
                if (homeAsset) {
                    homeAsset.value = '';
                }
                // Clearing the slip also resets the whole quick-action form
                // (item rows, action, dates, loan lock) so staff start fresh.
                const homeFormClear = document.querySelector('#clear-home-form');
                if (homeFormClear) {
                    homeFormClear.click();
                } else if (homeForm) {
                    homeForm.reset();
                    if (typeof resetItemRows === 'function') {
                        resetItemRows();
                    }
                    if (typeof setLoanLock === 'function') {
                        setLoanLock(false);
                    }
                    if (typeof updateActionFields === 'function') {
                        updateActionFields();
                    }
                }
                setStatus('Enter the 4-digit slip number or scan with the camera.');
            });
        }

        slipInput.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter') {
                return;
            }
            event.preventDefault();
            runSlipLookup();
        });
        // Also resolve the slip when the field loses focus (e.g. user types the
        // code and taps elsewhere instead of pressing Enter).
        slipInput.addEventListener('blur', runSlipLookup);
    }
}

/* Stop any running cameras when the page unloads (bound once). */
window.addEventListener('beforeunload', function () {
    const scanners = window.__cameraScanners || [];
    scanners.forEach(function (s) {
        const stop = s && typeof s.stop === 'function' ? s.stop : s;
        if (typeof stop === 'function') {
            try {
                stop();
            } catch (err) {
                /* ignore */
            }
        }
    });
});

/* ---------- Collapsible notification groups ---------- */
function initCollapsibles() {
    document.querySelectorAll('[data-collapsible]').forEach(function (group) {
        const toggle = group.querySelector('.collapsible-toggle');
        const body = group.querySelector('.collapsible-body');
        if (!toggle || !body) {
            return;
        }
        toggle.addEventListener('click', function () {
            const expanded = toggle.getAttribute('aria-expanded') === 'true';
            toggle.setAttribute('aria-expanded', String(!expanded));
            body.hidden = expanded;
        });
    });
}

/* Copy-to-clipboard for slip codes and similar references. Any button with a
   data-copy-target (id of the element whose textContent should be copied) gets
   a click handler that copies the value and briefly confirms it. */
document.addEventListener('click', function (event) {
    const btn = event.target.closest('[data-copy-target]');
    if (!btn) {
        return;
    }
    const target = document.getElementById(btn.getAttribute('data-copy-target'));
    if (!target) {
        return;
    }
    const text = (target.textContent || '').trim();
    if (!text || !navigator.clipboard) {
        return;
    }
    navigator.clipboard.writeText(text).then(function () {
        const original = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(function () {
            btn.textContent = original;
        }, 1500);
    });
});

/* ---------- Slip modal popup ---------- */
/* A slip can be rendered as an in-page modal (e.g. right after a member
   submits a request). Auto-open it on load, and close on the close button,
   the backdrop, or the Escape key. */
function initModals() {
    const modals = document.querySelectorAll('.modal-overlay[data-autoshow]');
    modals.forEach(function (modal) {
        openModal(modal);
    });

    // Event delegation for any [data-close-modal] buttons across the app.
    document.addEventListener('click', function (event) {
        const closer = event.target.closest('[data-close-modal]');
        if (!closer) {
            return;
        }
        const target = document.getElementById(closer.getAttribute('data-close-modal'));
        if (target) {
            closeModal(target);
        }
    });
}

/* ---------- First-login onboarding popup ---------- */
/* An interactive, role-specific step carousel. Step state lives client-side;
   any dismissal clears the server session flag (skip-for-now) and the final
   "Let's go!" marks the popup seen permanently so it never returns. */
function initOnboarding() {
    const overlay = document.getElementById('onboarding-modal');
    if (!overlay) {
        return;
    }

    // Immediately clear the session flag so this popup only ever shows on the
    // very first page load after login. The modal itself stays open until the
    // user dismisses it; subsequent navigations won't re-trigger it.
    const csrf = getCsrfToken();
    fetch(appUrl('onboarding/mark-seen/'), {
        method: 'POST',
        headers: {
            'x-requested-with': 'XMLHttpRequest',
            'X-CSRFToken': csrf,
        },
        credentials: 'same-origin',
    }).catch(function () { /* best-effort */ });

    const steps = Array.prototype.slice.call(overlay.querySelectorAll('.onboarding-step'));
    if (!steps.length) {
        return;
    }
    const dotsWrap = document.getElementById('onboarding-dots');
    const backBtn = document.getElementById('onboarding-back');
    const nextBtn = document.getElementById('onboarding-next');
    const skipBtn = document.getElementById('onboarding-skip');
    const closeBtn = document.getElementById('onboarding-close');

    let index = 0;

    // Build one dot per step (clickable to jump).
    steps.forEach(function (step, i) {
        if (!dotsWrap) {
            return;
        }
        const dot = document.createElement('button');
        dot.type = 'button';
        dot.className = 'onboarding-dot';
        dot.setAttribute('aria-label', 'Go to step ' + (i + 1));
        dot.addEventListener('click', function () {
            goTo(i);
        });
        dotsWrap.appendChild(dot);
    });
    const dots = dotsWrap ? Array.prototype.slice.call(dotsWrap.children) : [];

    function goTo(i) {
        index = Math.max(0, Math.min(i, steps.length - 1));
        steps.forEach(function (step, si) {
            step.classList.toggle('is-active', si === index);
        });
        dots.forEach(function (dot, di) {
            dot.classList.toggle('is-active', di === index);
        });
        const last = index === steps.length - 1;
        if (backBtn) {
            backBtn.hidden = index === 0;
        }
        // The Next button becomes "Let's go!" on the final step and dismisses
        // the popup permanently; on earlier steps it simply advances.
        if (nextBtn) {
            if (last) {
                nextBtn.textContent = "Let's go!";
                nextBtn.classList.add('onboarding-done');
            } else {
                nextBtn.textContent = 'Next';
                nextBtn.classList.remove('onboarding-done');
            }
        }
    }

    function dismiss(permanent) {
        const csrf = getCsrfToken();
        const body = new URLSearchParams();
        if (permanent) {
            body.set('permanent', '1');
        }
        fetch(appUrl('onboarding/mark-seen/'), {
            method: 'POST',
            headers: {
                'x-requested-with': 'XMLHttpRequest',
                'X-CSRFToken': csrf,
            },
            body: body.toString(),
            credentials: 'same-origin',
        }).catch(function () { /* best-effort; hide anyway */ });
        overlay.classList.remove('is-open');
        overlay.setAttribute('hidden', '');
    }

    if (nextBtn) {
        nextBtn.addEventListener('click', function () {
            if (index === steps.length - 1) {
                dismiss(true);
            } else {
                goTo(index + 1);
            }
        });
    }
    if (backBtn) {
        backBtn.addEventListener('click', function () { goTo(index - 1); });
    }
    if (closeBtn) {
        closeBtn.addEventListener('click', function () { dismiss(true); });
    }

    // Backdrop click (outside the card) and Escape dismiss permanently —
    // this popup is shown only on the user's very first login, so any
    // dismissal marks it as seen forever.
    overlay.addEventListener('click', function (event) {
        if (event.target === overlay) {
            dismiss(true);
        }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape') {
            return;
        }
        if (overlay.classList.contains('is-open')) {
            dismiss(true);
        }
    });

    goTo(0);
}

function openModal(modal) {
    if (!modal) {
        return;
    }
    window.openModal = openModal;
    const previouslyFocused = document.activeElement;
    modal.classList.add('is-open');
    modal.removeAttribute('hidden');
    modal._previousFocus = previouslyFocused && previouslyFocused !== document.body ? previouslyFocused : null;
    const focusTarget = modal.querySelector('[data-close-modal], .button-link, button');
    if (focusTarget) {
        focusTarget.focus();
    }
}

function closeModal(modal) {
    if (!modal) {
        return;
    }
    modal.classList.remove('is-open');
    modal.setAttribute('hidden', '');
    if (modal._previousFocus && typeof modal._previousFocus.focus === 'function') {
        modal._previousFocus.focus();
    }
}

// Close on backdrop click + Escape.
document.addEventListener('click', function (event) {
    const overlay = event.target.closest('.modal-overlay');
    if (overlay && event.target === overlay) {
        closeModal(overlay);
    }
});
document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') {
        return;
    }
    const open = document.querySelector('.modal-overlay.is-open');
    if (open) {
        closeModal(open);
    }
});

/* Print a slip. Instead of printing the whole page (which forces the
   browser's own header/footer with the site title and can't be hidden via
   CSS), we open a clean window that contains ONLY the slip markup and print
   that. The window title is the slip code so the printed header, if shown,
   is minimal and there is no site chrome. */
document.addEventListener('click', function (event) {
    const btn = event.target.closest('[data-print-target]');
    if (!btn) {
        return;
    }
    const target = document.getElementById(btn.getAttribute('data-print-target'));
    if (!target) {
        return;
    }

    const slipHtml = target.innerHTML;
    const codeMatch = slipHtml.match(/slip-doc-code[^>]*>([^<]+)</);
    const title = codeMatch ? codeMatch[1].trim() : 'Slip';

    const win = window.open('', '_blank', 'width=800,height=900');
    if (!win) {
        // Popup blocked: fall back to the in-page print path.
        document.body.classList.add('is-printing-slip');
        target.classList.add('is-print-region');
        const cleanup = function () {
            document.body.classList.remove('is-printing-slip');
            target.classList.remove('is-print-region');
            window.removeEventListener('afterprint', cleanup);
        };
        window.addEventListener('afterprint', cleanup);
        window.print();
        return;
    }

    win.document.open();
    win.document.write(
        '<!DOCTYPE html><html><head><meta charset="utf-8">' +
        '<title>' + title + '</title>' +
        '<style>' +
        'html,body{margin:0;padding:0;background:#fff;color:#111;' +
        'font-family:"Inter",system-ui,sans-serif;}' +
        '.slip-print-wrap{max-width:620px;margin:14mm auto;' +
        'border:1px solid #111;border-radius:6px;padding:22px 26px;}' +
        '.slip-doc-header{display:flex;align-items:center;gap:14px;' +
        'padding-bottom:14px;border-bottom:2px solid #111;}' +
        '.slip-doc-logo{width:72px;height:72px;object-fit:contain;}' +
        '.slip-doc-brand{display:flex;flex-direction:column;line-height:1.2;}' +
        '.slip-doc-brand-name{font-size:1.25rem;font-weight:800;}' +
        '.slip-doc-brand-sub{font-size:.85rem;letter-spacing:.14em;' +
        'text-transform:uppercase;color:#555;}' +
        '.slip-doc-status-chip{margin-left:auto;padding:6px 12px;' +
        'border:1.5px solid #111;border-radius:999px;font-size:.72rem;' +
        'font-weight:700;letter-spacing:.06em;text-transform:uppercase;}' +
        '.slip-doc-coderow{display:flex;align-items:baseline;flex-wrap:wrap;' +
        'gap:8px 14px;margin:16px 0;padding:14px 16px;background:#f4f1e8;' +
        'border:1.5px solid #111;border-radius:6px;}' +
        '.slip-doc-code-label{font-size:.72rem;letter-spacing:.1em;' +
        'text-transform:uppercase;color:#555;}' +
        '.slip-doc-code{font-family:"Courier New",monospace;font-size:2rem;' +
        'font-weight:800;letter-spacing:.12em;}' +
        '.slip-doc-type{margin-left:auto;font-size:.78rem;font-weight:700;' +
        'letter-spacing:.06em;text-transform:uppercase;color:#333;' +
        'padding:3px 10px;border:1px solid #111;border-radius:4px;}' +
        '.slip-doc-qr{margin-left:auto;width:56px;height:56px;background:#fff;' +
        'border:1px solid #111;border-radius:3px;padding:2px;' +
        'object-fit:contain;}' +
        '.slip-doc-qr svg,.slip-doc-qr img{width:100%;height:100%;display:block;}' +
        '.slip-doc-member{margin:0 0 16px;border:1px solid #111;' +
        'border-radius:6px;overflow:hidden;}' +
        '.slip-doc-member-head{padding:7px 12px;background:#111;color:#fff;' +
        'font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;' +
        'font-weight:700;}' +
        '.slip-doc-member-grid{width:100%;border-collapse:collapse;table-layout:fixed;font-size:.86rem;}' +
        '.slip-doc-member-grid col.slip-doc-member-label{width:84px;}' +
        '.slip-doc-member-grid th,.slip-doc-member-grid td{' +
        'text-align:left;vertical-align:top;padding:6px 12px;border-top:1px solid #ddd;' +
        'overflow-wrap:anywhere;word-break:break-word;}' +
        '.slip-doc-member-grid tr:first-child th,' +
        '.slip-doc-member-grid tr:first-child td{border-top:none;}' +
        '.slip-doc-member-grid th{font-size:.66rem;letter-spacing:.06em;' +
        'text-transform:uppercase;color:#555;font-weight:700;white-space:nowrap;}' +
        '.slip-doc-member-grid td{font-weight:600;}' +
        '.slip-doc-section-label{font-size:.68rem;letter-spacing:.1em;' +
        'text-transform:uppercase;color:#555;font-weight:700;margin:0 0 6px 2px;}' +
        '.slip-doc-meta{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:.9rem;}' +
        '.slip-doc-meta th,.slip-doc-meta td{text-align:left;vertical-align:top;' +
        'padding:5px 8px 5px 0;}' +
        '.slip-doc-meta th{width:88px;font-size:.68rem;letter-spacing:.08em;' +
        'text-transform:uppercase;color:#555;font-weight:700;}' +
        '.slip-doc-meta td{padding-right:20px;font-weight:600;}' +
        '.slip-doc-items{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:.9rem;}' +
        '.slip-doc-items thead th{text-align:left;font-size:.68rem;letter-spacing:.08em;' +
        'text-transform:uppercase;color:#555;border-bottom:1.5px solid #111;padding:6px 8px;}' +
        '.slip-doc-items tbody td{padding:7px 8px;border-bottom:1px solid #ccc;}' +
        '.slip-doc-items tbody td:last-child{color:#555;}' +
        '.slip-doc-items tfoot td{padding:8px;font-weight:800;border-top:2px solid #111;}' +
        '.slip-doc-qty{width:46px;text-align:center;}' +
        '.slip-doc-notes{margin-bottom:16px;padding:10px 12px;border:1px solid #ccc;' +
        'border-radius:6px;}' +
        '.slip-doc-notes-label{display:block;font-size:.68rem;letter-spacing:.08em;' +
        'text-transform:uppercase;color:#555;margin-bottom:4px;}' +
        '.slip-doc-notes p{margin:0;font-size:.9rem;}' +
        '.slip-doc-history{margin-bottom:16px;padding:12px 14px;border:1px solid #111;' +
        'border-radius:6px;}' +
        '.slip-doc-history-label{display:block;font-size:.68rem;letter-spacing:.08em;' +
        'text-transform:uppercase;color:#555;margin-bottom:8px;font-weight:700;}' +
        '.slip-doc-timeline{list-style:none;margin:0;padding:0;}' +
        '.slip-doc-event{position:relative;padding:0 0 10px 18px;border-left:2px solid #ccc;}' +
        '.slip-doc-event:last-child{padding-bottom:0;border-left-color:transparent;}' +
        '.slip-doc-event::before{content:"";position:absolute;left:-5px;top:3px;' +
        'width:8px;height:8px;border-radius:50%;background:#111;}' +
        '.slip-doc-event-danger::before{background:#b91c1c;}' +
        '.slip-doc-event-success::before{background:#2e7d32;}' +
        '.slip-doc-event-title{display:block;font-size:.9rem;font-weight:700;}' +
        '.slip-doc-event-meta{display:block;font-size:.78rem;color:#555;margin-top:1px;}' +
        '.slip-doc-foot{text-align:center;border-top:1px dashed #bbb;padding-top:12px;}' +
        '.slip-doc-foot-note{margin:0 0 8px;font-size:.8rem;color:#444;}' +
        '.slip-doc-fineprint{font-size:.72rem;color:#777;margin:0;}' +
        '@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact;}}' +
        '</style></head><body><div class="slip-print-wrap">' + slipHtml + '</div>' +
        '<script>window.onload=function(){window.focus();window.print();' +
        'window.onafterprint=function(){window.close();};};<\/script>' +
        '</body></html>'
    );
    win.document.close();
});

document.addEventListener('DOMContentLoaded', function () {
    initGlobal();
    gearroomInit();
    initCollapsibles();
    initModals();
    initOnboarding();
    tickRelativeTimes();
    // Keep "minutes overdue" / "due in" labels ticking as time passes,
    // independent of data edits (a version bump alone won't fire here).
    setInterval(tickRelativeTimes, 30000);
});

/* ---------- Live updates: in-place region merging ---------- */
(function () {
    let lastVersion = null;

    // Replace every CSRF token value with a placeholder so two fragments that
    // differ ONLY by their (per-request) token are treated as identical.
    function _stripCsrf(s) {
        return s.replace(
            /(name="csrfmiddlewaretoken"[^>]*value=")[^"]*(")/g,
            '$1*STRIPPED*$2'
        );
    }

    // Track the element currently under the pointer so we can tell whether the
    // user is mid-interaction (hovering / about to click) inside a region. A
    // full region swap underneath a hovering cursor would otherwise yank the
    // tile the user is reaching for out from under their click.
    let pointerElem = null;
    // True while a mouse/touch button is held down — we must never swap a
    // region's DOM mid-press, or the click that completes the press lands on a
    // detached element and is lost (e.g. a catalog group fails to expand).
    let pointerPressed = false;
    document.addEventListener('pointermove', function (event) {
        pointerElem = event.target;
    }, { passive: true });
    document.addEventListener('pointerout', function (event) {
        if (event.target === pointerElem) {
            pointerElem = null;
        }
    }, { passive: true });
    document.addEventListener('pointerdown', function () { pointerPressed = true; }, { passive: true });
    document.addEventListener('pointerup', function () { pointerPressed = false; }, { passive: true });
    // If the pointer leaves the window mid-press, treat the press as released.
    document.addEventListener('pointercancel', function () { pointerPressed = false; }, { passive: true });

    // Find the id of the [data-region] container that currently holds a
    // *text-entry* element the user is editing, so we can tell the server to
    // skip refreshing it (never yank a field out from under the user mid-edit).
    // We deliberately ignore buttons/links: clicking an action button is a
    // deliberate act and its container SHOULD refresh afterwards — otherwise a
    // button that lives inside the very region it updates (e.g. Approve inside
    // #pending-requests) would stay focused and block its own refresh.
    function isTextEntry(el) {
        if (!el) {
            return false;
        }
        if (el.tagName === 'TEXTAREA') {
            return true;
        }
        if (el.tagName === 'SELECT') {
            return true;
        }
        if (el.tagName === 'INPUT') {
            const t = el.type;
            return t === 'text' || t === 'search' || t === 'number' ||
                t === 'email' || t === 'url' || t === 'tel' || t === 'password';
        }
        return false;
    }

    function regionIdOf(el) {
        while (el && el !== document.body) {
            if (el.hasAttribute && el.hasAttribute('data-region')) {
                return el.getAttribute('data-region');
            }
            el = el.parentElement;
        }
        return '';
    }

    function focusedRegionId() {
        const el = document.activeElement;
        if (!isTextEntry(el)) {
            return '';
        }
        return regionIdOf(el);
    }

    // True when one of the registered camera scanners is actively streaming a
    // <video> that lives inside `container`. Used to block live region merges
    // that would otherwise tear down the camera mid-scan.
    function regionHasLiveCamera(container) {
        if (!container) {
            return false;
        }
        const scanners = window.__cameraScanners || [];
        return scanners.some(function (s) {
            return s && typeof s.isActive === 'function' && s.isActive() &&
                s.video && container.contains(s.video);
        });
    }

    // True when the user is actively interacting with the given region: a
    // text-entry inside it has focus, the pointer is currently hovering over
    // it (about to click), or a mouse/touch button is held down over it. In
    // those cases we must not swap the region out from under the user. Other
    // state the user sets (an expanded catalog group, a typed quantity) is
    // preserved by mergeRegion itself, so it does not need to block the merge
    // — that would otherwise freeze the region forever.
    function regionIsInteracting(id) {
        const container = document.querySelector('[data-region="' + id + '"]');
        if (!container) {
            return false;
        }
        if (regionHasLiveCamera(container)) {
            return true;
        }
        if (document.activeElement && isTextEntry(document.activeElement)) {
            if (regionIdOf(document.activeElement) === id) {
                return true;
            }
        }
        if (pointerElem && container.contains(pointerElem)) {
            return true;
        }
        if (pointerPressed && pointerElem && container.contains(pointerElem)) {
            return true;
        }
        return false;
    }

    // Update the top-bar chrome (notification / request / cart badges) in place
    // from the JSON state endpoint. These live OUTSIDE #main, so a body-only
    // update would never refresh them — leaving the badge stale even after a
    // notification is pushed through.
    function updateBadge(name, count) {
        count = parseInt(count, 10) || 0;
        document.querySelectorAll('[data-nav-badge="' + name + '"]').forEach(function (badge) {
            if (count > 0) {
                badge.textContent = String(count);
                badge.removeAttribute('hidden');
            } else {
                badge.textContent = '';
                badge.setAttribute('hidden', '');
            }
        });
    }

    function applyState(state) {
        if (state.unread_notification_count !== undefined) {
            updateBadge('unread', state.unread_notification_count);
        }
        if (state.pending_request_count !== undefined) {
            updateBadge('pending', state.pending_request_count);
        }
        if (state.cart_total !== undefined) {
            updateBadge('cart', state.cart_total);
        }
    }

    // Merge one server-rendered region fragment into its live container,
    // WITHOUT touching anything else on the page (forms, scroll, focus,
    // modals, camera panels all survive). Only swaps when the HTML differs,
    // so we avoid needless DOM churn / flicker.
    function mergeRegion(id, html, force) {
        // Regions are keyed by their data-region attribute (not a DOM id), so
        // look them up that way. Many regions (e.g. home-info, catalog-sections)
        // intentionally have no separate id, so getElementById would miss them.
        const container = document.querySelector('[data-region="' + id + '"]');
        if (!container) {
            return;
        }
        // Never yank a region out from under the user mid-interaction (a group
        // they expanded, a quantity they are typing, or a tile they are
        // hovering to click). The next tick will retry once they move on.
        // `force` bypasses this for an explicit user action (e.g. clicking
        // "Clear cart") where the merge IS the expected response.
        if (!force && regionIsInteracting(id)) {
            return;
        }
        // Unwrap the fragment the server returned (it is the full region
        // element, so grab its inner content).
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        const incoming = tmp.firstElementChild;
        const newInner = incoming ? incoming.innerHTML : html;
        // Server-rendered fragments carry a fresh CSRF token on every request,
        // so the value of every csrfmiddlewaretoken input differs from the one
        // already in the DOM even when nothing else changed. Normalise those
        // tokens away before comparing, otherwise we'd "see" a change on every
        // poll and re-render the region — which would re-collapse an expanded
        // catalog group the instant the user opened it.
        if (_stripCsrf(newInner) === _stripCsrf(container.innerHTML)) {
            return; // identical (bar the token) — don't disturb
        }
        // Capture the user's current interactive state so a refresh doesn't
        // reset it: which catalog groups they have expanded.
        let savedExpanded = null;
        if (id === 'catalog-sections') {
            savedExpanded = {};
            container.querySelectorAll('.group-body').forEach(function (body) {
                if (!body.classList.contains('collapsed')) {
                    savedExpanded[body.id] = true;
                }
            });
        }
        // Account "For you" notifications: preserve the collapsible open
        // state the user set, since the server renders it collapsed by default.
        let keepExpanded = null;
        if (id === 'account-notifications') {
            const toggle = container.querySelector('.collapsible-toggle');
            if (toggle && toggle.getAttribute('aria-expanded') === 'true') {
                keepExpanded = true;
            }
        }
        // The Quick-action form is split into two live sub-regions so a data
        // push (e.g. an admin approving a loan) only refreshes the *loan-
        // dependent* parts — never the user's half-filled form body.
        //
        //   home-qa-actions : the action radios (a new loan can add "Check in")
        //   home-qa-loans   : the loan picker + camera/scan area
        //
        // We must preserve the user's in-progress state across the swap: the
        // action they picked, and which loan they selected.
        let savedAction = null;
        let savedLoan = null;
        if (id === 'home-qa-actions') {
            const checked = container.querySelector('input[name="transaction_type"]:checked');
            savedAction = checked ? checked.value : null;
        }
        if (id === 'home-qa-loans') {
            const sel = container.querySelector('#source-request-id');
            savedLoan = sel ? sel.value : '';
        }
        (window.__cameraScanners || []).forEach(function (s) {
            if (s && typeof s.isActive === 'function' && s.isActive() &&
                s.video && container.contains(s.video)) {
                try {
                    s.stop();
                } catch (err) {
                    /* ignore */
                }
            }
        });
        container.innerHTML = newInner;
        // Restore the user's chosen action on the freshly-rendered radios. If
        // the action they had selected is no longer offered (e.g. their only
        // loan was returned), leave the server's default selection in place.
        if (id === 'home-qa-actions' && savedAction) {
            const radio = container.querySelector(
                'input[name="transaction_type"][value="' + savedAction + '"]'
            );
            if (radio) {
                radio.checked = true;
            }
        }
        // Restore the selected loan (and the open camera panel) on the new
        // picker if that loan still exists among the options.
        if (id === 'home-qa-loans') {
            if (savedLoan) {
                const sel = container.querySelector('#source-request-id');
                if (sel) {
                    let stillThere = false;
                    sel.querySelectorAll('option').forEach(function (opt) {
                        if (opt.value === savedLoan) {
                            stillThere = true;
                        }
                    });
                    if (stillThere) {
                        sel.value = savedLoan;
                    }
                }
            }
        }
        // Any quick-action sub-region swap changes what fields are valid, so
        // re-run the field-visibility/required logic against the (preserved)
        // action. The radios themselves are direct-bound, so wire a fresh
        // change listener too.
        if ((id === 'home-qa-actions' || id === 'home-qa-loans') &&
            typeof gearroomInit === 'function') {
            gearroomInit();
            if (typeof updateActionFields === 'function') {
                updateActionFields();
            }
        }
        // The Manage-items panel is server-rendered closed (display:none), but
        // the user may have it open. Preserve that open state across the merge
        // so a live refresh never snaps the panel shut or corrupts the table.
        if (id === 'manage-maintenance' && window.__managePanelOpen) {
            container.style.display = 'block';
            const btn = document.getElementById('toggle-manage-items');
            if (btn) {
                btn.setAttribute('aria-expanded', 'true');
                btn.textContent = 'Close';
            }
        }
        if (keepExpanded) {
            const toggle = container.querySelector('.collapsible-toggle');
            const body = container.querySelector('.collapsible-body');
            if (toggle) {
                toggle.setAttribute('aria-expanded', 'true');
            }
            if (body) {
                body.hidden = false;
            }
        }
        // Restore the catalog interactive state we captured above.
        if (id === 'catalog-sections') {
            if (savedExpanded) {
                container.querySelectorAll('.group-body').forEach(function (body) {
                    if (savedExpanded[body.id]) {
                        body.classList.remove('collapsed');
                        const toggle = body.previousElementSibling &&
                            body.previousElementSibling.querySelector('.group-toggle');
                        if (toggle) {
                            toggle.setAttribute('aria-expanded', 'true');
                        }
                    }
                });
                const toggleAllBtn = document.getElementById('catalog-toggle-all');
                if (toggleAllBtn) {
                    const anyCollapsed = container.querySelectorAll('.group-body.collapsed').length > 0;
                    toggleAllBtn.textContent = anyCollapsed ? 'Expand all' : 'Collapse all';
                }
            }
            if (typeof applyCatalogFilters === 'function') {
                applyCatalogFilters();
            }
        }
        // NOTE: we deliberately do NOT re-run gearroomInit() here. The
        // regions are server-rendered display fragments (tables / stats / lists);
        // their interactivity (collapsible toggles, copy, modal close, form
        // submits) is handled by the document-level delegated listeners bound
        // once at load, so re-binding would only duplicate listeners on the
        // stable elements outside the regions and is unnecessary.
    }

    async function mergeRegions(forceId) {
        const path = window.location.pathname;
        const focused = focusedRegionId();
        // The catalog product grid is NOT live-refreshed by the main poller —
        // only stock quantities update there, via a separate lightweight call.
        // Refreshing the whole grid would re-render it server-side and discard
        // the user's active filter state.
        const skip = ['catalog-sections'];
        if (focused) {
            skip.push(focused);
        }
        document.querySelectorAll('[data-region]').forEach(function (region) {
            const id = region.getAttribute('data-region');
            if (!id || id === focused || id === forceId || id === 'catalog-sections') {
                return;
            }
            if (regionIsInteracting(id)) {
                skip.push(id);
            }
        });
        const skipParam = skip.length
            ? '&skip=' + skip.map(encodeURIComponent).join(',')
            : '';
        const fullPath = window.location.pathname + window.location.search;
        const qs = 'path=' + encodeURIComponent(fullPath) +
            (focused ? '&focused=' + encodeURIComponent(focused) : '') + skipParam;
        try {
            const resp = await fetch(appUrl('live/region/?') + qs, {
                credentials: 'same-origin',
                cache: 'no-store',
            });
            if (!resp.ok) {
                return;
            }
            const data = await resp.json();
            if (!data || !data.regions) {
                return;
            }
            const returnedIds = Object.keys(data.regions);
            returnedIds.forEach(function (id) {
                mergeRegion(id, data.regions[id], id === forceId);
            });
            // A region the server no longer renders (e.g. the cart panel once
            // the cart is emptied, since home.html only outputs it inside
            // `{% if cart_items %}`) must be removed from the DOM — the server
            // can't hand us a fragment to "clear" it. Skip any region we asked
            // the server to omit, and leave alone anything the user is mid-
            // interaction with (it'll be reconciled on the next tick). When the
            // user just triggered `forceId` (e.g. clicked "Clear cart"), bypass
            // the interaction guard even if the pointer is still hovering the
            // now-removed button — the removal IS the expected response.
            document.querySelectorAll('[data-region]').forEach(function (region) {
                const id = region.getAttribute('data-region');
                if (!id || returnedIds.indexOf(id) !== -1 || skip.indexOf(id) !== -1) {
                    return;
                }
                if (id !== forceId && regionIsInteracting(id)) {
                    return;
                }
                region.remove();
            });
            if (data.version !== undefined) {
                lastVersion = data.version;
            }
        } catch (err) {
            /* Network hiccup — the next tick will retry. */
        }
    }

    function handleVersion(version) {
        if (version === null || version === undefined) {
            return;
        }
        if (lastVersion === null) {
            lastVersion = version;
            return;
        }
        if (version === lastVersion) {
            return;
        }
        // A version change means some data moved — pull the fresh fragments
        // in place (no full reload).
        lastVersion = version;
        mergeRegions();
    }

    // Inline action forms tagged data-live-submit (e.g. announcement show/hide,
    // pin, delete; notification mark-read; request approve/reject; maintenance
    // return/write-off) submit via fetch and refresh the live regions in place
    // instead of doing a full POST -> redirect that reloads the page and jumps
    // the user back to the top. The server already bumps the data version via
    // its save signals, so a region merge reflects the change without a nav.
    document.addEventListener('submit', function (event) {
        const form = event.target.closest('form[data-live-submit], form.cart-clear-form, form.cart-remove-form');
        if (!form) {
            return;
        }
        event.preventDefault();
        // Respect a submitter button's formaction (e.g. maintenance "write off"
        // vs "return" share one form but post to different URLs).
        const submitter = event.submitter;
        let action = form.getAttribute('action');
        if (submitter && submitter.getAttribute('formaction')) {
            action = submitter.getAttribute('formaction');
        }
        const btn = submitter || form.querySelector('button[type="submit"]');
        if (btn) {
            btn.disabled = true;
        }
        const formData = new FormData(form);
        if (submitter && submitter.name) {
            formData.append(submitter.name, submitter.value);
        }
        fetch(action, {
            method: 'POST',
            headers: { 'x-requested-with': 'XMLHttpRequest' },
            body: formData,
            credentials: 'same-origin',
        })
            .then(function () {
                // The change is recorded server-side (version bumped); pull the
                // fresh fragments in place. Force-refresh the region this form
                // lives in, even if the pointer is still hovering it (e.g. right
                // after clicking "Clear cart"), since the merge IS the expected
                // response to the click. Top-bar badges may also shift.
                const forceId = regionIdOf(form);
                mergeRegions(forceId);
                fetch(appUrl('live/state/'), { credentials: 'same-origin', cache: 'no-store' })
                    .then(function (r) { return r.ok ? r.json() : null; })
                    .then(function (d) { if (d) { applyState(d); } })
                    .catch(function () { /* ignore */ });
                if (typeof updateCatalogStock === 'function') {
                    updateCatalogStock();
                }
            })
            .catch(function () {
                // Network failure: fall back to the normal form submission so
                // the action still goes through (page will reload).
                if (btn) {
                    btn.disabled = false;
                }
                form.submit();
            });
    });

    // Poll the lightweight state endpoint: updates the top-bar badges instantly
    // (dynamic, no context loss) and triggers an in-place region merge when
    // the data version changes.
    setInterval(function () {
        fetch(appUrl('live/state/'), { credentials: 'same-origin', cache: 'no-store' })
            .then(function (resp) { return resp.ok ? resp.json() : null; })
            .then(function (data) {
                if (!data) {
                    return;
                }
                applyState(data);
                handleVersion(data.version);
            })
            .catch(function () { /* ignore */ });
    }, 2000);

    // Also merge regions on a fixed cadence so the page body stays live even
    // for changes the version counter might lag on (e.g. the same second).
    setInterval(mergeRegions, 5000);

    // Catalog stock poller — updates only the quantity badges on product cards
    // without touching the rest of the grid. This keeps the user's filters and
    // scroll position intact while still showing live stock numbers.
    function updateCatalogStock() {
        try {
            const cards = document.querySelectorAll('.product-card');
            if (!cards.length) {
                return;
            }
            const ids = [];
            cards.forEach(function (card) {
                const pk = card.getAttribute('data-item-id');
                if (pk) ids.push(pk);
            });
            if (!ids.length) {
                return;
            }
            const url = appUrl('catalog/stock/') + '?ids=' + encodeURIComponent(ids.join(','));
            fetch(url, { credentials: 'same-origin', cache: 'no-store' })
                .then(function (resp) {
                    if (!resp.ok) {
                        console.error('catalog/stock/ failed:', resp.status, resp.statusText);
                    }
                    return resp.ok ? resp.json() : null;
                })
                .then(function (data) {
                    if (!data || !data.items) {
                        return;
                    }
                    cards.forEach(function (card) {
                        const pk = card.getAttribute('data-item-id');
                        if (!pk) return;
                        const info = data.items[pk];
                        if (!info) return;
                        const badge = card.querySelector('.product-card-badge');
                        if (!badge) return;
                        if (info.available > 0) {
                            badge.className = 'pill pill-success product-card-badge';
                            badge.textContent = info.available + ' in stock';
                        } else {
                            badge.className = 'pill pill-danger product-card-badge';
                            badge.textContent = 'Out';
                        }
                        card.setAttribute('data-available', String(info.available));
                        const qtyInput = card.querySelector('.catalog-qty');
                        if (qtyInput && parseInt(qtyInput.value, 10) > info.available) {
                            qtyInput.value = Math.max(1, info.available);
                        }
                        if (qtyInput) {
                            qtyInput.max = Math.max(1, info.available);
                        }
                        const hiddenQty = card.querySelector('.catalog-qty-hidden');
                        if (hiddenQty) {
                            hiddenQty.value = qtyInput ? qtyInput.value : '1';
                        }
                    });
                })
                .catch(function (err) {
                    console.error('catalog/stock/ error:', err);
                });
        } catch (err) {
            console.error('updateCatalogStock error:', err);
        }
    }

    if (document.querySelector('.product-card')) {
        setInterval(updateCatalogStock, 3000);
        updateCatalogStock();
    }

    const backToTop = document.getElementById('back-to-top');
    if (backToTop) {
        function updateBackToTop() {
            if (window.scrollY > 400) {
                backToTop.classList.add('is-visible');
                backToTop.removeAttribute('aria-hidden');
                backToTop.removeAttribute('tabindex');
            } else {
                backToTop.classList.remove('is-visible');
                backToTop.setAttribute('aria-hidden', 'true');
                backToTop.setAttribute('tabindex', '-1');
            }
        }
        window.addEventListener('scroll', updateBackToTop, { passive: true });
        backToTop.addEventListener('click', function () {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
        updateBackToTop();
    }

    (function initPushPermissionBar() {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
        const bar = document.getElementById('push-permission-bar');
        if (!bar) return;
        const enableBtn = document.getElementById('push-permission-enable');
        const dismissBtn = document.getElementById('push-permission-dismiss');
        if (!enableBtn || !dismissBtn) return;

        if (sessionStorage.getItem('push_prompt_dismissed') === '1') {
            bar.hidden = true;
            return;
        }

        enableBtn.addEventListener('click', async function () {
            bar.hidden = true;
            const perm = await Notification.requestPermission();
            if (perm === 'granted') {
                for (const attempt of [1, 2, 3]) {
                    const sub = await subscribeUser();
                    if (sub) break;
                    await new Promise(function (r) { setTimeout(r, 500); });
                }
            }
        });

        dismissBtn.addEventListener('click', function () {
            bar.hidden = true;
            sessionStorage.setItem('push_prompt_dismissed', '1');
        });

        bar.hidden = false;
    })();

    if ('serviceWorker' in navigator && 'PushManager' in window) {
        const vapidPublicKey = (window.__WEBPUSH_VAPID_PUBLIC_KEY__ || '').trim();
        const csrfToken = getCsrfToken();
        let currentSubscription = null;

        function urlBase64ToUint8Array(base64String) {
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
            const rawData = window.atob(base64);
            const outputArray = new Uint8Array(rawData.length);
            for (let i = 0; i < rawData.length; ++i) {
                outputArray[i] = rawData.charCodeAt(i);
            }
            return outputArray;
        }

        async function ensureServiceWorker() {
            const registration = await navigator.serviceWorker.register('/sw.js');
            return registration;
        }

        async function subscribeUser() {
            if (!vapidPublicKey) {
                console.warn('Web push VAPID public key is not set.');
                return null;
            }
            try {
                const registration = await ensureServiceWorker();
                const subscription = await registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
                });
                await saveSubscription(subscription);
                currentSubscription = subscription;
                return subscription;
            } catch (err) {
                console.error('Web push subscription failed:', err);
                return null;
            }
        }

        async function saveSubscription(subscription) {
            const endpoint = subscription.endpoint;
            const auth = subscription.getKey('auth');
            const p256dh = subscription.getKey('p256dh');
            const body = JSON.stringify({
                endpoint: endpoint,
                keys: {
                    auth: btoa(String.fromCharCode.apply(null, new Uint8Array(auth))),
                    p256dh: btoa(String.fromCharCode.apply(null, new Uint8Array(p256dh))),
                },
            });
            const resp = await fetch(appUrl('webpush/subscribe/'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: body,
            });
            return resp.ok;
        }

        async function unsubscribeUser() {
            if (!currentSubscription) {
                const registration = await ensureServiceWorker();
                currentSubscription = await registration.pushManager.getSubscription();
            }
            if (!currentSubscription) {
                return true;
            }
            const endpoint = currentSubscription.endpoint;
            const resp = await fetch(appUrl('webpush/unsubscribe/'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: JSON.stringify({ endpoint: endpoint }),
            });
            const ok = resp.ok;
            if (ok) {
                await currentSubscription.unsubscribe();
                currentSubscription = null;
            }
            return ok;
        }

        async function initWebPush() {
            try {
                const registration = await ensureServiceWorker();
                const subscription = await registration.pushManager.getSubscription();
                currentSubscription = subscription || null;
            } catch (err) {
                console.warn('Web push init skipped.', err);
            }
        }

        initWebPush();
    }
})();

