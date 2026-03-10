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
    return JSONResponse({"ok": True, "dir": expanded})


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

    ref_dir = Path(state["ref_dir"])
    ref_images = []
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


if __name__ == "__main__":
    import uvicorn
    print("\n  🦕 Dino-Bildgen-UI")
    print(f"  http://localhost:8080\n")
    uvicorn.run(app, host="0.0.0.0", port=8080)
