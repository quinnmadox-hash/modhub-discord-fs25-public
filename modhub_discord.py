"""Publica en Discord mods nuevos y actualizaciones de FS25 del ModHub oficial.

V4.1: añade Solo PC / Multiplataforma a los avisos de Discord,
sin modificar la detección ni el historial de mods.
"""

import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = "https://www.farming-simulator.com/"
LIST = BASE + "mods.php?lang=en&country=es&title=fs2025&filter=latest&page={}"
STATE = Path(__file__).with_name("seen.json")
USER_AGENT = "Mozilla/5.0 (compatible; FS25-ModHub-Discord/4.1)"
MAX_PAGES = 6


class ModParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.item = None
        self.items = []
        self.in_title = False
        self.in_author = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class", "").split()
        if tag == "div" and "mod-item" in classes:
            self.depth = 1
            self.item = {"title": "", "author": "", "url": "", "image": "", "_all_text": []}
            return
        if self.item is None:
            return
        if tag == "div":
            self.depth += 1
        elif tag == "a" and "mod_id=" in a.get("href", ""):
            self.item["url"] = urljoin(BASE, a["href"])
            match = re.search(r"mod_id=(\d+)", a["href"])
            if match:
                self.item["id"] = match.group(1)
        elif tag == "img" and not self.item["image"]:
            self.item["image"] = urljoin(BASE, a.get("src", ""))
        elif tag == "h4":
            self.in_title = True
        elif tag == "span" and not self.item["author"]:
            self.in_author = True

    def handle_endtag(self, tag):
        if self.item is None:
            return
        if tag == "h4":
            self.in_title = False
        elif tag == "span":
            self.in_author = False
        elif tag == "div":
            self.depth -= 1
            if self.depth == 0:
                if self.item.get("id") and self.item["title"] and self.item["image"]:
                    all_text = " ".join(self.item.pop("_all_text", []))
                    if re.search(r"UPDATE\s*!", all_text, re.IGNORECASE):
                        self.item["marker"] = "UPDATE"
                    elif re.search(r"NEW\s*!", all_text, re.IGNORECASE):
                        self.item["marker"] = "NEW"
                    else:
                        self.item["marker"] = ""
                    self.items.append(self.item)
                self.item = None

    def handle_data(self, data):
        if self.item is not None:
            clean = " ".join(data.split())
            if clean:
                self.item["_all_text"].append(clean)
            if self.in_title:
                self.item["title"] += data
            elif self.in_author:
                self.item["author"] += data


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def fetch(url):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def get_details(mod):
    parser = TextParser()
    parser.feed(fetch(mod["url"]))
    text = "\n".join(parser.parts)
    patterns = [
        r"(?:^|\n)Version\s*\n\s*([0-9]+(?:\.[0-9]+)+)(?:\n|$)",
        r"\bVersion\s*[: ]\s*([0-9]+(?:\.[0-9]+)+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            version = match.group(1)
            break
    else:
        raise RuntimeError(f"No pude leer la versión de {mod['title'].strip()} ({mod['id']})")

    # La ficha oficial presenta Platform seguido de PC/MAC y, si procede, consolas.
    platform_match = re.search(r"(?:^|\n)Platform\s*\n\s*([^\n]+)", text, re.IGNORECASE)
    if platform_match:
        platforms = platform_match.group(1).upper()
        if re.search(r"\b(?:PS5|PS4|XBS|XBOX|S2|SWITCH)\b", platforms):
            platform = "🎮 Multiplataforma"
        elif re.search(r"\bPC\s*/\s*MAC\b|\bPC\b", platforms):
            platform = "🖥️ Solo PC"
        else:
            platform = "Plataforma no determinada"
    else:
        platform = "Plataforma no determinada"
    return version, platform


def get_mods():
    mods = []
    seen_ids = set()
    for page in range(MAX_PAGES):
        parser = ModParser()
        parser.feed(fetch(LIST.format(page)))
        if not parser.items:
            break
        for mod in parser.items:
            if mod["id"] not in seen_ids:
                seen_ids.add(mod["id"])
                mods.append(mod)
        if len(parser.items) < 10:
            break
    if len(mods) < 10:
        raise RuntimeError(f"ModHub devolvió solo {len(mods)} mods; cancelo para no alterar el estado.")
    return mods


def load_state():
    if not STATE.exists():
        return {"format": 3, "legacy_ids": [], "mods": {}}
    raw = json.loads(STATE.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {"format": 3, "legacy_ids": raw, "mods": {}}
    if isinstance(raw, dict) and raw.get("format") in (2, 3):
        raw["format"] = 3
        raw.setdefault("legacy_ids", [])
        raw.setdefault("mods", {})
        return raw
    raise RuntimeError("seen.json tiene un formato desconocido")


def save_state(state):
    state["legacy_ids"] = sorted(set(state.get("legacy_ids", [])))
    STATE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def send(webhook, mod, event):
    image_req = Request(mod["image"], headers={"User-Agent": USER_AGENT, "Referer": BASE})
    with urlopen(image_req, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "").split(";")[0]
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise RuntimeError(f"Imagen inválida para {mod['title'].strip()}: {content_type}")
        image_bytes = response.read(8_000_001)
    if len(image_bytes) > 8_000_000 or not image_bytes:
        raise RuntimeError(f"Imagen demasiado grande o vacía: {mod['title'].strip()}")

    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    filename = "modhub." + extension
    is_update = event == "UPDATE"
    heading = "🔄 ACTUALIZACIÓN" if is_update else "🆕 NUEVO MOD"
    footer = (
        "Actualizado en el ModHub oficial · Farming Simulator 25"
        if is_update else
        "Nuevo en el ModHub oficial · Farming Simulator 25"
    )
    description = html.unescape(mod["author"].strip())[:400]
    if mod.get("version"):
        description += f"\n**Versión:** {mod['version']}"
    description += f"\n**Plataforma:** {mod.get('platform', 'Plataforma no determinada')}"

    payload = {
        "username": "ModHub FS25",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "author": {"name": heading},
            "title": html.unescape(mod["title"].strip())[:256],
            "url": mod["url"],
            "description": description[:500],
            "color": 0xF0A000 if is_update else 0x82B440,
            "image": {"url": "attachment://" + filename},
            "footer": {"text": footer},
        }],
    }

    boundary = "modhub-" + uuid.uuid4().hex
    data = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        "Content-Type: application/json\r\n\r\n"
    ).encode() + json.dumps(payload).encode("utf-8") + (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()

    for attempt in range(3):
        try:
            req = Request(webhook, data=data, headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "User-Agent": USER_AGENT,
            })
            with urlopen(req, timeout=30):
                return
        except HTTPError as err:
            if err.code != 429 or attempt == 2:
                raise
            try:
                delay = float(json.loads(err.read())["retry_after"])
            except (ValueError, KeyError):
                delay = 5
            time.sleep(min(max(delay, 1), 30))


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError("Falta el secreto DISCORD_WEBHOOK_URL")

    mods = get_mods()
    state = load_state()
    known_ids = set(state.get("legacy_ids", [])) | set(state["mods"].keys())
    published = baselined = errors = official_updates = 0

    for mod in reversed(mods):
        mod_id = mod["id"]
        old = state["mods"].get(mod_id)
        was_known = mod_id in known_ids
        marker = mod.get("marker", "")
        try:
            version, platform = get_details(mod)
        except Exception as exc:
            print(f"Aviso: {exc}")
            errors += 1
            continue

        mod["version"] = version
        mod["platform"] = platform
        title = html.unescape(mod["title"].strip())

        if not was_known:
            send(webhook, mod, "NEW")
            state["mods"][mod_id] = {"version": version, "title": title, "last_marker": marker}
            state["legacy_ids"].append(mod_id)
            known_ids.add(mod_id)
            save_state(state)
            print(f"Publicado NUEVO: {title} v{version} ({mod_id})")
            published += 1
            time.sleep(1)
            continue

        if old is None or not old.get("version"):
            state["mods"][mod_id] = {"version": version, "title": title, "last_marker": marker}
            state["legacy_ids"].append(mod_id)
            save_state(state)
            print(f"Versión base registrada: {title} v{version} ({mod_id})")
            baselined += 1
            continue

        old_version = old.get("version", "")
        old_marker = old.get("last_marker", "")
        version_changed = version != old_version
        fresh_official_update = marker == "UPDATE" and old_marker != "UPDATE"

        if version_changed or fresh_official_update:
            send(webhook, mod, "UPDATE")
            state["mods"][mod_id] = {"version": version, "title": title, "last_marker": marker}
            save_state(state)
            reason = f"{old_version} -> {version}" if version_changed else "marcado UPDATE! por ModHub"
            print(f"Publicado UPDATE: {title} ({reason}) ({mod_id})")
            published += 1
            if fresh_official_update:
                official_updates += 1
            time.sleep(1)
        elif old.get("title") != title or old_marker != marker:
            state["mods"][mod_id] = {"version": version, "title": title, "last_marker": marker}
            save_state(state)

    state["format"] = 3
    save_state(state)
    if not published:
        print("No hay mods nuevos ni cambios de versión/estado UPDATE!.")
    print(
        f"Revisados {len(mods)} mods de hasta {MAX_PAGES} páginas · "
        f"publicados {published} · updates oficiales {official_updates} · "
        f"bases registradas {baselined} · avisos {errors}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
