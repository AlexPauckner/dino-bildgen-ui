// Dino-Bildgen-UI V3 — Frontend Logic
// 5-Block-Schema + 3 Ref-Kategorien

const FIELDS = ['style', 'ref_description', 'scene', 'character', 'composition', 'negative'];

// Legacy field mapping (V1/V2 → V3)
const LEGACY_MAP = {
    style_header: 'style',
    logline: 'scene',
    child_char_block: 'character',
    scene_block: 'scene',
    brush_guide: 'style',
    medium_block: 'style',
    negative_block: 'negative',
};

// --- Upscayl availability ---
let upscaylAvailable = false;

// --- Ref State (3 categories) ---
let refs = {
    style: [],      // [{name, path, data_url, size_kb, reason?, score?, registry_index?}]
    character: [],
    scribble: [],
};


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

        for (const field of FIELDS) {
            const ta = document.querySelector('textarea[data-field="' + field + '"]');
            if (ta && data[field]) {
                ta.value = data[field];
                autoResize(ta);
            }
        }

        if (data.temperature !== undefined) {
            document.getElementById('temperature').value = data.temperature;
            document.getElementById('tempValue').textContent = data.temperature;
        }
        if (data.variants) {
            document.getElementById('variants').value = data.variants;
        }

        const name = input.files[0].name.replace('.py', '').replace('generate_', '');
        document.getElementById('outputName').value = name;

        showBlocks();
        updatePreview();
        autoSave();
        toast('Script geladen: ' + input.files[0].name, 'success');
    } catch (e) {
        toast('Fehler beim Parsen: ' + e.message, 'error');
    }
}


function resetBlocks() {
    for (const field of FIELDS) {
        const ta = document.querySelector('textarea[data-field="' + field + '"]');
        if (ta) ta.value = '';
    }
    refs = { style: [], character: [], scribble: [] };
    renderAllRefs();
    showBlocks();
    updatePreview();
    autoSave();
}

function showBlocks() {
    document.getElementById('emptyState').style.display = 'none';
    var bc = document.getElementById('blocksContainer');
    bc.style.display = 'flex';
    bc.style.flexDirection = 'column';
    bc.style.gap = '12px';
    document.querySelectorAll('textarea').forEach(autoResize);
}


// --- Block Toggle ---

function toggleBlock(headerEl) {
    var body = headerEl.nextElementSibling;
    body.style.display = body.style.display === 'none' ? '' : 'none';
}


// --- Prompt Building ---

function getBlocks() {
    var blocks = {};
    for (var i = 0; i < FIELDS.length; i++) {
        var field = FIELDS[i];
        var ta = document.querySelector('textarea[data-field="' + field + '"]');
        blocks[field] = ta ? ta.value.trim() : '';
    }
    return blocks;
}

function buildPrompt(blocks) {
    var parts = [];
    for (var i = 0; i < FIELDS.length; i++) {
        // ref_description goes separately to the API, not into the prompt text
        if (FIELDS[i] === 'ref_description') continue;
        if (blocks[FIELDS[i]]) parts.push(blocks[FIELDS[i]]);
    }
    return parts.join('\n\n');
}

function updatePreview() {
    var blocks = getBlocks();
    var prompt = buildPrompt(blocks);

    var refDescEl = document.querySelector('textarea[data-field="ref_description"]');
    var refDesc = refDescEl ? refDescEl.value.trim() : '';
    var fullPreview = refDesc ? '[REF DESCRIPTION]\n' + refDesc + '\n\n' + prompt : prompt;
    document.getElementById('promptPreview').textContent = fullPreview;
    document.getElementById('totalChars').textContent = fullPreview.length.toLocaleString();

    for (var i = 0; i < FIELDS.length; i++) {
        var field = FIELDS[i];
        var card = document.querySelector('[data-block="' + field + '"]');
        if (!card) continue;
        var counter = card.querySelector('[data-counter]');
        var ta = card.querySelector('textarea');
        if (counter && ta) {
            counter.textContent = ta.value.length > 0 ? ta.value.length + ' Z' : '';
        }
    }
    autoSave();
}


// --- Prompt Preview Toggle ---

function togglePreview() {
    var el = document.getElementById('promptPreview');
    var arrow = document.getElementById('previewArrow');
    el.classList.toggle('open');
    arrow.textContent = el.classList.contains('open') ? '\u25B2' : '\u25BC';
}


// --- Ref Category Management ---

function addRef(role, refObj) {
    if (!refs[role]) refs[role] = [];
    // Dedupe by path
    if (refObj.path && refs[role].some(function(r) { return r.path === refObj.path; })) {
        toast('Bereits als Ref aktiv', 'info');
        return false;
    }
    refs[role].push(refObj);
    renderRefCategory(role);
    updateRefTotal();
    autoSave();
    return true;
}

function removeRef(role, index) {
    refs[role].splice(index, 1);
    renderRefCategory(role);
    updateRefTotal();
    autoSave();
    toast('Ref entfernt (' + refs[role].length + ' ' + role + ')', 'info');
}

function renderRefCategory(role) {
    var grid = document.getElementById(role + 'RefGrid');
    var countEl = document.getElementById(role + 'RefCount');
    var items = refs[role] || [];

    countEl.textContent = items.length;

    if (items.length === 0) {
        grid.innerHTML = '<div class="ref-empty">Drop oder + klicken</div>';
        return;
    }

    grid.innerHTML = items.map(function(img, i) {
        var reason = img.reason ? '<span class="ref-reason">' + img.reason + '</span>' : '';
        var src = img.data_url || (img.path ? '/api/image?path=' + encodeURIComponent(img.path) : '/api/registry/image/' + img.registry_index);
        return '<div class="ref-thumb active-ref" title="' + img.name + (img.reason ? ' \u2014 ' + img.reason : '') + '">' +
            '<img src="' + src + '" alt="' + img.name + '" onclick="showLightbox(this.src)">' +
            '<button class="ref-delete" onclick="removeRef(\'' + role + '\', ' + i + ')">&times;</button>' +
            '<span class="ref-name">' + img.name + '</span>' +
            reason +
            '</div>';
    }).join('');
}

function renderAllRefs() {
    renderRefCategory('style');
    renderRefCategory('character');
    renderRefCategory('scribble');
    updateRefTotal();
}

function updateRefTotal() {
    var total = refs.style.length + refs.character.length + refs.scribble.length;
    document.getElementById('refTotal').textContent = total;
}

function getAllRefPaths() {
    return {
        style: refs.style.map(function(r) { return r.path; }).filter(Boolean),
        character: refs.character.map(function(r) { return r.path; }).filter(Boolean),
        scribble: refs.scribble.map(function(r) { return r.path; }).filter(Boolean),
    };
}


// --- Ref Upload per Category ---

function uploadRefsToCategory(role, files) {
    for (var i = 0; i < files.length; i++) {
        (function(file) {
            var reader = new FileReader();
            reader.onload = function() {
                var dataUrl = reader.result;
                var sizeKb = Math.round(file.size / 1024);
                // Upload to server first, then add ref with server path
                var formData = new FormData();
                formData.append('file', file);
                fetch('/api/refs/upload', { method: 'POST', body: formData })
                    .then(function(resp) { return resp.json(); })
                    .then(function(data) {
                        addRef(role, {
                            name: file.name,
                            path: data.path || '',
                            data_url: dataUrl,
                            size_kb: sizeKb,
                        });
                    })
                    .catch(function() {
                        // Fallback: add without server path
                        addRef(role, {
                            name: file.name,
                            path: '',
                            data_url: dataUrl,
                            size_kb: sizeKb,
                        });
                    });
            };
            reader.readAsDataURL(file);
        })(files[i]);
    }
    toast(files.length + ' Bild(er) hinzugef\u00fcgt zu ' + role, 'success');
}


// --- Ref Drag & Drop per Category ---

function refCatDragOver(e) {
    e.preventDefault();
    e.currentTarget.classList.add('drop-highlight');
}

function refCatDragLeave(e) {
    e.currentTarget.classList.remove('drop-highlight');
}

function refCatDrop(e, role) {
    e.preventDefault();
    e.currentTarget.classList.remove('drop-highlight');
    var files = [];
    for (var i = 0; i < e.dataTransfer.files.length; i++) {
        if (e.dataTransfer.files[i].type.startsWith('image/')) {
            files.push(e.dataTransfer.files[i]);
        }
    }
    if (files.length) uploadRefsToCategory(role, files);
}


// --- Ref Suggestions ---

function addRefFromSuggestion(registryIndex, suggestedRole) {
    var sugEl = document.querySelector('[data-sug-index="' + registryIndex + '"]');
    if (!sugEl) return;

    var path = sugEl.dataset.path;
    if (!path) return;

    var role = suggestedRole || sugEl.dataset.suggestedRole || 'style';
    var titel = sugEl.querySelector('.suggestion-title');
    titel = titel ? titel.textContent : path.split('/').pop();
    var reasonEl = sugEl.querySelector('.suggestion-reason');
    var reason = reasonEl ? reasonEl.textContent : '';

    var added = addRef(role, {
        name: path.split('/').pop(),
        path: path,
        data_url: '/api/registry/image/' + registryIndex,
        registry_index: registryIndex,
        reason: reason,
    });

    if (added) {
        sugEl.classList.add('ref-added');
        toast('Ref \u2192 ' + role + ': ' + titel, 'success');
    }
}

function renderRefSuggestions(suggestions) {
    var container = document.getElementById('suggestionsContainer');
    var section = document.getElementById('suggestionsSection');
    if (!container || !section) return;

    container.innerHTML = suggestions.map(function(sug) {
        var fileName = sug.datei.split('/').pop();
        var roleLabel = sug.suggested_role === 'character' ? 'Char' : 'Style';
        var roleClass = sug.suggested_role === 'character' ? 'role-character' : 'role-style';
        return '<div class="suggestion-item" ' +
            'data-sug-index="' + sug.index + '" ' +
            'data-path="/Users/alexanderpauckner/Kinderbuch/Comic_Projekt_2025/Dino-Buch/' + sug.datei + '" ' +
            'data-suggested-role="' + (sug.suggested_role || 'style') + '" ' +
            'onclick="addRefFromSuggestion(' + sug.index + ', \'' + (sug.suggested_role || 'style') + '\')" ' +
            'title="Score: ' + sug.score + ' \u2014 ' + sug.reason + '">' +
            '<img src="/api/registry/image/' + sug.index + '" loading="lazy" alt="' + sug.titel + '">' +
            '<div class="suggestion-info">' +
            '<span class="suggestion-title">' + sug.titel + '</span>' +
            '<span class="suggestion-reason">' + sug.reason +
            (sug.bewertung ? ' \u2b50' + sug.bewertung : '') + '</span>' +
            '</div>' +
            '<span class="suggestion-role ' + roleClass + '">' + roleLabel + '</span>' +
            '</div>';
    }).join('');

    container.style.display = 'block';
    section.style.display = 'block';
}


// --- Output Dir ---

async function changeOutputDir(dir) {
    var formData = new FormData();
    formData.append('dir', dir);
    await fetch('/api/output/dir', { method: 'POST', body: formData });
    toast('Output-Ordner: ' + dir, 'info');
}

async function openOutputDir() {
    await fetch('/api/output/open', { method: 'POST' });
}


// --- Generate ---

async function generate() {
    var blocks = getBlocks();
    var prompt = buildPrompt(blocks);

    if (!prompt) {
        toast('Kein Prompt vorhanden', 'error');
        return;
    }

    var btn = document.getElementById('generateBtn');
    var originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Generiere...';

    var resultsContainer = document.getElementById('resultsContainer');
    resultsContainer.innerHTML = '<div style="padding:20px;text-align:center"><span class="spinner"></span><p style="margin-top:10px;color:var(--text-dim);font-size:12px">Warte auf Gemini API...</p></div>';

    var refPaths = getAllRefPaths();
    var contextChecked = document.getElementById('contextPrefix').checked;

    try {
        var resp = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                prompt: prompt,
                refs_style: refPaths.style,
                refs_character: refPaths.character,
                refs_scribble: refPaths.scribble,
                ref_description: blocks.ref_description || '',
                context_prefix: contextChecked,
                temperature: parseFloat(document.getElementById('temperature').value),
                variants: parseInt(document.getElementById('variants').value),
                output_name: document.getElementById('outputName').value,
                aspect_ratio: document.getElementById('aspectRatio').value,
                image_size: document.getElementById('imageSize').value,
                thinking_level: document.getElementById('thinkingLevel').value,
                model: document.getElementById('modelSelect').value,
            }),
        });

        var data = await resp.json();

        if (data.error) {
            toast(data.error, 'error');
            resultsContainer.innerHTML = '';
            return;
        }

        resultsContainer.innerHTML = '';
        for (var ri = 0; ri < data.results.length; ri++) {
            var result = data.results[ri];
            if (result.error) {
                resultsContainer.innerHTML += '<div style="padding:10px;color:var(--accent);font-size:12px">Variante ' + result.variant + ': ' + result.error + '</div>';
                continue;
            }
            for (var pi = 0; pi < result.parts.length; pi++) {
                var part = result.parts[pi];
                if (part.type === 'image') {
                    var upscaleBtn = upscaylAvailable
                        ? '<button class="btn btn-secondary btn-small upscale-btn" onclick="upscaleImage(this, \'' + part.saved_to.replace(/'/g, "\\'") + '\')">Upscale 2K</button>'
                        : '';
                    var fullResUrl = '/api/image?path=' + encodeURIComponent(part.saved_to);
                    resultsContainer.innerHTML += '<div class="result-image">' +
                        '<img src="' + part.data_url + '" alt="' + part.filename + '" onclick="showLightbox(\'' + fullResUrl + '\')">' +
                        '<div class="result-meta"><span>' + part.filename + ' &middot; ' + part.size_kb + ' KB &middot; ' + result.elapsed + 's</span>' + upscaleBtn + '</div>' +
                        '</div>';
                } else if (part.type === 'text') {
                    resultsContainer.innerHTML += '<div style="padding:8px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border)">' + part.content + '</div>';
                }
            }
        }

        toast(data.results.length + ' Variante(n) generiert \u2014 ' + data.refs_used + ' Refs', 'success');
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
        var resp = await fetch('/api/output/images');
        var data = await resp.json();
        var container = document.getElementById('resultsContainer');

        if (data.images.length === 0) {
            container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px">Keine Bilder im Output-Ordner</div>';
            return;
        }

        container.innerHTML = data.images.map(function(img) {
            var upBtn = upscaylAvailable && !img.name.match(/_upscayl\d+k/)
                ? '<button class="btn btn-secondary btn-small upscale-btn" onclick="upscaleImage(this, \'' + img.path.replace(/'/g, "\\'") + '\')">Upscale 2K</button>'
                : '';
            var imgFullRes = '/api/image?path=' + encodeURIComponent(img.path);
            return '<div class="result-image' + (img.name.match(/_upscayl\d+k/) ? ' upscaled' : '') + '">' +
                '<img src="/api/output/image/' + encodeURIComponent(img.name) + '" alt="' + img.name + '" onclick="showLightbox(\'' + imgFullRes + '\')" loading="lazy">' +
                '<div class="result-meta"><span>' + (img.name.match(/_upscayl\d+k/) ? '<span class="upscale-badge">UPSCALED</span> ' : '') + img.name + ' &middot; ' + img.size_kb + ' KB</span>' + upBtn + '</div>' +
                '</div>';
        }).join('');
    } catch (e) {
        console.error('Output-Bilder laden fehlgeschlagen:', e);
    }
}


// --- Lightbox ---

function showLightbox(src) {
    document.getElementById('lightboxImg').src = src;
    document.getElementById('lightbox').style.display = 'flex';
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.getElementById('lightbox').style.display = 'none';
    }
});


// --- Auto-resize textareas ---

function autoResize(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.max(80, ta.scrollHeight) + 'px';
}

document.querySelectorAll('textarea').forEach(function(ta) {
    ta.addEventListener('input', function() { autoResize(ta); });
});


// --- Drag & Drop for script files ---

var editorPanel = document.getElementById('editorPanel');
editorPanel.addEventListener('dragover', function(e) {
    e.preventDefault();
    editorPanel.classList.add('drop-highlight');
});
editorPanel.addEventListener('dragleave', function() {
    editorPanel.classList.remove('drop-highlight');
});
editorPanel.addEventListener('drop', function(e) {
    e.preventDefault();
    editorPanel.classList.remove('drop-highlight');
    var file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.py')) {
        var input = document.getElementById('scriptFile');
        var dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        loadScript();
    }
});


// --- Toast ---

function toast(msg, type) {
    type = type || 'info';
    var el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function() { el.remove(); }, 3500);
}


// --- Registry Loading ---

async function loadFromRegistry(index) {
    try {
        var resp = await fetch('/api/registry/' + index);
        var data = await resp.json();

        if (data.error) {
            toast(data.error, 'error');
            return;
        }

        // Fill V3 blocks
        if (data.blocks) {
            for (var i = 0; i < FIELDS.length; i++) {
                var field = FIELDS[i];
                var ta = document.querySelector('textarea[data-field="' + field + '"]');
                if (ta) {
                    ta.value = data.blocks[field] || '';
                    autoResize(ta);
                }
            }
        }

        // Set output name + ref description
        if (data.output_name) {
            document.getElementById('outputName').value = data.output_name;
        }
        // ref_description from registry → into block field
        if (data.ref_description) {
            var rdTa = document.querySelector('textarea[data-field="ref_description"]');
            if (rdTa) { rdTa.value = data.ref_description; autoResize(rdTa); }
        }

        // Show original image
        var resultsContainer = document.getElementById('resultsContainer');
        if (data.original_image) {
            var badge = data.bewertung ? '<span style="background:var(--accent);color:white;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600">' + data.bewertung + '</span>' : '';
            resultsContainer.innerHTML =
                '<div style="padding:8px">' +
                '<div style="font-size:12px;color:var(--yellow);margin-bottom:6px;font-weight:600">' +
                'Original: ' + data.titel + ' ' + badge + '</div>' +
                (data.notiz ? '<div style="font-size:11px;color:var(--text-dim);margin-bottom:6px;font-style:italic">' + data.notiz + '</div>' : '') +
                '</div>' +
                '<div class="result-image">' +
                '<img src="' + data.original_image.data_url + '" alt="' + data.titel + '" onclick="showLightbox(this.src)">' +
                '<div class="result-meta">' + data.original_image.name + ' &middot; ' + data.original_image.size_kb + ' KB &middot; Original</div>' +
                '</div>';
        }

        // Load ref images per category (V3 format from backend)
        refs = { style: [], character: [], scribble: [] };

        if (data.ref_images) {
            var roles = ['style', 'character', 'scribble'];
            for (var ri = 0; ri < roles.length; ri++) {
                var role = roles[ri];
                var roleRefs = data.ref_images[role] || [];
                for (var rj = 0; rj < roleRefs.length; rj++) {
                    refs[role].push(roleRefs[rj]);
                }
            }
        }

        renderAllRefs();

        // Show suggestions panel
        if (data.all_suggestions && data.all_suggestions.length > 0) {
            renderRefSuggestions(data.all_suggestions);
        }

        // Update output dir
        if (data.output_dir) {
            document.getElementById('outputDirInput').value = data.output_dir;
        }

        document.title = 'Dino Bildgen \u2014 ' + data.titel;

        showBlocks();
        updatePreview();
        autoSave();
        toast('Registry geladen: ' + data.titel + ' (' + data.sektion + ')', 'success');
    } catch (e) {
        toast('Registry-Fehler: ' + e.message, 'error');
    }
}

function checkHashRoute() {
    var hash = window.location.hash;
    if (hash.indexOf('#registry:') === 0) {
        var index = parseInt(hash.split(':')[1]);
        if (!isNaN(index)) {
            loadFromRegistry(index);
        }
    }
}

window.addEventListener('hashchange', checkHashRoute);


// --- Auto-Save (localStorage) ---

function stripDataUrls(refList) {
    // Strip base64 data_urls to avoid localStorage quota overflow
    return refList.map(function(r) {
        var slim = { name: r.name, size_kb: r.size_kb };
        if (r.path) slim.path = r.path;
        if (r.registry_index !== undefined) slim.registry_index = r.registry_index;
        if (r.reason) slim.reason = r.reason;
        if (r.score !== undefined) slim.score = r.score;
        if (r.suggested_role) slim.suggested_role = r.suggested_role;
        return slim;
    });
}

function autoSave() {
    var data = {
        version: 3,
        blocks: getBlocks(),
        refs_style: stripDataUrls(refs.style),
        refs_character: stripDataUrls(refs.character),
        refs_scribble: stripDataUrls(refs.scribble),
        outputName: document.getElementById('outputName').value,
        temperature: document.getElementById('temperature').value,
        variants: document.getElementById('variants').value,
        outputDir: document.getElementById('outputDirInput').value,
        contextPrefix: document.getElementById('contextPrefix').checked,
        aspectRatio: document.getElementById('aspectRatio').value,
        imageSize: document.getElementById('imageSize').value,
        thinkingLevel: document.getElementById('thinkingLevel').value,
        modelSelect: document.getElementById('modelSelect').value,
        timestamp: Date.now(),
    };
    localStorage.setItem('dino-bildgen-autosave', JSON.stringify(data));
}

function migrateAutoSaveData(data) {
    // Migrate V1/V2 blocks to V3
    if (data.blocks && data.blocks.style_header !== undefined && data.blocks.style === undefined) {
        var old = data.blocks;
        data.blocks = {
            style: [old.style_header, old.brush_guide, old.medium_block].filter(Boolean).join('\n\n'),
            scene: [old.logline, old.scene_block].filter(Boolean).join('\n\n'),
            character: old.child_char_block || '',
            composition: '',
            negative: old.negative_block || '',
        };
    }
    // Migrate refs
    if (data.activeRefs && !data.refs_style) {
        data.refs_style = data.activeRefs;
        data.refs_character = [];
        data.refs_scribble = [];
    }
    return data;
}

function autoRestore() {
    var raw = localStorage.getItem('dino-bildgen-autosave');
    if (!raw) return false;
    try {
        var data = JSON.parse(raw);
        if (Date.now() - data.timestamp > 4 * 60 * 60 * 1000) return false;

        data = migrateAutoSaveData(data);

        if (data.blocks) {
            for (var i = 0; i < FIELDS.length; i++) {
                var field = FIELDS[i];
                var ta = document.querySelector('textarea[data-field="' + field + '"]');
                if (ta && data.blocks[field]) {
                    ta.value = data.blocks[field];
                    autoResize(ta);
                }
            }
        }

        if (data.outputName) document.getElementById('outputName').value = data.outputName;
        if (data.temperature) {
            document.getElementById('temperature').value = data.temperature;
            document.getElementById('tempValue').textContent = data.temperature;
        }
        if (data.variants) document.getElementById('variants').value = data.variants;
        if (data.outputDir) document.getElementById('outputDirInput').value = data.outputDir;
        if (data.contextPrefix !== undefined) {
            document.getElementById('contextPrefix').checked = data.contextPrefix;
        }
        if (data.aspectRatio) document.getElementById('aspectRatio').value = data.aspectRatio;
        if (data.imageSize) document.getElementById('imageSize').value = data.imageSize;
        if (data.thinkingLevel) document.getElementById('thinkingLevel').value = data.thinkingLevel;
        if (data.modelSelect) document.getElementById('modelSelect').value = data.modelSelect;

        // Restore refs
        if (data.refs_style) refs.style = data.refs_style;
        if (data.refs_character) refs.character = data.refs_character;
        if (data.refs_scribble) refs.scribble = data.refs_scribble;
        renderAllRefs();

        showBlocks();
        updatePreview();
        var age = Math.round((Date.now() - data.timestamp) / 60000);
        toast('Auto-Save wiederhergestellt (' + age + ' Min alt)', 'success');
        return true;
    } catch (e) {
        return false;
    }
}

// Save on every edit (input for text/range, change for selects/checkboxes)
document.addEventListener('input', function() { autoSave(); });
document.addEventListener('change', function() { autoSave(); });


// --- Upscayl ---

async function checkUpscayl() {
    try {
        var resp = await fetch('/api/upscayl/status');
        var data = await resp.json();
        upscaylAvailable = data.available;
    } catch (e) {
        upscaylAvailable = false;
    }
}

async function upscaleImage(btn, path) {
    var originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Upscaling...';

    try {
        var resp = await fetch('/api/upscale', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: path,
                model: 'high-fidelity-4x',
                scale: 2,
            }),
        });
        var data = await resp.json();

        if (data.error) {
            toast('Upscale Fehler: ' + data.error, 'error');
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }

        // Insert upscaled image after current result
        var resultDiv = btn.closest('.result-image');
        var upscaledDiv = document.createElement('div');
        upscaledDiv.className = 'result-image upscaled';
        var upscaleFullRes = '/api/image?path=' + encodeURIComponent(data.saved_to);
        upscaledDiv.innerHTML =
            '<img src="' + data.data_url + '" alt="' + data.filename + '" onclick="showLightbox(\'' + upscaleFullRes + '\')">' +
            '<div class="result-meta"><span class="upscale-badge">UPSCALED</span> ' + data.filename + ' &middot; ' + data.size_kb + ' KB &middot; ' + data.elapsed + 's (' + data.model + ')</div>';
        resultDiv.parentNode.insertBefore(upscaledDiv, resultDiv.nextSibling);

        btn.textContent = 'Done';
        btn.disabled = true;
        toast('Upscaled: ' + data.filename, 'success');
    } catch (e) {
        toast('Upscale Fehler: ' + e.message, 'error');
        btn.disabled = false;
        btn.textContent = originalText;
    }
}


// --- Init ---

checkUpscayl();
updatePreview();
renderAllRefs();

var hash = window.location.hash;
if (hash.indexOf('#registry:') === 0) {
    checkHashRoute();
} else {
    autoRestore();
}
