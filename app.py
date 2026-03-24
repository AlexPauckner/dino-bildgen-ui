#!/usr/bin/env python3
"""Dino-Bildgen-UI V3 — Lokale Web-App fuer Gemini-Bildgenerierung.

V3: 5-Block-Schema (STYLE/SCENE/CHARACTER/COMPOSITION/NEGATIVE),
    3 Ref-Kategorien (Style/Character/Scribble), NB2-optimiert.
"""

import base64
import json
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- Config ---
API_KEY_PATH = Path(os.path.expanduser("~/.google_api_key"))
DEFAULT_REF_DIR = Path("/tmp/dino-neue-refs")
DEFAULT_OUTPUT_DIR = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch/Charsheets-Dinos"
))
REGISTRY_PATH = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch/look-registry.json"
))
DINO_BUCH_DIR = Path(os.path.expanduser(
    "~/Kinderbuch/Comic_Projekt_2025/Dino-Buch"
))
KINDERBUCH_BASE = DINO_BUCH_DIR.parent

# Upscayl config
UPSCAYL_BIN = Path("/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin")
UPSCAYL_MODELS = Path("/Applications/Upscayl.app/Contents/Resources/models")

# Runtime state
state = {
    "ref_dir": str(DEFAULT_REF_DIR),
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "ref_paths": [],
}

# --- V3 Block Schema ---
BLOCK_KEYS = ["style", "scene", "character", "composition", "negative"]

LEGACY_KEY_MAP = {
    "style_header": "style",
    "logline": "scene",
    "child_char_block": "character",
    "scene_block": "scene",
    "brush_guide": "style",
    "medium_block": "style",
    "negative_block": "negative",
}

REF_INSTRUCTIONS = {
    "style": (
        "Use these images as STYLE REFERENCE \u2014 match the oil paint texture, "
        "impasto brush technique, color warmth, and paint density exactly:"
    ),
    "character": (
        "Use these images as CHARACTER REFERENCE \u2014 preserve exact proportions, "
        "feather colors, eye shape, and markings. Do not change the character design:"
    ),
    "scribble": (
        "Use this rough SKETCH as COMPOSITION GUIDE \u2014 follow the positioning "
        "and layout but render everything in the oil paint style described above:"
    ),
}

CONTEXT_PREFIX = (
    "Create an oil painted children's book illustration for a dinosaur "
    "science book aimed at 3-8 year olds."
)

IDENTITY_LOCK = (
    "Preserve exact proportions, feather colors, eye shape, "
    "and markings from the character reference."
)


# --- Script Parser (V1/V2 compat, unchanged) ---

KNOWN_BLOCKS = [
    "STYLE_HEADER", "CHILD_CHAR_BLOCK", "BABY_CHAR_BLOCK",
    "BRUSH_GUIDE", "MEDIUM_BLOCK", "NEGATIVE_BLOCK",
]


def parse_script(source):
    # type: (str) -> dict
    """Parse a Python generation script into named blocks (old format)."""
    blocks = {}

    pattern = r'(\w+)\s*=\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')'
    for match in re.finditer(pattern, source, re.DOTALL):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        blocks[name] = value.strip()

    prompt_match = re.search(r'PROMPT\s*=\s*f"""(.*?)"""', source, re.DOTALL)
    if not prompt_match:
        prompt_match = re.search(r"PROMPT\s*=\s*f'''(.*?)'''", source, re.DOTALL)

    scene_block = ""
    if prompt_match:
        prompt_template = prompt_match.group(1).strip()
        scene = prompt_template
        for block_name in KNOWN_BLOCKS:
            scene = scene.replace(f"{{{block_name}}}", "<<<BLOCK_MARKER>>>")
        parts = scene.split("<<<BLOCK_MARKER>>>")
        meaningful = [p.strip() for p in parts if p.strip()]
        scene_block = "\n\n".join(meaningful)

    ref_match = re.search(r'ref_instruction\s*=\s*\((.*?)\)', source, re.DOTALL)
    if ref_match:
        raw = ref_match.group(1)
        parts = re.findall(r'"(.*?)"', raw, re.DOTALL)
        blocks["REF_INSTRUCTION"] = "".join(parts)
    else:
        ref_match = re.search(
            r'ref_instruction\s*=\s*(?:"""(.*?)"""|"(.*?)")', source, re.DOTALL
        )
        if ref_match:
            blocks["REF_INSTRUCTION"] = (ref_match.group(1) or ref_match.group(2) or "").strip()

    out_match = re.search(r'OUTPUT_DIR\s*=\s*Path\([^"]*"([^"]+)"', source)
    if out_match:
        blocks["_OUTPUT_DIR"] = os.path.expanduser(out_match.group(1))

    ref_match2 = re.search(r'REF_DIR\s*=\s*Path\([^"]*"([^"]+)"', source)
    if ref_match2:
        blocks["_REF_DIR"] = os.path.expanduser(ref_match2.group(1))

    temp_match = re.search(r'temperature\s*=\s*([\d.]+)', source)
    if temp_match:
        blocks["_TEMPERATURE"] = float(temp_match.group(1))

    range_match = re.search(r'range\((\d+)\)', source)
    if range_match:
        blocks["_VARIANTS"] = int(range_match.group(1))

    return {
        "style_header": blocks.get("STYLE_HEADER", ""),
        "logline": "",
        "child_char_block": blocks.get("CHILD_CHAR_BLOCK", blocks.get("BABY_CHAR_BLOCK", "")),
        "scene_block": scene_block,
        "brush_guide": blocks.get("BRUSH_GUIDE", ""),
        "medium_block": blocks.get("MEDIUM_BLOCK", ""),
        "negative_block": blocks.get("NEGATIVE_BLOCK", ""),
        "ref_instruction": blocks.get("REF_INSTRUCTION", ""),
        "temperature": blocks.get("_TEMPERATURE", 1.0),
        "variants": blocks.get("_VARIANTS", 1),
        "output_dir": blocks.get("_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)),
        "ref_dir": blocks.get("_REF_DIR", str(DEFAULT_REF_DIR)),
    }


# --- Prompt Splitting (V3) ---

def split_prompt_into_blocks(prompt):
    # type: (str) -> dict
    """Split a prompt into 5 named blocks + ref_description using keyword + legacy detection.

    Primary: INPUT IMAGES:/STYLE:/SCENE:/CHARACTER:/COMPOSITION:/NEGATIVE: keywords.
    Legacy ref: "I am giving you ... reference images" / "IMAGE N —" patterns.
    Legacy style: Oil paint.../Child proportions.../Brush stroke guide:/Medium:/No photorealism...
    """
    all_keys = BLOCK_KEYS + ["ref_description"]
    block_lines = {k: [] for k in all_keys}
    lines = prompt.strip().split('\n')
    current_block = None

    for line in lines:
        stripped = line.strip()
        detected = None

        # Primary V3 keywords
        if re.match(r'^INPUT IMAGES:', stripped, re.IGNORECASE):
            detected = "ref_description"
        elif re.match(r'^STYLE:', stripped):
            detected = "style"
        elif re.match(r'^SCENE( INSTRUCTIONS)?:', stripped):
            detected = "scene"
        elif re.match(r'^CHARACTER:', stripped):
            detected = "character"
        elif re.match(r'^COMPOSITION:', stripped):
            detected = "composition"
        elif re.match(r'^(NEGATIVE|CONSTRAINTS):', stripped):
            detected = "negative"
        # Legacy ref description patterns
        elif re.match(r'^I am giving you .* reference image', stripped, re.IGNORECASE):
            detected = "ref_description"
        elif re.match(r'^IMAGE \d+', stripped) and current_block in (None, "ref_description", "scene"):
            # "IMAGE 1 — LAYOUT REFERENCE" etc. — belongs to ref_description
            if current_block != "ref_description" and not block_lines["ref_description"]:
                detected = "ref_description"
            elif current_block == "ref_description":
                pass  # stay in ref_description
        elif re.match(r'^COMBINE both:', stripped, re.IGNORECASE) and current_block == "ref_description":
            pass  # stays in ref_description
        # Legacy V1/V2 patterns
        elif re.match(r'^(Oil paint|REAL oil paint)', stripped, re.IGNORECASE) and not block_lines["style"]:
            detected = "style"
        elif re.match(r'^Child proportions', stripped, re.IGNORECASE) and not block_lines["character"]:
            detected = "character"
        elif re.match(r'^Brush stroke guide', stripped, re.IGNORECASE):
            detected = "style"
        elif re.match(r'^Medium:', stripped, re.IGNORECASE):
            detected = "style"
        elif re.match(r'^SURFACE', stripped, re.IGNORECASE):
            detected = "style"
        elif re.match(r'^(EMOTION|LIGHTING|FORMAT):', stripped, re.IGNORECASE):
            detected = "composition"
        elif re.match(r'^No photorealism', stripped, re.IGNORECASE):
            detected = "negative"

        if detected is not None:
            current_block = detected
            block_lines[current_block].append(line)
        elif current_block is not None:
            block_lines[current_block].append(line)
        else:
            block_lines["scene"].append(line)

    return {k: '\n'.join(block_lines[k]).strip() for k in all_keys}


def build_prompt(blocks):
    # type: (dict) -> str
    """Assemble full prompt from V3 5-block format."""
    parts = []
    for key in BLOCK_KEYS:
        val = blocks.get(key, "")
        if val:
            parts.append(val)
    return "\n\n".join(parts)


def _map_legacy_blocks(data):
    # type: (dict) -> dict
    """Map old V1/V2 block keys to V3 keys, merging duplicates."""
    # Already V3?
    if any(data.get(k) for k in BLOCK_KEYS):
        return {k: data.get(k, "") for k in BLOCK_KEYS}
    # Map legacy
    merge = {k: [] for k in BLOCK_KEYS}
    for old_key, new_key in LEGACY_KEY_MAP.items():
        val = data.get(old_key, "")
        if val:
            merge[new_key].append(val)
    return {k: "\n\n".join(merge[k]) for k in BLOCK_KEYS}


def _extract_paths(ref_list):
    # type: (list) -> list
    """Extract path strings from mixed format ref lists."""
    paths = []
    for r in ref_list:
        if isinstance(r, str):
            paths.append(r)
        elif isinstance(r, dict):
            p = r.get("path", "")
            if p:
                paths.append(p)
    return [p for p in paths if p]


def _make_relative(paths, base=None):
    # type: (list, Optional[Path]) -> list
    """Make paths relative to base directory."""
    if base is None:
        base = KINDERBUCH_BASE
    result = []
    for p in paths:
        p = Path(p)
        try:
            result.append(str(p.relative_to(base)))
        except ValueError:
            result.append(p.name)
    return result


def _parse_registry_refs(refs_raw):
    # type: (object) -> dict
    """Parse registry referenzbilder field (handles old flat list + new dict)."""
    if isinstance(refs_raw, list):
        return {"style": refs_raw, "character": [], "scribble": []}
    elif isinstance(refs_raw, dict):
        return {
            "style": refs_raw.get("style", []),
            "character": refs_raw.get("character", []),
            "scribble": refs_raw.get("scribble", []),
        }
    return {"style": [], "character": [], "scribble": []}


# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.post("/api/parse-script")
async def api_parse_script(file: UploadFile = File(...)):
    """Parse an uploaded Python script into V3 blocks."""
    content = await file.read()
    source = content.decode("utf-8", errors="replace")
    old_result = parse_script(source)

    # Map to V3 blocks
    v3_blocks = _map_legacy_blocks(old_result)

    state["ref_dir"] = old_result["ref_dir"]
    state["output_dir"] = old_result["output_dir"]

    result = dict(v3_blocks)
    result["temperature"] = old_result["temperature"]
    result["variants"] = old_result["variants"]
    result["output_dir"] = old_result["output_dir"]
    result["ref_dir"] = old_result["ref_dir"]
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
                "data_url": "data:%s;base64,%s" % (mime, data),
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
    return JSONResponse({"ok": True, "name": file.filename, "path": str(dest)})


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
    state["ref_paths"] = []
    return JSONResponse({"ok": True, "dir": expanded})


@app.post("/api/refs/set-paths")
async def api_set_ref_paths(request: Request):
    """Set explicit reference image paths."""
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
    """Generate image via Gemini API (V3: role-based refs)."""
    body = await request.json()

    prompt_text = body.get("prompt", "")
    temperature = float(body.get("temperature", 1.0))
    variants = int(body.get("variants", 1))
    output_name = body.get("output_name", "generated")
    context_prefix = body.get("context_prefix", False)
    ref_description = body.get("ref_description", "").strip()
    model = body.get("model", "gemini-3.1-flash-image-preview")
    aspect_ratio = body.get("aspect_ratio", "")
    image_size = body.get("image_size", "")
    thinking_level = body.get("thinking_level", "")

    if not prompt_text:
        return JSONResponse({"error": "Kein Prompt angegeben"}, status_code=400)

    if not API_KEY_PATH.exists():
        return JSONResponse({"error": "API Key nicht gefunden: %s" % API_KEY_PATH}, status_code=500)
    api_key = API_KEY_PATH.read_text().strip()

    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)

    def load_ref_images(paths):
        images = []
        for rp in paths:
            rp = Path(rp)
            if rp.exists() and rp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                data = rp.read_bytes()
                mime = "image/png" if rp.suffix.lower() == ".png" else "image/jpeg"
                images.append(types.Part.from_bytes(data=data, mime_type=mime))
        return images

    # --- Detect legacy vs V3 format ---
    is_legacy = "ref_instruction" in body and "refs_style" not in body

    if is_legacy:
        # V2 compat: flat refs + ref_instruction prefix
        ref_instruction = body.get("ref_instruction", "")
        legacy_paths = body.get("ref_paths", state.get("ref_paths", []))
        ref_images = load_ref_images(legacy_paths) if legacy_paths else []
        if not ref_images:
            ref_dir = Path(state["ref_dir"])
            if ref_dir.exists():
                for ref_file in sorted(ref_dir.glob("*")):
                    if ref_file.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                        data = ref_file.read_bytes()
                        mime = "image/png" if ref_file.suffix.lower() == ".png" else "image/jpeg"
                        ref_images.append(types.Part.from_bytes(data=data, mime_type=mime))
        contents = list(ref_images) + [ref_instruction + prompt_text]
        total_refs = len(ref_images)
        ref_paths_for_registry = legacy_paths or []
    else:
        # V3: role-based refs
        style_paths = _extract_paths(body.get("refs_style", []))
        char_paths = _extract_paths(body.get("refs_character", []))
        scribble_paths = _extract_paths(body.get("refs_scribble", []))

        # Legacy fallback: ref_paths → style
        if not any([style_paths, char_paths, scribble_paths]):
            legacy = body.get("ref_paths", [])
            if legacy:
                style_paths = legacy

        style_refs = load_ref_images(style_paths)
        char_refs = load_ref_images(char_paths)
        scribble_refs = load_ref_images(scribble_paths)

        # Build role-based contents
        contents = []
        all_refs = style_refs + char_refs + scribble_refs
        if ref_description and all_refs:
            # Custom ref description overrides auto-instructions
            contents.append(ref_description)
            contents.extend(all_refs)
        else:
            if style_refs:
                contents.append(REF_INSTRUCTIONS["style"])
                contents.extend(style_refs)
            if char_refs:
                contents.append(REF_INSTRUCTIONS["character"])
                contents.extend(char_refs)
            if scribble_refs:
                contents.append(REF_INSTRUCTIONS["scribble"])
                contents.extend(scribble_refs)

        # Build generation prompt (context + identity lock are generation-time only)
        gen_prompt = prompt_text
        if context_prefix:
            gen_prompt = CONTEXT_PREFIX + "\n\n" + gen_prompt
        if char_refs:
            gen_prompt += "\n\n" + IDENTITY_LOCK

        contents.append(gen_prompt)
        total_refs = len(style_refs) + len(char_refs) + len(scribble_refs)
        ref_paths_for_registry = {
            "style": style_paths,
            "character": char_paths,
            "scribble": scribble_paths,
        }

    output_dir = Path(state["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i in range(variants):
        try:
            start = time.time()
            # Build config
            config_kwargs = {
                "response_modalities": ["image", "text"],
                "temperature": temperature,
            }
            if aspect_ratio:
                config_kwargs["image_config"] = types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                )
            if image_size:
                ic = config_kwargs.get("image_config")
                if ic:
                    # ImageConfig already set — rebuild with both
                    config_kwargs["image_config"] = types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                    )
                else:
                    config_kwargs["image_config"] = types.ImageConfig(
                        image_size=image_size,
                    )
            if thinking_level:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=thinking_level,
                )

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            elapsed = time.time() - start

            variant_result = {"variant": i + 1, "elapsed": round(elapsed, 1), "parts": []}

            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    ext = part.inline_data.mime_type.split("/")[-1]
                    if ext == "jpeg":
                        ext = "jpg"
                    suffix = "_v%d" % (i + 1) if variants > 1 else ""
                    filename = "%s%s.%s" % (output_name, suffix, ext)
                    out_path = output_dir / filename
                    counter = 2
                    while out_path.exists():
                        filename = "%s%s_%d.%s" % (output_name, suffix, counter, ext)
                        out_path = output_dir / filename
                        counter += 1
                    out_path.write_bytes(part.inline_data.data)

                    img_b64 = base64.b64encode(part.inline_data.data).decode()
                    variant_result["parts"].append({
                        "type": "image",
                        "mime": part.inline_data.mime_type,
                        "size_kb": len(part.inline_data.data) // 1024,
                        "filename": filename,
                        "saved_to": str(out_path),
                        "data_url": "data:%s;base64,%s" % (part.inline_data.mime_type, img_b64),
                    })
                elif part.text:
                    variant_result["parts"].append({
                        "type": "text",
                        "content": part.text,
                    })

            results.append(variant_result)
        except Exception as e:
            results.append({"variant": i + 1, "error": str(e)})

        if i < variants - 1:
            time.sleep(16)

    # Add to registry + rebuild HTML
    for result_item in results:
        if "error" in result_item:
            continue
        for part_item in result_item.get("parts", []):
            if part_item.get("type") == "image":
                # Full prompt for registry: ref_description + prompt blocks
                full_prompt = (ref_description + "\n\n" + prompt_text).strip() if ref_description else prompt_text
                _add_to_registry(
                    datei=part_item["saved_to"],
                    titel=output_name,
                    prompt=full_prompt,
                    ref_paths_categorized=ref_paths_for_registry,
                    ref_description=ref_description,
                    temperature=temperature,
                    refs_count=total_refs,
                    model=model,
                )
    _rebuild_html()

    return JSONResponse({
        "ok": True,
        "results": results,
        "output_dir": str(output_dir),
        "refs_used": total_refs,
    })


@app.post("/api/output/open")
async def api_open_output():
    """Open the output directory in Finder."""
    output_dir = Path(state["output_dir"])
    if output_dir.exists():
        subprocess.Popen(["open", str(output_dir)])
    return JSONResponse({"ok": True, "dir": str(output_dir)})


@app.get("/api/output/images")
async def api_list_output_images():
    """List recently generated images in output directory."""
    output_dir = Path(state["output_dir"])
    if not output_dir.exists():
        return JSONResponse({"images": [], "dir": str(output_dir)})

    images = []
    for f in sorted(output_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            images.append({
                "name": f.name,
                "size_kb": f.stat().st_size // 1024,
                "path": str(f),
                "mtime": f.stat().st_mtime,
            })
    return JSONResponse({"images": images[:20], "dir": str(output_dir)})


@app.get("/api/upscayl/status")
async def api_upscayl_status():
    """Check if Upscayl CLI is available."""
    available = UPSCAYL_BIN.exists() and UPSCAYL_MODELS.exists()
    return JSONResponse({"available": available})


@app.post("/api/upscale")
async def api_upscale(request: Request):
    """Upscale an image using Upscayl CLI."""
    body = await request.json()
    input_path = body.get("path", "")
    model = body.get("model", "high-fidelity-4x")
    scale = int(body.get("scale", 2))

    if not input_path:
        return JSONResponse({"error": "Kein Bildpfad angegeben"}, status_code=400)

    src = Path(input_path)
    if not src.exists():
        return JSONResponse({"error": "Datei nicht gefunden: %s" % input_path}, status_code=404)

    if not UPSCAYL_BIN.exists():
        return JSONResponse({"error": "Upscayl nicht installiert"}, status_code=500)

    # Build output filename: original_upscayl2k.png
    suffix = "_upscayl%dk" % scale
    out_name = src.stem + suffix + ".png"
    out_path = src.parent / out_name
    counter = 2
    while out_path.exists():
        out_name = src.stem + suffix + "_%d.png" % counter
        out_path = src.parent / out_name
        counter += 1

    try:
        start = time.time()
        result = subprocess.run(
            [
                str(UPSCAYL_BIN),
                "-i", str(src),
                "-o", str(out_path),
                "-m", str(UPSCAYL_MODELS),
                "-n", model,
                "-s", str(scale),
                "-f", "png",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.time() - start

        if not out_path.exists():
            return JSONResponse({
                "error": "Upscayl hat kein Bild erzeugt",
                "stderr": result.stderr[-500:] if result.stderr else "",
            }, status_code=500)

        # Read upscaled image for preview
        data = out_path.read_bytes()
        img_b64 = base64.b64encode(data).decode()

        return JSONResponse({
            "ok": True,
            "filename": out_name,
            "saved_to": str(out_path),
            "size_kb": len(data) // 1024,
            "elapsed": round(elapsed, 1),
            "data_url": "data:image/png;base64,%s" % img_b64,
            "model": model,
            "scale": scale,
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Upscayl Timeout (>120s)"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": "Upscayl Fehler: %s" % str(e)}, status_code=500)


@app.get("/api/output/image/{filename}")
async def api_get_output_image(filename: str):
    """Serve a generated image."""
    path = Path(state["output_dir"]) / filename
    if not path.exists():
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(path)


@app.get("/api/image")
async def api_serve_image(path: str = ""):
    """Serve an image by absolute path (for lightbox full-res view)."""
    p = Path(path)
    if not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return JSONResponse({"error": "Nicht gefunden"}, status_code=404)
    return FileResponse(p)


# --- Registry ---

def _add_to_registry(datei, titel, prompt, ref_paths_categorized, temperature, refs_count, ref_description="", model="gemini-3.1-flash-image-preview"):
    # type: (str, str, str, object, float, int, str, str) -> None
    """Add a generated image to look-registry.json (V3: categorized refs)."""
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except Exception:
        return

    datei_path = Path(datei)
    try:
        rel = datei_path.relative_to(DINO_BUCH_DIR)
    except ValueError:
        rel = datei_path.name

    # Handle both legacy (list) and V3 (dict) ref formats
    if isinstance(ref_paths_categorized, list):
        rel_refs = {
            "style": _make_relative(ref_paths_categorized),
            "character": [],
            "scribble": [],
        }
    elif isinstance(ref_paths_categorized, dict):
        rel_refs = {
            "style": _make_relative(ref_paths_categorized.get("style", [])),
            "character": _make_relative(ref_paths_categorized.get("character", [])),
            "scribble": _make_relative(ref_paths_categorized.get("scribble", [])),
        }
    else:
        rel_refs = {"style": [], "character": [], "scribble": []}

    entry = {
        "datei": str(rel),
        "titel": titel,
        "sektion": "Bildgen-UI",
        "tool": "Bildgen-UI",
        "modell": model,
        "prompt": prompt,
        "parameter": "%d Refs, temp %s" % (refs_count, temperature),
        "bewertung": "",
        "session": str(date.today()),
        "notiz": "Via Bildgen-UI V3 generiert",
        "ref_description": ref_description,
        "referenzbilder": rel_refs,
    }
    data["bilder"].append(entry)
    REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _rebuild_html():
    """Run build script to regenerate the HTML gallery."""
    try:
        subprocess.Popen(
            ["python3", str(DINO_BUCH_DIR / "build-look-vergleich.py")],
            cwd=str(DINO_BUCH_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


@app.get("/api/registry")
async def api_list_registry():
    """List all entries in look-registry.json."""
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


def _load_image_as_thumb(path):
    # type: (Path) -> Optional[dict]
    """Load an image file, return as base64 thumbnail dict."""
    if not path.exists():
        return None
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {
        "name": path.name,
        "path": str(path),
        "size_kb": len(data) // 1024,
        "data_url": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode()),
    }


def _extract_dino_name(titel):
    # type: (str) -> str
    """Extract dino species name from a title."""
    cleaned = titel.lower()
    for word in ["baby", "adult", "m\u00e4nnchen", "weibchen", "charsheet",
                 "panorama", "kolonie", "portrait", "ganzkoerper", "ganzk\u00f6rper",
                 "jagd", "fluss", "seite", "frontal", "laufen", "rennt",
                 "v1", "v2", "v3", "v4", "v5", "revidiert", "final",
                 "frisch geschl\u00fcpft", "braun/tarnung", "soft camouflage",
                 "extravagante balz-federn", "cyan", "korrekturen", "4k 21:9",
                 "weiss", "hintergrund", "\u2014", "-", "(", ")"]:
        cleaned = cleaned.replace(word, " ")
    words = [w.strip() for w in cleaned.split() if len(w.strip()) > 3]
    return words[0] if words else ""


def suggest_refs_for_entry(all_entries, current_index):
    # type: (list, int) -> list
    """Suggest best reference images for a registry entry with role hints."""
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

        if current_dino and entry_dino and current_dino == entry_dino:
            score += 100
            reasons.append("gleicher Dino")
            if "charsheet" in datei.lower():
                score += 30
                reasons.append("Charsheet")
            if "final" in titel or "v3" in titel:
                score += 10
                reasons.append("Final")

        if entry.get("bewertung") == "TOP6":
            score += 50
            reasons.append("TOP6")

        if current_sektion and entry.get("sektion") == current_sektion:
            score += 20
            reasons.append("gleiche Sektion")

        if "charsheet" in datei.lower() and score > 0:
            score += 5

        if score > 0:
            # Suggest role based on scoring reason
            reason_str = ", ".join(reasons)
            if "charsheet" in reason_str.lower() or "gleicher dino" in reason_str.lower():
                suggested_role = "character"
            else:
                suggested_role = "style"

            suggestions.append({
                "index": i,
                "titel": entry.get("titel", ""),
                "datei": datei,
                "bewertung": entry.get("bewertung", ""),
                "score": score,
                "reason": reason_str,
                "suggested_role": suggested_role,
            })

    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions


@app.get("/api/registry/{index}")
async def api_load_registry(index: int):
    """Load a registry entry into the editor (V3: categorized refs)."""
    if not REGISTRY_PATH.exists():
        return JSONResponse({"error": "Registry nicht gefunden"}, status_code=404)
    data = json.loads(REGISTRY_PATH.read_text())
    if index < 0 or index >= len(data["bilder"]):
        return JSONResponse({"error": "Index %d nicht vorhanden" % index}, status_code=404)

    entry = data["bilder"][index]
    prompt = entry.get("prompt", "")
    refs_raw = entry.get("referenzbilder", [])

    # Split prompt into V3 blocks
    blocks = split_prompt_into_blocks(prompt)

    # ref_description: prefer registry field over splitter result
    # (newer entries store ref_description separately, not inside prompt)
    registry_rd = entry.get("ref_description", "").strip()
    if registry_rd:
        blocks["ref_description"] = registry_rd

    # Load original image
    original_image = None
    img_path = DINO_BUCH_DIR / entry.get("datei", "")
    if img_path.exists():
        original_image = _load_image_as_thumb(img_path)

    # Parse refs (handles both old flat list and new categorized dict)
    refs_categorized = _parse_registry_refs(refs_raw)

    # Load ref images per category
    ref_images = {"style": [], "character": [], "scribble": []}
    ref_dir_resolved = None
    for role in ["style", "character", "scribble"]:
        for ref_path_str in refs_categorized.get(role, []):
            ref_path = KINDERBUCH_BASE / ref_path_str
            if not ref_path.exists():
                ref_path = DINO_BUCH_DIR / ref_path_str
            if ref_path.exists():
                if not ref_dir_resolved:
                    ref_dir_resolved = str(ref_path.parent)
                thumb = _load_image_as_thumb(ref_path)
                if thumb:
                    ref_images[role].append(thumb)

    has_any_refs = any(ref_images[r] for r in ref_images)

    # Intelligent suggestions
    suggestions = suggest_refs_for_entry(data["bilder"], index)

    # Auto-select top refs if none explicitly listed
    suggested_refs = []
    if not has_any_refs:
        for sug in suggestions[:8]:
            sug_path = DINO_BUCH_DIR / sug["datei"]
            thumb = _load_image_as_thumb(sug_path)
            if thumb:
                thumb["reason"] = sug["reason"]
                thumb["score"] = sug["score"]
                thumb["registry_index"] = sug["index"]
                thumb["suggested_role"] = sug.get("suggested_role", "style")
                suggested_refs.append(thumb)

    # Determine output dir
    output_dir = str(img_path.parent) if img_path.exists() else str(DEFAULT_OUTPUT_DIR)
    output_name = img_path.stem if img_path.exists() else "generated"

    state["output_dir"] = output_dir
    if ref_dir_resolved:
        state["ref_dir"] = ref_dir_resolved

    return JSONResponse({
        "index": index,
        "titel": entry.get("titel", ""),
        "sektion": entry.get("sektion", ""),
        "bewertung": entry.get("bewertung", ""),
        "notiz": entry.get("notiz", ""),
        "prompt": prompt,
        "ref_description": entry.get("ref_description", ""),
        "blocks": blocks,
        "original_image": original_image,
        "ref_images": ref_images,
        "suggested_refs": suggested_refs,
        "all_suggestions": suggestions[:20],
        "ref_dir": ref_dir_resolved,
        "output_dir": output_dir,
        "output_name": output_name,
        "temperature": 1.0,
        "variants": 1,
    })


@app.get("/api/registry/image/{index}")
async def api_registry_image(index: int):
    """Serve a registry image by index."""
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
    print("\n  Dino-Bildgen-UI V3")
    print("  http://localhost:8899\n")
    uvicorn.run(app, host="0.0.0.0", port=8899)
