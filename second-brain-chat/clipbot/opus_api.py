"""OpusClip API client (https://api.opus.pro/api, Bearer key). Shapes from OpusClip's official
MIT skill repo (github.com/opus-pro/opus-skills, references/api-reference.md), Sep 2026.

Costs to remember: 1 credit per source minute on project creation (10-credit minimum per API
project), 1 credit per X post, thumbnails 7 credits. Pro (Beta) API cap 900 credits / month.
"""
from __future__ import annotations

import math
import os
import time

import requests

BASE = "https://api.opus.pro/api"
TIMEOUT = 60


class RateLimited(Exception):
    pass


class OpusClient:
    def __init__(self, api_key: str | None = None, session=None):
        self.key = api_key or os.environ.get("OPUSCLIP_API_KEY")
        self.available = bool(self.key)
        self.s = session or requests.Session()
        if self.key:
            self.s.headers.update({"Authorization": f"Bearer {self.key}"})

    def _req(self, method: str, path: str, **kw):
        if not self.available:
            raise RuntimeError("OPUSCLIP_API_KEY not set")
        r = self.s.request(method, BASE + path, timeout=TIMEOUT, **kw)
        if r.status_code == 429:
            raise RateLimited(r.headers.get("Retry-After", "?"))
        r.raise_for_status()
        return r.json() if r.content else {}

    # ---- account -------------------------------------------------------------------------
    def usage(self) -> dict:
        """{'uncapped': bool, 'monthly': {used, limit, remaining, reset_at}, 'concurrent': {...}}"""
        return self._req("GET", "/api-usage", params={"q": "mine"})

    # ---- projects / clips ----------------------------------------------------------------
    def create_project(self, video_url: str, title: str = "", prompt: str = "", durations=None,
                       model: str = "ClipAnything", aspect: str = "portrait", brand_template_id: str = "",
                       webhook: str = "", genre: str = "", source_lang: str = "") -> dict:
        curation = {"model": model, "clipDurations": durations or [[15, 35], [30, 60]]}
        if prompt and model == "ClipAnything":
            curation["customPrompt"] = prompt
        if genre:
            curation["genre"] = genre
        body = {"videoUrl": video_url, "curationPref": curation,
                "renderPref": {"layoutAspectRatio": aspect}}
        if title:
            body["uploadedVideoAttr"] = {"title": title}
        if brand_template_id:
            body["brandTemplateId"] = brand_template_id
        if source_lang:
            body["importPreference"] = {"sourceLang": source_lang}
        if webhook:
            body["conclusionActions"] = [{"type": "WEBHOOK", "url": webhook, "notifyFailure": True}]
        return self._req("POST", "/clip-projects", json=body)

    def clips(self, project_id: str) -> list:
        d = self._req("GET", "/exportable-clips", params={"q": "findByProjectId", "projectId": project_id})
        return normalize_clips(d, project_id)

    def upload_local(self, path: str, log=print) -> str:
        """The 4-step resumable upload. Returns the uploadId to use as videoUrl."""
        step1 = self._req("POST", "/upload-links", json={"video": {"usecase": "LocalUpload"}})
        link = step1.get("data", step1)
        start = requests.post(link["url"], headers={"x-goog-resumable": "start", "Content-Length": "0"}, timeout=TIMEOUT)
        start.raise_for_status()
        location = start.headers["Location"]
        size = os.path.getsize(path)
        log(f"  uploading {os.path.basename(path)} ({size / 1e6:.0f} MB)")
        with open(path, "rb") as f:
            put = requests.put(location, data=f, headers={"Content-Type": "application/octet-stream",
                                                          "Content-Length": str(size)}, timeout=3600)
        put.raise_for_status()
        return link["uploadId"]

    def hd_urls_via_collection(self, project_id: str, clip_ids: list, name: str = "") -> dict:
        """{clip_id: hd_url}. HD exports come from a collection export (the clip list is preview-only,
        mirroring the web app where the HD link appears after Export)."""
        col = self._req("POST", "/collections", json={"collectionName": name or f"clipbot {project_id}"})
        col_id = (col.get("data") or col).get("collectionId")
        for cid in clip_ids:
            self._req("POST", "/collection-contents", json={"collectionId": col_id, "contentId": f"{project_id}.{cid}"})
        exp = self._req("POST", f"/collections/{col_id}/export", json={})
        out = {}
        for item in ((exp.get("data") or exp).get("contentList") or []):
            content_id, uri = item.get("contentId", ""), item.get("uriForExport", "")
            if content_id and uri:
                out[content_id.split(".", 1)[-1]] = uri
        return out

    def download(self, url: str, dest: str) -> str:
        with requests.get(url, stream=True, timeout=TIMEOUT) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return dest

    # ---- social posting (driver 2) -------------------------------------------------------
    def social_accounts(self) -> list:
        d = self._req("GET", "/social-accounts", params={"q": "mine"})
        return d.get("data", d) if isinstance(d, dict) else d

    def schedule(self, project_id: str, clip_id: str, post_account_id: str, publish_at_iso: str,
                 title: str, description: str, privacy: str = "public", sub_account_id: str | None = None,
                 media_type: str = "video") -> dict:
        body = {"projectId": project_id, "clipId": clip_id, "postAccountId": post_account_id,
                "publishAt": publish_at_iso,
                "postDetail": {"title": title, "mediaType": media_type,
                               "custom": {"description": description, "privacy": privacy}}}
        if sub_account_id:
            body["subAccountId"] = sub_account_id
        return self._req("POST", "/publish-schedules", json=body)

    def cancel_schedule(self, schedule_id: str) -> dict:
        return self._req("DELETE", f"/publish-schedules/{schedule_id}")


# ---- helpers -------------------------------------------------------------------------------
def estimate_credits(minutes: float) -> int:
    """1 credit per whole source minute, 10-credit minimum per API project."""
    return max(10, int(math.floor(minutes)))


def project_id_from(payload: dict) -> str | None:
    for node in (payload, payload.get("data") if isinstance(payload, dict) else None):
        if isinstance(node, dict):
            for k in ("projectId", "project_id", "id"):
                if node.get(k):
                    return str(node[k])
    return None


def _pick(row: dict, *keys, default=None):
    for k in keys:
        if isinstance(row, dict) and row.get(k) not in (None, ""):
            return row[k]
    return default


def normalize_clips(payload, project_id: str) -> list:
    """Flatten whatever shape /exportable-clips returns into a stable dict per clip."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data", payload)
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("clips") or rows.get("items") or []
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        clip_id = str(_pick(r, "clipId", "curationId", "id", default=""))
        full = _pick(r, "clipFullId", "contentId", default="")
        if not clip_id and full and "." in str(full):
            clip_id = str(full).split(".", 1)[1]
        out.append({
            "project_id": project_id,
            "clip_id": clip_id,
            "title": str(_pick(r, "title", "name", default="") or "")[:200],
            "transcript": str(_pick(r, "transcript", "text", default="") or ""),
            "score": _to_float(_pick(r, "score", "viralityScore", "virality_score", default=0)),
            "duration_s": _to_float(_pick(r, "duration", "durationSec", "duration_sec", default=0)),
            "preview_url": _pick(r, "previewUrl", "preview_url", "previewUri", default=""),
            "hd_url": _pick(r, "uriForExport", "exportUri", "exportUrl", "export_url", "downloadUrl", "hdUrl", default=""),
            "hashtags": _pick(r, "hashtags", default=[]) or [],
            "raw_keys": sorted(r.keys()),
        })
    return out


def _to_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def wait_for_clips(client: OpusClient, project_id: str, timeout_s: int = 3600, every_s: int = 60, log=print) -> list:
    """Poll until clips exist or the timeout passes (webhooks need a public URL; polling does not)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            clips = client.clips(project_id)
        except RateLimited as exc:
            log(f"  rate limited, retry-after {exc}")
            clips = []
        if clips:
            return clips
        time.sleep(every_s)
    return []
