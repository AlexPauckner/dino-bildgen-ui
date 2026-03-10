#!/usr/bin/env python3
"""Dino-Bildgen-UI — Lokale Web-App für Gemini-Bildgenerierung."""

import base64
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- Config ---
API_KEY_PATH = Path(os.path.expanduser("~/.google_api_key"))
DEFAULT_REF_DIR = Path("/tmp/dino-neue-refs")
DEFAULT_OUTPUT_DIR = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch/NB2-Neue-Arten"
))

# Runtime state
state = {
    "ref_dir": str(DEFAULT_REF_DIR),
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "ref_paths": [],  # Explicit ref file paths (overrides ref_dir when set)
}


# --- Script Parser ---

# Known block names in the scripts
KNOWN_BLOCKS = [
    "STYLE_HEADER",
    "CHILD_CHAR_BLOCK",
    "BABY_CHAR_BLOCK",
    "BRUSH_GUIDE",
    "MEDIUM_BLOCK",
    "NEGATIVE_BLOCK",
]

def parse_script(source: str) -> dict:
    """Parse a Python generation script into named blocks."""
    blocks = {}

    # Extract triple-quoted variable assignments: VAR = """...""" or VAR = '''...'''
    pattern = r'(\w+)\s*=\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')'
    for match in re.finditer(pattern, source, re.DOTALL):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        blocks[name] = value.strip()

    # Extract the PROMPT f-string — this is more complex
    # Look for PROMPT = f"""..."""
    prompt_match = re.search(
        r'PROMPT\s*=\s*f"""(.*?)"""', source, re.DOTALL
    )
    if not prompt_match:
        prompt_match = re.search(
            r"PROMPT\s*=\s*f'''(.*?)'''", source, re.DOTALL
        )

    scene_block = ""
    if prompt_match:
        prompt_template = prompt_match.group(1).strip()
        # Extract the "scene" part — everything between the known block references
        # Remove {STYLE_HEADER}, {CHILD_CHAR_BLOCK}, etc. and grab what's left
        scene = prompt_template
        for block_name in KNOWN_BLOCKS:
            scene = scene.replace(f"{{{block_name}}}", "<<<BLOCK_MARKER>>>")

        # The scene block is everything between the first and last markers
        parts = scene.split("<<<BLOCK_MARKER>>>")
        # Filter out empty parts, the meaningful content is usually in the middle
        meaningful = [p.strip() for p in parts if p.strip()]
        scene_block = "\n\n".join(meaningful)

    # Extract ref_instruction if present
    # Handle multi-line concatenated strings: ref_instruction = (\n"..."\n"..."\n)
    ref_match = re.search(
        r'ref_instruction\s*=\s*\((.*?)\)',
        source, re.DOTALL
    )
    if ref_match:
        raw = ref_match.group(1)
        # Extract all quoted strings and concatenate them
        parts = re.findall(r'"(.*?)"', raw, re.DOTALL)
        blocks["REF_INSTRUCTION"] = "".join(parts)
    else:
        ref_match = re.search(
            r'ref_instruction\s*=\s*(?:"""(.*?)"""|"(.*?)")',
            source, re.DOTALL
        )
        if ref_match:
            blocks["REF_INSTRUCTION"] = (ref_match.group(1) or ref_match.group(2) or "").strip()

    # Extract output dir
    out_match = re.search(r'OUTPUT_DIR\s*=\s*Path\([^"]*"([^"]+)"', source)
    if out_match:
        blocks["_OUTPUT_DIR"] = os.path.expanduser(out_match.group(1))

    # Extract ref dir
    ref_match2 = re.search(r'REF_DIR\s*=\s*Path\([^"]*"([^"]+)"', source)
    if ref_match2:
        blocks["_REF_DIR"] = os.path.expanduser(ref_match2.group(1))

    # Extract temperature
    temp_match = re.search(r'temperature\s*=\s*([\d.]+)', source)
    if temp_match:
        blocks["_TEMPERATURE"] = float(temp_match.group(1))

    # Extract number of variants (range(N))
    range_match = re.search(r'range\((\d+)\)', source)
    if range_match:
        blocks["_VARIANTS"] = int(range_match.group(1))

    result = {
        "style_header": blocks.get("STYLE_HEADER", ""),
        "child_char_block": blocks.get("CHILD_CHAR_BLOCK", blocks.get("BABY_CHAR_BLOCK", "")),
        "scene_block": scene_block,
        "brush_guide": blocks.get("BRUSH_GUIDE", ""),
        "medium_block": blocks.get("MEDIUM_BLOCK", ""),
        "negative_block": blocks.get("NEGATIVE_BLOCK", ""),
        "ref_instruction": blocks.get("REF_INSTRUCTION",
            "Use these images as STYLE REFERENCE for the oil painting texture and "
            "impasto brush strokes. Match the THICK paint texture from the style references. "
            "MAXIMIZE the visible brush strokes and paint texture — make it look like a real oil painting. "
            "Generate the following CHARACTER SHEET:\n\n"
        ),
        "temperature": blocks.get("_TEMPERATURE", 1.0),
        "variants": blocks.get("_VARIANTS", 1),
        "output_dir": blocks.get("_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)),
        "ref_dir": blocks.get("_REF_DIR", str(DEFAULT_REF_DIR)),
    }
    return result


def split_prompt_into_blocks(prompt: str) -> dict:
    """Intelligently split a monolithic prompt into named blocks.

    Recognizes known patterns by scanning line-by-line:
    - Style header: "Oil painted..." opening section
    - Child char block: "Child proportions..." section
    - Brush guide: "Brush stroke guide..." section (multi-line with - bullets)
    - Medium block: "Medium: oil..." line
    - Negative block: "No photorealism..." line
    - Everything else: scene_block
    """
    blocks = {
        "style_header": "",
        "child_char_block": "",
        "scene_block": "",
        "brush_guide": "",
        "medium_block": "",
        "negative_block": "",
    }

    lines = prompt.strip().split('\n')
    current_block = None  # which block we're appending to
    block_lines = {k: [] for k in blocks}

    for line in lines:
        stripped = line.strip()

        # Detect block starts
        if re.match(r'^Oil paint', stripped, re.IGNORECASE) and not block_lines["style_header"]:
            current_block = "style_header"
            block_lines[current_block].append(line)
        elif re.match(r'^Child proportions', stripped, re.IGNORECASE) and not block_lines["child_char_block"]:
            current_block = "child_char_block"
            block_lines[current_block].append(line)
        elif re.match(r'^Brush stroke guide', stripped, re.IGNORECASE):
            current_block = "brush_guide"
            block_lines[current_block].append(line)
        elif re.match(r'^Medium:', stripped, re.IGNORECASE):
            current_block = "medium_block"
            block_lines[current_block].append(line)
        elif re.match(r'^No photorealism', stripped, re.IGNORECASE):
            current_block = "negative_block"
            block_lines[current_block].append(line)
        elif stripped == '' and current_block in ("style_header", "child_char_block", "medium_block", "negative_block"):
            # Empty line after a single-paragraph block → back to scene
            current_block = "scene_block"
            block_lines[current_block].append(line)
        elif stripped == '' and current_block == "brush_guide":
            # Empty line after brush guide → check if next lines are still bullet points
            # For now, end the brush guide block
            current_block = "scene_block"
            block_lines[current_block].append(line)
        elif current_block is not None:
            block_lines[current_block].append(line)
        else:
            block_lines["scene_block"].append(line)

    # Join lines back into strings, strip leading/trailing whitespace
    for key in blocks:
        blocks[key] = '\n'.join(block_lines[key]).strip()

    return blocks


def build_prompt(blocks: dict) -> str:
    """Assemble the full prompt from blocks."""
    parts = []
    if blocks.get("style_header"):
        parts.append(blocks["style_header"])
    if blocks.get("scene_block"):
        # Scene block contains CHARACTER SHEET header + char block placeholder
        scene = blocks["scene_block"]
        # Insert child_char_block after first paragraph if it's not already there
        if blocks.get("child_char_block") and blocks["child_char_block"] not in scene:
            # Find a good insertion point — after "CHARACTER SHEET" line
            lines = scene.split("\n")
            inserted = False
            new_lines = []
            for line in lines:
                new_lines.append(line)
                if "CHARACTER SHEET" in line and not inserted:
                    new_lines.append("")
                    new_lines.append(blocks["child_char_block"])
                    inserted = True
            if not inserted:
                new_lines.insert(1, "")
                new_lines.insert(2, blocks["child_char_block"])
            scene = "\n".join(new_lines)
        parts.append(scene)
    else:
        if blocks.get("child_char_block"):
            parts.append(blocks["child_char_block"])
    if blocks.get("brush_guide"):
        parts.append(blocks["brush_guide"])
    if blocks.get("medium_block"):
        parts.append(blocks["medium_block"])
    if blocks.get("negative_block"):
        parts.append(blocks["negative_block"])
    return "\n\n".join(parts)


# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.post("/api/parse-script")
async def api_parse_script(file: UploadFile = File(...)):
    """Parse an uploaded Python script into blocks."""
    content = await file.read()
    source = content.decode("utf-8", errors="replace")
    result = parse_script(source)
    # Update state dirs
    state["ref_dir"] = result["ref_dir"]
    state["output_dir"] = result["output_dir"]
    return JSONResponse(result)


@app.get("/api/refs")
async def api_list_refs():
    """List reference images in current ref directory."""
    ref_dir = Path(state["ref_dir"])
    if not ref_dir.exists():
        return JSONResponse({"images": [], "dir": str(ref_dir)})

    images = []
    for f in sorted(ref_dir.glob("*")):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            data = base64.b64encode(f.read_bytes()).decode()
            mime = "image/png" if f.suffix == ".png" else "image/jpeg"
            images.append({
                "name": f.name,
                "size_kb": f.stat().st_size // 1024,
                "data_url": f"data:{mime};base64,{data}",
            })
    return JSONResponse({"images": images, "dir": str(ref_dir)})


@app.post("/api/refs/upload")
async def api_upload_ref(file: UploadFile = File(...)):
    """Upload a new reference image."""
    ref_dir = Path(state["ref_dir"])
    ref_dir.mkdir(parents=True, exist_ok=True)
    dest = ref_dir / file.filename
    content = await file.read()
    dest.write_bytes(content)
    return JSONResponse({"ok": True, "name": file.filename})


@app.delete("/api/refs/{filename}")
async def api_delete_ref(filename: str):
    """Remove a reference image."""
    ref_path = Path(state["ref_dir"]) / filename
    if ref_path.exists():
        ref_path.unlink()
    return JSONResponse({"ok": True})


@app.post("/api/refs/dir")
async def api_set_ref_dir(dir: str = Form(...)):
    """Change the reference images directory."""
    expanded = os.path.expanduser(dir)
    state["ref_dir"] = expanded
    state["ref_paths"] = []  # Clear explicit paths when switching to dir mode
    return JSONResponse({"ok": True, "dir": expanded})


@app.post("/api/refs/set-paths")
async def api_set_ref_paths(request: Request):
    """Set explicit reference image paths (from registry suggestions)."""
    body = await request.json()
    paths = body.get("paths", [])
    state["ref_paths"] = [p for p in paths if Path(p).exists()]
    return JSONResponse({"ok": True, "count": len(state["ref_paths"])})


@app.post("/api/output/dir")
async def api_set_output_dir(dir: str = Form(...)):
    """Change the output directory."""
    expanded = os.path.expanduser(dir)
    state["output_dir"] = expanded
    return JSONResponse({"ok": True, "dir": expanded})


@app.post("/api/generate")
async def api_generate(request: Request):
    """Generate image via Gemini API."""
    body = await request.json()

    prompt_text = body.get("prompt", "")
    ref_instruction = body.get("ref_instruction", "")
    temperature = float(body.get("temperature", 1.0))
    variants = int(body.get("variants", 1))
    output_name = body.get("output_name", "generated")

    if not prompt_text:
        return JSONResponse({"error": "Kein Prompt angegeben"}, status_code=400)

    # Load API key
    if not API_KEY_PATH.exists():
        return JSONResponse({"error": f"API Key nicht gefunden: {API_KEY_PATH}"}, status_code=500)
    api_key = API_KEY_PATH.read_text().strip()

    # Load reference images
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # Load reference images — prefer explicit paths, fallback to directory
    ref_images = []
    ref_file_paths = body.get("ref_paths", state.get("ref_paths", []))
    if ref_file_paths:
        for rp in ref_file_paths:
            rp = Path(rp)
            if rp.exists() and rp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                data = rp.read_bytes()
                mime = "image/png" if rp.suffix.lower() == ".png" else "image/jpeg"
                ref_images.append(types.Part.from_bytes(data=data, mime_type=mime))
    else:
        ref_dir = Path(state["ref_dir"])
        if ref_dir.exists():
            for ref_file in sorted(ref_dir.glob("*")):
                if ref_file.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    data = ref_file.read_bytes()
                    mime = "image/png" if ref_file.suffix.lower() == ".png" else "image/jpeg"
                    ref_images.append(types.Part.from_bytes(data=data, mime_type=mime))

    contents = list(ref_images) + [ref_instruction + prompt_text]

    output_dir = Path(state["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i in range(variants):
        try:
            start = time.time()
            response = client.models.generate_content(
                model="gemini-3.1-flash-image-preview",
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["image", "text"],
                    temperature=temperature,
                ),
            )
            elapsed = time.time() - start

            variant_result = {"variant": i + 1, "elapsed": round(elapsed, 1), "parts": []}

            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    ext = part.inline_data.mime_type.split("/")[-1]
                    if ext == "jpeg":
                        ext = "jpg"
                    suffix = f"_v{i+1}" if variants > 1 else ""
                    filename = f"{output_name}{suffix}.{ext}"
                    out_path = output_dir / filename
                    out_path.write_bytes(part.inline_data.data)

                    img_b64 = base64.b64encode(part.inline_data.data).decode()
                    variant_result["parts"].append({
                        "type": "image",
                        "mime": part.inline_data.mime_type,
                        "size_kb": len(part.inline_data.data) // 1024,
                        "filename": filename,
                        "saved_to": str(out_path),
                        "data_url": f"data:{part.inline_data.mime_type};base64,{img_b64}",
                    })
                elif part.text:
                    variant_result["parts"].append({
                        "type": "text",
                        "content": part.text,
                    })

            results.append(variant_result)

        except Exception as e:
            results.append({
                "variant": i + 1,
                "error": str(e),
            })

        # Rate limit pause between variants
        if i < variants - 1:
            time.sleep(16)

    return JSONResponse({
        "ok": True,
        "results": results,
        "output_dir": str(output_dir),
        "refs_used": len(ref_images),
    })


@app.get("/api/output/images")
async def api_list_output_images():
    """List recently generated images in output directory."""
    output_dir = Path(state["output_dir"])
    if not output_dir.exists():
        return JSONResponse({"images": [], "dir": str(output_dir)})

    images = []
    for f in sorted(output_dir.glob("charsheet-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            images.append({
                "name": f.name,
                "size_kb": f.stat().st_size // 1024,
                "path": str(f),
                "mtime": f.stat().st_mtime,
            })
    return JSONResponse({"images": images[:20], "dir": str(output_dir)})


@app.get("/api/output/image/{filename}")
async def api_get_output_image(filename: str):
    """Serve a generated image."""
    path = Path(state["output_dir"]) / filename
    if not path.exists():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(path)


REGISTRY_PATH = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch/look-registry.json"
))
DINO_BUCH_DIR = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch"
))
# Ref images use paths relative to the parent (Comic_Projekt_2025/)
KINDERBUCH_BASE = DINO_BUCH_DIR.parent


@app.get("/api/registry")
async def api_list_registry():
    """List all entries in look-registry.json (titles + indices)."""
    import json
    if not REGISTRY_PATH.exists():
        return JSONResponse({"error": "Registry nicht gefunden"}, status_code=404)
    data = json.loads(REGISTRY_PATH.read_text())
    entries = []
    for i, entry in enumerate(data["bilder"]):
        entries.append({
            "index": i,
            "titel": entry.get("titel", ""),
            "sektion": entry.get("sektion", ""),
            "bewertung": entry.get("bewertung", ""),
            "datei": entry.get("datei", ""),
        })
    return JSONResponse({"entries": entries, "total": len(entries)})


def _load_image_as_thumb(path: Path, max_width: int = 300) -> dict | None:
    """Load an image file, return as base64 thumbnail dict."""
    if not path.exists():
        return None
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {
        "name": path.name,
        "path": str(path),
        "size_kb": len(data) // 1024,
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
    }


def _extract_dino_name(titel: str) -> str:
    """Extract the dino species name from a title.

    'Baby Ornithomimus Charsheet V1' → 'ornithomimus'
    'Ornithomimus-Kolonie Panorama V1' → 'ornithomimus'
    'Velociraptor Portrait' → 'velociraptor'
    """
    # Remove common prefixes/suffixes
    cleaned = titel.lower()
    for word in ["baby", "adult", "männchen", "weibchen", "charsheet",
                 "panorama", "kolonie", "portrait", "ganzkoerper", "ganzkörper",
                 "jagd", "fluss", "seite", "frontal", "laufen", "rennt",
                 "v1", "v2", "v3", "v4", "v5", "revidiert", "final",
                 "frisch geschlüpft", "braun/tarnung", "soft camouflage",
                 "extravagante balz-federn", "cyan", "korrekturen", "4k 21:9",
                 "weiss", "hintergrund", "—", "-", "(", ")"]:
        cleaned = cleaned.replace(word, " ")
    # Take the longest remaining word (likely the species name)
    words = [w.strip() for w in cleaned.split() if len(w.strip()) > 3]
    return words[0] if words else ""


def suggest_refs_for_entry(all_entries: list, current_index: int) -> list[dict]:
    """Suggest the best reference images for a registry entry.

    Strategy:
    1. Same dino species → highest priority (charsheets > scenes)
    2. TOP6 rated images → good style references
    3. Same sektion → related images
    Excludes the current image itself.
    Returns list of {index, titel, datei, score, reason} sorted by score.
    """
    current = all_entries[current_index]
    current_dino = _extract_dino_name(current.get("titel", ""))
    current_sektion = current.get("sektion", "")

    suggestions = []
    for i, entry in enumerate(all_entries):
        if i == current_index:
            continue
        score = 0
        reasons = []
        entry_dino = _extract_dino_name(entry.get("titel", ""))
        titel = entry.get("titel", "").lower()
        datei = entry.get("datei", "")

        # Same dino species — highest priority
        if current_dino and entry_dino and current_dino == entry_dino:
            score += 100
            reasons.append("gleicher Dino")
            # Charsheets are better refs than scenes
            if "charsheet" in datei.lower():
                score += 30
                reasons.append("Charsheet")
            # Prefer approved/final versions
            if "final" in titel or "v3" in titel:
                score += 10
                reasons.append("Final")

        # TOP6 rated — good style benchmark
        if entry.get("bewertung") == "TOP6":
            score += 50
            reasons.append("TOP6")

        # Same sektion
        if current_sektion and entry.get("sektion") == current_sektion:
            score += 20
            reasons.append("gleiche Sektion")

        # Charsheets are generally better refs
        if "charsheet" in datei.lower() and score > 0:
            score += 5

        if score > 0:
            suggestions.append({
                "index": i,
                "titel": entry.get("titel", ""),
                "datei": datei,
                "bewertung": entry.get("bewertung", ""),
                "score": score,
                "reason": ", ".join(reasons),
            })

    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions


@app.get("/api/registry/{index}")
async def api_load_registry(index: int):
    """Load a specific entry from look-registry.json into the editor."""
    import json
    if not REGISTRY_PATH.exists():
        return JSONResponse({"error": "Registry nicht gefunden"}, status_code=404)
    data = json.loads(REGISTRY_PATH.read_text())
    if index < 0 or index >= len(data["bilder"]):
        return JSONResponse({"error": f"Index {index} nicht vorhanden"}, status_code=404)

    entry = data["bilder"][index]
    prompt = entry.get("prompt", "")
    refs = entry.get("referenzbilder", [])

    # Split the monolithic prompt into named blocks
    blocks = split_prompt_into_blocks(prompt)

    # Load the original image as base64 for preview
    original_image = None
    img_path = DINO_BUCH_DIR / entry.get("datei", "")
    if img_path.exists():
        original_image = _load_image_as_thumb(img_path)

    # Resolve explicit ref image paths from registry
    ref_images = []
    ref_dir_resolved = None
    for ref_path_str in refs:
        ref_path = KINDERBUCH_BASE / ref_path_str
        if not ref_path.exists():
            ref_path = DINO_BUCH_DIR / ref_path_str
        if ref_path.exists():
            if not ref_dir_resolved:
                ref_dir_resolved = str(ref_path.parent)
            thumb = _load_image_as_thumb(ref_path)
            if thumb:
                ref_images.append(thumb)

    # Intelligent ref suggestions from the registry
    suggestions = suggest_refs_for_entry(data["bilder"], index)

    # Auto-select top refs if registry has none explicitly listed
    suggested_refs = []
    if not ref_images:
        # Load the top suggested images as refs (max 8)
        for sug in suggestions[:8]:
            sug_path = DINO_BUCH_DIR / sug["datei"]
            thumb = _load_image_as_thumb(sug_path)
            if thumb:
                thumb["reason"] = sug["reason"]
                thumb["score"] = sug["score"]
                thumb["registry_index"] = sug["index"]
                suggested_refs.append(thumb)

    # Determine output dir from the image path
    output_dir = str(img_path.parent) if img_path.exists() else str(DEFAULT_OUTPUT_DIR)
    output_name = img_path.stem if img_path.exists() else "generated"

    return JSONResponse({
        "index": index,
        "titel": entry.get("titel", ""),
        "sektion": entry.get("sektion", ""),
        "bewertung": entry.get("bewertung", ""),
        "notiz": entry.get("notiz", ""),
        "prompt": prompt,
        "blocks": blocks,
        "original_image": original_image,
        "ref_images": ref_images,
        "suggested_refs": suggested_refs,
        "all_suggestions": suggestions[:20],  # For the picker UI
        "ref_dir": ref_dir_resolved,
        "output_dir": output_dir,
        "output_name": output_name,
        "temperature": 1.0,
        "variants": 1,
    })


@app.get("/api/registry/image/{index}")
async def api_registry_image(index: int):
    """Serve a registry image by index (for the ref picker)."""
    import json
    if not REGISTRY_PATH.exists():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    data = json.loads(REGISTRY_PATH.read_text())
    if index < 0 or index >= len(data["bilder"]):
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    img_path = DINO_BUCH_DIR / data["bilder"][index]["datei"]
    if not img_path.exists():
        return JSONResponse({"error": "Bild nicht gefunden"}, status_code=404)
    return FileResponse(img_path)


if __name__ == "__main__":
    import uvicorn
    print("\n  🦕 Dino-Bildgen-UI")
    print(f"  http://localhost:8080\n")
    uvicorn.run(app, host="0.0.0.0", port=8080)
