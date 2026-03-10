// Dino-Bildgen-UI — Frontend Logic

const FIELDS = [
    'style_header', 'logline', 'child_char_block', 'scene_block',
    'brush_guide', 'medium_block', 'negative_block', 'ref_instruction'
];

// --- Script Loading ---

document.getElementById('scriptFile').addEventListener('change', loadScript);

async function loadScript() {
    const input = document.getElementById('scriptFile');
    if (!input.files.length) return;

    const formData = new FormData();
    formData.append('file', input.files[0]);

    try {
        const resp = await fetch('/api/parse-script', { method: 'POST', body: formData });
        const data = await resp.json();

        // Fill blocks
        for (const field of FIELDS) {
            const ta = document.querySelector(`textarea[data-field="${field}"]`);
            if (ta && data[field]) {
                ta.value = data[field];
                autoResize(ta);
            }
        }

        // Update dirs
        if (data.ref_dir) {
            document.getElementById('refDirInput').value = data.ref_dir;
        }
        if (data.output_dir) {
            // Update output name based on loaded script
            const name = input.files[0].name.replace('.py', '').replace('generate_', 'charsheet-');
            document.getElementById('outputName').value = name;
        }
        if (data.temperature !== undefined) {
            document.getElementById('temperature').value = data.temperature;
            document.getElementById('tempValue').textContent = data.temperature;
        }
        if (data.variants) {
            document.getElementById('variants').value = data.variants;
        }

        showBlocks();
        updatePreview();
        loadRefs();
        toast('Script geladen: ' + input.files[0].name, 'success');
    } catch (e) {
        toast('Fehler beim Parsen: ' + e.message, 'error');
    }
}


function resetBlocks() {
    for (const field of FIELDS) {
        const ta = document.querySelector(`textarea[data-field="${field}"]`);
        if (ta) ta.value = '';
    }
    showBlocks();
    updatePreview();
    loadRefs();
}

function showBlocks() {
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('blocksContainer').style.display = 'flex';
    document.getElementById('blocksContainer').style.flexDirection = 'column';
    document.getElementById('blocksContainer').style.gap = '12px';

    // Auto-resize all textareas
    document.querySelectorAll('textarea').forEach(autoResize);
}


// --- Block Toggle ---

function toggleBlock(headerEl) {
    const body = headerEl.nextElementSibling;
    body.style.display = body.style.display === 'none' ? '' : 'none';
}


// --- Prompt Building ---

function getBlocks() {
    const blocks = {};
    for (const field of FIELDS) {
        const ta = document.querySelector(`textarea[data-field="${field}"]`);
        blocks[field] = ta ? ta.value.trim() : '';
    }
    return blocks;
}

function buildPrompt(blocks) {
    // Reihenfolge: Style → Logline → Character → Scene/Composition → Brush → Medium → Negative
    const parts = [];
    if (blocks.style_header) parts.push(blocks.style_header);
    if (blocks.logline) parts.push(blocks.logline);
    if (blocks.child_char_block) parts.push(blocks.child_char_block);
    if (blocks.scene_block) parts.push(blocks.scene_block);
    if (blocks.brush_guide) parts.push(blocks.brush_guide);
    if (blocks.medium_block) parts.push(blocks.medium_block);
    if (blocks.negative_block) parts.push(blocks.negative_block);

    return parts.join('\n\n');
}

function updatePreview() {
    const blocks = getBlocks();
    const prompt = buildPrompt(blocks);

    // Update preview
    document.getElementById('promptPreview').textContent = prompt;
    document.getElementById('totalChars').textContent = prompt.length.toLocaleString();

    // Update per-block counters
    for (const field of FIELDS) {
        const card = document.querySelector(`[data-block="${field}"]`);
        if (!card) continue;
        const counter = card.querySelector('[data-counter]');
        const ta = card.querySelector('textarea');
        if (counter && ta) {
            counter.textContent = ta.value.length > 0 ? `${ta.value.length} Z` : '';
        }
    }
}


// --- Prompt Preview Toggle ---

function togglePreview() {
    const el = document.getElementById('promptPreview');
    const arrow = document.getElementById('previewArrow');
    el.classList.toggle('open');
    arrow.textContent = el.classList.contains('open') ? '▲' : '▼';
}


// --- Active Refs (used for generation) ---
let activeRefs = []; // [{name, path, data_url, size_kb, reason?, score?}]
let activeRefsLabel = 'Refs';

function getActiveRefPaths() {
    return activeRefs.map(r => r.path);
}

function renderActiveRefs(refs, label) {
    if (label) activeRefsLabel = label;
    activeRefs = refs || activeRefs;
    const grid = document.getElementById('refGrid');
    if (activeRefs.length === 0) {
        grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:16px;color:var(--text-dim);font-size:12px">Keine Referenzbilder</div>';
        return;
    }
    const header = `<div style="grid-column:1/-1;font-size:10px;color:var(--green);margin-bottom:2px">${activeRefsLabel} (${activeRefs.length})</div>`;
    grid.innerHTML = header + activeRefs.map((img, i) => {
        const reason = img.reason ? `<span class="ref-reason">${img.reason}</span>` : '';
        const src = img.data_url || `/api/registry/image/${img.registry_index}`;
        return `
            <div class="ref-thumb active-ref" title="${img.name}${img.reason ? ' — ' + img.reason : ''} (${img.size_kb || '?'} KB)">
                <img src="${src}" alt="${img.name}" onclick="showLightbox(this.src)">
                <button class="ref-delete" onclick="removeActiveRef(${i})">&times;</button>
                <span class="ref-name">${img.name}</span>
                ${reason}
            </div>
        `;
    }).join('');
}

function removeActiveRef(index) {
    activeRefs.splice(index, 1);
    renderActiveRefs();
    syncRefPaths();
    toast(`Ref entfernt (${activeRefs.length} übrig)`, 'info');
}

function syncRefPaths() {
    fetch('/api/refs/set-paths', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: getActiveRefPaths() }),
    });
}

async function addRefFromSuggestion(registryIndex) {
    try {
        const sugEl = document.querySelector(`[data-sug-index="${registryIndex}"]`);
        if (!sugEl) return;

        const path = sugEl.dataset.path;
        if (!path) return;

        // Already active?
        if (activeRefs.some(r => r.path === path)) {
            toast('Bereits als Ref aktiv', 'info');
            return;
        }

        // Get suggestion info
        const titel = sugEl.querySelector('.suggestion-title')?.textContent || path.split('/').pop();
        const reason = sugEl.querySelector('.suggestion-reason')?.textContent || '';

        // Add to active refs
        activeRefs.push({
            name: path.split('/').pop(),
            path: path,
            data_url: `/api/registry/image/${registryIndex}`,
            registry_index: registryIndex,
            reason: reason,
        });

        renderActiveRefs();
        syncRefPaths();
        sugEl.classList.add('ref-added');
        toast(`Ref hinzugefügt: ${titel} (${activeRefs.length} total)`, 'success');
    } catch (e) {
        console.error('Ref hinzufügen fehlgeschlagen:', e);
    }
}

function renderRefSuggestions(suggestions) {
    const container = document.getElementById('suggestionsContainer');
    const section = document.getElementById('suggestionsSection');
    if (!container || !section) return;

    // Show only suggestions that aren't already active refs
    const activeNames = new Set(activeRefs.map(r => r.name || r.path?.split('/').pop()));

    container.innerHTML = suggestions.map(sug => {
        const fileName = sug.datei.split('/').pop();
        const isActive = activeNames.has(fileName);
        return `
            <div class="suggestion-item ${isActive ? 'ref-added' : ''}"
                 data-sug-index="${sug.index}"
                 data-path="/Users/alexanderpauckner/Kinderbuch/Comic_Projekt_2025/Dino-Buch/${sug.datei}"
                 onclick="addRefFromSuggestion(${sug.index})"
                 title="Score: ${sug.score} — ${sug.reason}">
                <img src="/api/registry/image/${sug.index}" loading="lazy" alt="${sug.titel}">
                <div class="suggestion-info">
                    <span class="suggestion-title">${sug.titel}</span>
                    <span class="suggestion-reason">${sug.reason}${sug.bewertung ? ' ⭐' + sug.bewertung : ''}</span>
                </div>
            </div>
        `;
    }).join('');

    container.style.display = 'block';
    section.style.display = 'block';
}


// --- Reference Images (directory mode) ---

async function loadRefs() {
    try {
        const resp = await fetch('/api/refs');
        const data = await resp.json();

        const grid = document.getElementById('refGrid');
        if (data.images.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:16px;color:var(--text-dim);font-size:12px">Keine Referenzbilder</div>';
            return;
        }

        grid.innerHTML = data.images.map(img => `
            <div class="ref-thumb" title="${img.name} (${img.size_kb} KB)">
                <img src="${img.data_url}" alt="${img.name}">
                <button class="ref-delete" onclick="deleteRef('${img.name}')">&times;</button>
                <span class="ref-name">${img.name}</span>
            </div>
        `).join('');
    } catch (e) {
        console.error('Refs laden fehlgeschlagen:', e);
    }
}

async function deleteRef(name) {
    await fetch(`/api/refs/${encodeURIComponent(name)}`, { method: 'DELETE' });
    loadRefs();
}

async function uploadRefs(files) {
    for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch('/api/refs/upload', { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.ok) {
            // Add uploaded file to active refs with preview
            const reader = new FileReader();
            reader.onload = () => {
                activeRefs.push({
                    name: file.name,
                    path: `${document.getElementById('refDirInput').value}/${file.name}`,
                    data_url: reader.result,
                    size_kb: Math.round(file.size / 1024),
                });
                renderActiveRefs();
                syncRefPaths();
            };
            reader.readAsDataURL(file);
        }
    }
    toast(`${files.length} Bild(er) hinzugefügt`, 'success');
}

async function changeRefDir(dir) {
    const formData = new FormData();
    formData.append('dir', dir);
    await fetch('/api/refs/dir', { method: 'POST', body: formData });
    activeRefs = [];
    loadRefs();
}


// --- Output Dir ---

async function changeOutputDir(dir) {
    const formData = new FormData();
    formData.append('dir', dir);
    await fetch('/api/output/dir', { method: 'POST', body: formData });
    toast('Output-Ordner: ' + dir, 'info');
}

async function openOutputDir() {
    await fetch('/api/output/open', { method: 'POST' });
}


// --- Generate ---

async function generate() {
    const blocks = getBlocks();
    const prompt = buildPrompt(blocks);

    if (!prompt) {
        toast('Kein Prompt vorhanden', 'error');
        return;
    }

    const btn = document.getElementById('generateBtn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Generiere...';

    const resultsContainer = document.getElementById('resultsContainer');
    resultsContainer.innerHTML = '<div style="padding:20px;text-align:center"><span class="spinner"></span><p style="margin-top:10px;color:var(--text-dim);font-size:12px">Warte auf Gemini API...</p></div>';

    try {
        const resp = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                prompt: prompt,
                ref_instruction: blocks.ref_instruction,
                temperature: parseFloat(document.getElementById('temperature').value),
                variants: parseInt(document.getElementById('variants').value),
                output_name: document.getElementById('outputName').value,
                ref_paths: activeRefs.length > 0 ? getActiveRefPaths() : undefined,
            }),
        });

        const data = await resp.json();

        if (data.error) {
            toast(data.error, 'error');
            resultsContainer.innerHTML = '';
            return;
        }

        // Render results
        resultsContainer.innerHTML = '';
        for (const result of data.results) {
            if (result.error) {
                resultsContainer.innerHTML += `<div style="padding:10px;color:var(--accent);font-size:12px">Variante ${result.variant}: ${result.error}</div>`;
                continue;
            }
            for (const part of result.parts) {
                if (part.type === 'image') {
                    resultsContainer.innerHTML += `
                        <div class="result-image">
                            <img src="${part.data_url}" alt="${part.filename}" onclick="showLightbox(this.src)">
                            <div class="result-meta">
                                ${part.filename} &middot; ${part.size_kb} KB &middot; ${result.elapsed}s
                            </div>
                        </div>
                    `;
                } else if (part.type === 'text') {
                    resultsContainer.innerHTML += `<div style="padding:8px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border)">${part.content}</div>`;
                }
            }
        }

        toast(`${data.results.length} Variante(n) generiert — ${data.refs_used} Refs`, 'success');
    } catch (e) {
        toast('Fehler: ' + e.message, 'error');
        resultsContainer.innerHTML = '';
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}


// --- Output Images ---

async function loadOutputImages() {
    try {
        const resp = await fetch('/api/output/images');
        const data = await resp.json();
        const container = document.getElementById('resultsContainer');

        if (data.images.length === 0) {
            container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px">Keine Bilder im Output-Ordner</div>';
            return;
        }

        container.innerHTML = data.images.map(img => `
            <div class="result-image">
                <img src="/api/output/image/${encodeURIComponent(img.name)}" alt="${img.name}" onclick="showLightbox(this.src)" loading="lazy">
                <div class="result-meta">${img.name} &middot; ${img.size_kb} KB</div>
            </div>
        `).join('');
    } catch (e) {
        console.error('Output-Bilder laden fehlgeschlagen:', e);
    }
}


// --- Lightbox ---

function showLightbox(src) {
    document.getElementById('lightboxImg').src = src;
    document.getElementById('lightbox').style.display = 'flex';
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        document.getElementById('lightbox').style.display = 'none';
    }
});


// --- Auto-resize textareas ---

function autoResize(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.max(80, ta.scrollHeight) + 'px';
}

document.querySelectorAll('textarea').forEach(ta => {
    ta.addEventListener('input', () => autoResize(ta));
});


// --- Drag & Drop for script files ---

const editorPanel = document.getElementById('editorPanel');
editorPanel.addEventListener('dragover', e => {
    e.preventDefault();
    editorPanel.classList.add('drop-highlight');
});
editorPanel.addEventListener('dragleave', () => {
    editorPanel.classList.remove('drop-highlight');
});
editorPanel.addEventListener('drop', e => {
    e.preventDefault();
    editorPanel.classList.remove('drop-highlight');
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.py')) {
        const input = document.getElementById('scriptFile');
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        loadScript();
    }
});

// Drag & Drop for ref images on right panel
const refsContainer = document.getElementById('refsContainer');
refsContainer.addEventListener('dragover', e => {
    e.preventDefault();
    refsContainer.classList.add('drop-highlight');
});
refsContainer.addEventListener('dragleave', () => {
    refsContainer.classList.remove('drop-highlight');
});
refsContainer.addEventListener('drop', e => {
    e.preventDefault();
    refsContainer.classList.remove('drop-highlight');
    const files = [...e.dataTransfer.files].filter(f => f.type.startsWith('image/'));
    if (files.length) uploadRefs(files);
});


// --- Toast ---

function toast(msg, type = 'info') {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}


// --- Registry Loading (from look-vergleich.html) ---

async function loadFromRegistry(index) {
    try {
        const resp = await fetch(`/api/registry/${index}`);
        const data = await resp.json();

        if (data.error) {
            toast(data.error, 'error');
            return;
        }

        // Use server-side split blocks if available, else fallback to monolithic
        if (data.blocks) {
            for (const field of FIELDS) {
                if (field === 'ref_instruction') continue;
                const ta = document.querySelector(`textarea[data-field="${field}"]`);
                if (ta) {
                    ta.value = data.blocks[field] || '';
                    autoResize(ta);
                }
            }
        } else {
            const sceneTA = document.querySelector('textarea[data-field="scene_block"]');
            if (sceneTA) {
                sceneTA.value = data.prompt;
                autoResize(sceneTA);
            }
            for (const field of ['style_header', 'child_char_block', 'brush_guide', 'medium_block', 'negative_block']) {
                const ta = document.querySelector(`textarea[data-field="${field}"]`);
                if (ta) { ta.value = ''; autoResize(ta); }
            }
        }

        // Set output name
        if (data.output_name) {
            document.getElementById('outputName').value = data.output_name;
        }

        // Show original image in results
        const resultsContainer = document.getElementById('resultsContainer');
        if (data.original_image) {
            const badge = data.bewertung ? `<span style="background:var(--accent);color:white;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600">${data.bewertung}</span>` : '';
            resultsContainer.innerHTML = `
                <div style="padding:8px">
                    <div style="font-size:12px;color:var(--yellow);margin-bottom:6px;font-weight:600">
                        Original: ${data.titel} ${badge}
                    </div>
                    ${data.notiz ? `<div style="font-size:11px;color:var(--text-dim);margin-bottom:6px;font-style:italic">${data.notiz}</div>` : ''}
                </div>
                <div class="result-image">
                    <img src="${data.original_image.data_url}" alt="${data.titel}" onclick="showLightbox(this.src)">
                    <div class="result-meta">
                        ${data.original_image.name} &middot; ${data.original_image.size_kb} KB &middot; Original
                    </div>
                </div>
            `;
        }

        // Show ref images — explicit from registry or suggested
        activeRefs = [];

        if (data.ref_images && data.ref_images.length > 0) {
            activeRefs = data.ref_images;
            renderActiveRefs(activeRefs, 'Registry-Refs');
        } else if (data.suggested_refs && data.suggested_refs.length > 0) {
            activeRefs = data.suggested_refs;
            renderActiveRefs(activeRefs, 'Vorgeschlagen');
        }

        // Show suggestions panel for adding more
        if (data.all_suggestions && data.all_suggestions.length > 0) {
            renderRefSuggestions(data.all_suggestions);
        }

        // Update ref dir display
        if (data.ref_dir) {
            document.getElementById('refDirInput').value = data.ref_dir || 'Intelligente Auswahl';
        } else {
            document.getElementById('refDirInput').value = 'Intelligente Auswahl (aus Registry)';
        }

        // Update output dir display
        if (data.output_dir) {
            document.getElementById('outputDirInput').value = data.output_dir;
        }

        // Tell backend about our ref paths
        syncRefPaths();

        // Set source info in header
        document.title = `Dino Bildgen — ${data.titel}`;

        showBlocks();
        updatePreview();
        autoSave();
        toast(`Registry geladen: ${data.titel} (${data.sektion})`, 'success');
    } catch (e) {
        toast('Registry-Fehler: ' + e.message, 'error');
    }
}

function checkHashRoute() {
    const hash = window.location.hash;
    if (hash.startsWith('#registry:')) {
        const index = parseInt(hash.split(':')[1]);
        if (!isNaN(index)) {
            loadFromRegistry(index);
        }
    }
}

window.addEventListener('hashchange', checkHashRoute);


// --- Auto-Save (localStorage) ---

function autoSave() {
    const data = {
        blocks: getBlocks(),
        outputName: document.getElementById('outputName').value,
        temperature: document.getElementById('temperature').value,
        variants: document.getElementById('variants').value,
        refDir: document.getElementById('refDirInput').value,
        outputDir: document.getElementById('outputDirInput').value,
        activeRefs: activeRefs,
        timestamp: Date.now(),
    };
    localStorage.setItem('dino-bildgen-autosave', JSON.stringify(data));
}

function autoRestore() {
    const raw = localStorage.getItem('dino-bildgen-autosave');
    if (!raw) return false;
    try {
        const data = JSON.parse(raw);
        // Only restore if less than 4 hours old
        if (Date.now() - data.timestamp > 4 * 60 * 60 * 1000) return false;

        for (const field of FIELDS) {
            const ta = document.querySelector(`textarea[data-field="${field}"]`);
            if (ta && data.blocks[field]) {
                ta.value = data.blocks[field];
                autoResize(ta);
            }
        }
        if (data.outputName) document.getElementById('outputName').value = data.outputName;
        if (data.temperature) {
            document.getElementById('temperature').value = data.temperature;
            document.getElementById('tempValue').textContent = data.temperature;
        }
        if (data.variants) document.getElementById('variants').value = data.variants;
        if (data.refDir) document.getElementById('refDirInput').value = data.refDir;
        if (data.outputDir) document.getElementById('outputDirInput').value = data.outputDir;
        if (data.activeRefs && data.activeRefs.length > 0) {
            activeRefs = data.activeRefs;
            renderActiveRefs();
            syncRefPaths();
        }

        showBlocks();
        updatePreview();
        const age = Math.round((Date.now() - data.timestamp) / 60000);
        toast(`Auto-Save wiederhergestellt (${age} Min alt)`, 'success');
        return true;
    } catch (e) {
        return false;
    }
}

// Save on every edit
document.addEventListener('input', () => autoSave());


// --- Init ---

loadRefs();
updatePreview();

// Registry hash has priority, otherwise restore auto-save
const hash = window.location.hash;
if (hash.startsWith('#registry:')) {
    checkHashRoute();
} else {
    autoRestore();
}
