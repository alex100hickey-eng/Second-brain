"""clipbot runner.

    python3 -m clipbot.runner status
    python3 -m clipbot.runner campaign add --name "Vyro MrBeast Sep" --marketplace vyro --rate 3 --cap 0 \
        --hashtags "#vyro #beast" --prompt "the funniest 20-40s moments with a clear payoff"
    python3 -m clipbot.runner campaign list
    python3 -m clipbot.runner ingest --campaign "Vyro MrBeast Sep" --url https://... --minutes 62
    python3 -m clipbot.runner ingest --campaign "Vyro MrBeast Sep" --file ~/Downloads/ep12.mp4
    python3 -m clipbot.runner process          # poll OpusClip, download, transform, stage (one pass)
    python3 -m clipbot.runner inbox            # ingest anything new in iCloud ClipBot/inbox/<campaign>/
    python3 -m clipbot.runner posted --variant 12 --url https://www.tiktok.com/@.../video/...
    python3 -m clipbot.runner views --variant 12 --views 4200 [--qualified 1600] [--approved 4.8]
    python3 -m clipbot.runner report
    python3 -m clipbot.runner hooks
    python3 -m clipbot.runner loop             # every 5 min: inbox + process; 17:00 ET: the ready nudge

Credits governor: an ingest is refused when this week's credits would pass `weekly_credit_budget`
or OpusClip's own /api-usage says the monthly cap cannot cover it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, hooks, notify, posting, transform
from .ledger import Ledger
from .opus_api import OpusClient, estimate_credits, normalize_clips, project_id_from

ET = ZoneInfo("America/New_York")


def can_spend(ledger: Ledger, cfg: config.Config, credits: int, usage: dict | None) -> tuple:
    """(ok, reason) for spending `credits` now."""
    if config.kill_switch_on():
        return False, "kill switch on"
    week = ledger.credits_this_week()
    if week + credits > cfg.weekly_credit_budget:
        return False, f"weekly budget: {week}+{credits} > {cfg.weekly_credit_budget}"
    if usage and not usage.get("uncapped"):
        remaining = (usage.get("monthly") or {}).get("remaining")
        if remaining is not None and credits > remaining:
            return False, f"OpusClip monthly cap: {credits} > {remaining} remaining"
    return True, "ok"


class Runner:
    def __init__(self, cfg: config.Config | None = None, ledger: Ledger | None = None, client=None, log=print):
        self.cfg = cfg or config.load()
        self.ledger = ledger or Ledger()
        self.client = client or OpusClient()
        self.log = log
        config.ensure_dirs()

    # ---- campaigns -----------------------------------------------------------------------
    def add_campaign(self, name, marketplace="", rate=0.0, cap=0.0, hashtags="", prompt="", platforms="", notes="") -> int:
        cid = self.ledger.add_campaign(name, marketplace, rate, cap, hashtags, prompt, platforms, notes)
        self.log(f"campaign #{cid} {name} ({marketplace}) ${rate}/1k cap ${cap}")
        return cid

    # ---- ingest ----------------------------------------------------------------------------
    def ingest(self, campaign_ref, url: str = "", path: str = "", minutes: float = 0.0, title: str = "") -> int | None:
        camp = self.ledger.campaign(campaign_ref)
        if not camp:
            self.log(f"no campaign {campaign_ref!r}; add it first")
            return None
        locator = url or path
        if self.ledger.source_by_locator(locator):
            self.log(f"already ingested: {locator}")
            return None
        if path:
            minutes = minutes or transform.probe_duration(path) / 60.0
            title = title or os.path.splitext(os.path.basename(path))[0]
        if not minutes:
            self.log("URL ingest needs --minutes (credits are charged per source minute)")
            return None
        credits = estimate_credits(minutes)
        usage = None
        if self.client.available:
            try:
                usage = self.client.usage()
            except Exception as exc:
                self.log(f"  usage check failed: {exc}")
        ok, why = can_spend(self.ledger, self.cfg, credits, usage)
        sid = self.ledger.add_source(camp["id"], locator, title, minutes, credits)
        if not ok:
            self.ledger.update_source(sid, status="queued", error=why)
            self.log(f"queued source #{sid} ({credits} credits) — not submitted: {why}")
            return sid
        if not self.client.available:
            self.ledger.update_source(sid, status="queued", error="OPUSCLIP_API_KEY not set")
            self.log(f"queued source #{sid} ({credits} credits) — waiting for OPUSCLIP_API_KEY")
            return sid
        return self._submit(sid, camp, locator, path, title, credits)

    def _submit(self, sid, camp, locator, path, title, credits) -> int:
        try:
            video_url = self.client.upload_local(path, self.log) if path else locator
            platforms = camp["platforms"] or ""
            resp = self.client.create_project(video_url, title=title, prompt=camp["prompt"] or "",
                                              durations=self.cfg.clip_durations, model=self.cfg.model,
                                              aspect=self.cfg.aspect, brand_template_id=self.cfg.brand_template_id)
            pid = project_id_from(resp)
            if not pid:
                raise RuntimeError(f"no project id in response keys {list(resp)[:8]}")
            self.ledger.update_source(sid, status="submitted", opus_project_id=pid, error="")
            self.ledger.record_credits(credits, f"source {sid} project {pid}")
            self.log(f"submitted source #{sid} → OpusClip project {pid} ({credits} credits) [{platforms or 'default platforms'}]")
        except Exception as exc:
            self.ledger.update_source(sid, status="failed", error=str(exc)[:300])
            self.log(f"submit failed for source #{sid}: {exc}")
        return sid

    def submit_queued(self) -> int:
        n = 0
        for s in self.ledger.sources("queued"):
            if not self.client.available:
                break
            camp = self.ledger.campaign(s["campaign_id"])
            try:
                usage = self.client.usage()
            except Exception:
                usage = None
            ok, why = can_spend(self.ledger, self.cfg, s["credits_est"], usage)
            if not ok:
                self.ledger.update_source(s["id"], error=why)
                continue
            path = s["locator"] if os.path.exists(s["locator"]) else ""
            self._submit(s["id"], camp, s["locator"], path, s["title"], s["credits_est"])
            n += 1
        return n

    def ingest_inbox(self) -> int:
        """iCloud Drive ClipBot/inbox/<campaign name>/<video> → ingest under that campaign."""
        n = 0
        if not os.path.isdir(config.INBOX_DIR):
            return 0
        for cname in sorted(os.listdir(config.INBOX_DIR)):
            cdir = os.path.join(config.INBOX_DIR, cname)
            if not os.path.isdir(cdir) or cname.startswith("."):
                continue
            camp = self.ledger.campaign(cname)
            if not camp:
                cid = self.ledger.add_campaign(cname, notes="auto-created from inbox folder; set rate/hashtags/prompt")
                camp = self.ledger.campaign(cid)
                self.log(f"created campaign '{cname}' from inbox folder — set its rate, tags and prompt")
            for fname in sorted(os.listdir(cdir)):
                fpath = os.path.join(cdir, fname)
                if not fname.lower().endswith(config.VIDEO_EXT) or fname.startswith("."):
                    continue
                if self.ledger.source_by_locator(fpath):
                    continue
                if time.time() - os.path.getmtime(fpath) < 120:
                    continue  # still syncing / being written
                if self.ingest(camp["id"], path=fpath) is not None:
                    n += 1
        return n

    # ---- process: poll → download → transform → stage --------------------------------------
    def poll_submitted(self) -> int:
        n = 0
        for s in self.ledger.sources("submitted"):
            if not self.client.available:
                break
            try:
                raw = self.client.clips(s["opus_project_id"])
            except Exception as exc:
                self.log(f"  poll {s['opus_project_id']}: {exc}")
                continue
            if not raw:
                continue
            kept = sorted((c for c in raw if c["score"] >= self.cfg.min_score), key=lambda c: -c["score"])
            kept = kept[: self.cfg.max_clips_per_source]
            if kept and not all(c["hd_url"] for c in kept):
                try:
                    hd = self.client.hd_urls_via_collection(s["opus_project_id"], [c["clip_id"] for c in kept])
                    for c in kept:
                        c["hd_url"] = c["hd_url"] or hd.get(c["clip_id"], "")
                except Exception as exc:
                    self.log(f"  HD export via collection failed for {s['opus_project_id']}: {exc}")
            for c in kept:
                self.ledger.add_clip(s["id"], c)
            self.ledger.update_source(s["id"], status="clipped")
            self.log(f"source #{s['id']}: {len(raw)} clips from OpusClip, kept {len(kept)} "
                     f"(keys seen: {raw[0]['raw_keys'][:10]})")
            n += 1
        return n

    def download_new(self) -> int:
        n = 0
        for c in self.ledger.clips("new"):
            if not c["hd_url"]:
                if c["preview_url"]:
                    self.log(f"  clip #{c['id']} has no HD url yet; using preview")
                    url = c["preview_url"]
                else:
                    continue
            else:
                url = c["hd_url"]
            dest = os.path.join(config.HOME, "hd", f"{c['id']:05d}_{posting.slug(c['title'])}.mp4")
            try:
                self.client.download(url, dest)
                dur = transform.probe_duration(dest)
                self.ledger.update_clip(c["id"], local_path=dest, status="downloaded", duration_s=dur)
                n += 1
            except Exception as exc:
                self.ledger.update_clip(c["id"], status="failed")
                self.log(f"  download failed clip #{c['id']}: {exc}")
        return n

    def transform_downloaded(self) -> int:
        n = 0
        lib = hooks.library()
        uses = self.ledger.hook_uses()
        if not lib:
            self.log("  no voice hooks in ClipBot/hooks — clips get the text hook only (weaker on originality)")
        for c in self.ledger.clips("downloaded"):
            src = self.ledger.clip(c["id"])["local_path"]
            if not src or not os.path.exists(src):
                self.ledger.update_clip(c["id"], status="failed")
                continue
            camp_platforms = self._platforms_for_clip(c)
            used_here = set()
            made = 0
            for platform in camp_platforms:
                recipe = config.VARIANTS.get(platform, config.VARIANTS["tiktok"])
                hook = hooks.pick(lib, uses, exclude=used_here) if lib else None
                hook_len = hooks.hook_length(hook, self.cfg.hook_seconds_max) if hook else 0.0
                text_hook = c["title"] or (hook["text"] if hook else "Watch this")
                dst = os.path.join(config.HOME, "variants", f"{c['id']:05d}_{platform}.mp4")
                try:
                    transform.make_variant(src, dst, text_hook, recipe, hook["file"] if hook else None, hook_len,
                                           self.cfg.text_hook_seconds)
                except Exception as exc:
                    self.log(f"  transform failed clip #{c['id']} {platform}: {exc}")
                    continue
                self.ledger.add_variant(c["id"], platform, dst, hook["name"] if hook else "", text_hook)
                if hook:
                    used_here.add(hook["name"])
                    uses[hook["name"]] = uses.get(hook["name"], 0) + 1
                    self.ledger.bump_hook(hook["name"])
                made += 1
            self.ledger.update_clip(c["id"], status="transformed" if made else "failed")
            n += made
        return n

    def _platforms_for_clip(self, clip) -> list:
        src = next((s for s in self.ledger.sources() if s["id"] == clip["source_id"]), None)
        camp = self.ledger.campaign(src["campaign_id"]) if src else None
        if camp and camp.get("platforms"):
            return [p.strip() for p in camp["platforms"].split(",") if p.strip() in config.VARIANTS]
        return [p for p in self.cfg.platforms if p in config.VARIANTS]

    def stage_made(self) -> int:
        n = 0
        for v in self.ledger.variants("made"):
            clip = self.ledger.clip(v["clip_id"])
            src = next((s for s in self.ledger.sources() if s["id"] == clip["source_id"]), {})
            camp = self.ledger.campaign(src.get("campaign_id")) or {}
            title, body = posting.build_caption(v["platform"], clip, camp, v["text_hook"])
            if self.cfg.poster == "opus":
                self.log(f"  opus poster not wired for variant #{v['id']} (needs connected accounts); staging to folder instead")
            try:
                text = posting.caption_file_text(v["platform"], title, body, camp, clip, v["id"])
                dest = posting.stage_folder(v["path"], v["platform"], title, text, v["id"])
                self.ledger.update_variant(v["id"], staged_path=dest, status="staged")
                n += 1
            except Exception as exc:
                self.log(f"  stage failed variant #{v['id']}: {exc}")
        return n

    def process(self) -> dict:
        counts = {"submitted": self.submit_queued(), "clipped": self.poll_submitted(),
                  "downloaded": self.download_new(), "variants": self.transform_downloaded(),
                  "staged": self.stage_made()}
        self.log(f"process: {counts}")
        return counts

    # ---- Alex's two commands ----------------------------------------------------------------
    def posted(self, variant_id: int, url: str) -> None:
        self.ledger.mark_posted(variant_id, url)
        v = self.ledger.variant(variant_id)
        for ext in (".mp4", ".txt"):
            p = (v["staged_path"] or "").replace(".mp4", ext)
            if p and os.path.exists(p):
                posted_dir = os.path.join(config.READY_DIR, "_posted")
                os.makedirs(posted_dir, exist_ok=True)
                os.replace(p, os.path.join(posted_dir, os.path.basename(p)))
        self.log(f"variant #{variant_id} posted: {url}")

    def views(self, variant_id: int, views: int, qualified: int | None = None, approved: float | None = None,
              settled: float | None = None) -> None:
        fields = {"views": views}
        if qualified is not None:
            fields["qualified_views"] = qualified
        if approved is not None:
            fields["usd_approved"] = approved
        if settled is not None:
            fields["usd_settled"] = settled
        self.ledger.update_post(variant_id, **fields)

    # ---- nudge / status ---------------------------------------------------------------------
    def ready_nudge(self) -> bool:
        staged = self.ledger.variants("staged")
        last_seen = self.ledger.get_kv("last_nudged_variant", 0)
        fresh = [v for v in staged if v["id"] > last_seen]
        if not fresh:
            return False
        by = {}
        for v in fresh:
            by[v["platform"]] = by.get(v["platform"], 0) + 1
        parts = ", ".join(f"{n} {p}" for p, n in sorted(by.items()))
        ok = notify.nudge(f"{len(fresh)} clips ready to post",
                          f"Files → ClipBot → ready: {parts}. Windows in each caption file. "
                          f"{len(staged)} staged in total.", log=self.log)
        if ok:
            self.ledger.set_kv("last_nudged_variant", max(v["id"] for v in fresh))
        return ok

    def status(self) -> str:
        lines = [f"clipbot — poster={self.cfg.poster} · OpusClip key: {'present' if self.client.available else 'MISSING'} · "
                 f"ffmpeg: {'ok' if transform.have_ffmpeg() else 'MISSING'} · kill: {'ON' if config.kill_switch_on() else 'off'}",
                 f"  ready dir: {config.READY_DIR}", f"  inbox dir: {config.INBOX_DIR}",
                 f"  hooks: {len(hooks.library())} recorded lines in {config.HOOKS_DIR}",
                 f"  weekly credits {self.ledger.credits_this_week()}/{self.cfg.weekly_credit_budget}"]
        if self.client.available:
            try:
                u = self.client.usage()
                m = u.get("monthly") or {}
                lines.append(f"  OpusClip API: used {m.get('used')} / {m.get('limit')} · remaining {m.get('remaining')} · resets {str(m.get('reset_at'))[:10]}")
            except Exception as exc:
                lines.append(f"  OpusClip API usage check failed: {exc}")
        lines.append(self.ledger.report())
        return "\n".join(lines)

    # ---- the loop -----------------------------------------------------------------------------
    def loop(self):
        self.log("clipbot loop started")
        last_tick, last_nudge_day = 0.0, ""
        while True:
            now = datetime.now(ET)
            try:
                if time.time() - last_tick >= self.cfg.poll_minutes * 60:
                    last_tick = time.time()
                    if config.kill_switch_on():
                        self.log("kill switch on — idle")
                    else:
                        self.ingest_inbox()
                        self.process()
                day = now.strftime("%Y-%m-%d")
                if now.hour == self.cfg.nudge_hour_et and last_nudge_day != day:
                    last_nudge_day = day
                    self.ready_nudge()
                    with open(os.path.join(config.ROOT, "report-latest.txt"), "w") as f:
                        f.write(self.ledger.report() + "\n")
            except Exception as exc:
                self.log(f"loop error: {exc}\n{traceback.format_exc(limit=3)}")
            time.sleep(30)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="clipbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("process")
    sub.add_parser("inbox")
    sub.add_parser("report")
    sub.add_parser("hooks")
    sub.add_parser("loop")
    sub.add_parser("nudge")
    c = sub.add_parser("campaign")
    c.add_argument("verb", choices=["add", "list"])
    c.add_argument("--name")
    c.add_argument("--marketplace", default="")
    c.add_argument("--rate", type=float, default=0.0)
    c.add_argument("--cap", type=float, default=0.0)
    c.add_argument("--hashtags", default="")
    c.add_argument("--prompt", default="")
    c.add_argument("--platforms", default="")
    c.add_argument("--notes", default="")
    i = sub.add_parser("ingest")
    i.add_argument("--campaign", required=True)
    i.add_argument("--url", default="")
    i.add_argument("--file", default="")
    i.add_argument("--minutes", type=float, default=0.0)
    i.add_argument("--title", default="")
    p = sub.add_parser("posted")
    p.add_argument("--variant", type=int, required=True)
    p.add_argument("--url", required=True)
    v = sub.add_parser("views")
    v.add_argument("--variant", type=int, required=True)
    v.add_argument("--views", type=int, required=True)
    v.add_argument("--qualified", type=int)
    v.add_argument("--approved", type=float)
    v.add_argument("--settled", type=float)
    a = ap.parse_args(argv)
    r = Runner()
    if a.cmd == "status":
        print(r.status())
    elif a.cmd == "campaign" and a.verb == "add":
        if not a.name:
            sys.exit("--name required")
        r.add_campaign(a.name, a.marketplace, a.rate, a.cap, a.hashtags, a.prompt, a.platforms, a.notes)
    elif a.cmd == "campaign":
        for k in r.ledger.campaigns():
            print(f"#{k['id']} {k['name']} ({k['marketplace']}) ${k['rate_per_1k']}/1k cap ${k['cap_per_clip']} tags '{k['hashtags']}' platforms '{k['platforms'] or 'default'}'")
    elif a.cmd == "ingest":
        r.ingest(a.campaign, url=a.url, path=os.path.expanduser(a.file) if a.file else "", minutes=a.minutes, title=a.title)
    elif a.cmd == "process":
        r.process()
    elif a.cmd == "inbox":
        print(f"{r.ingest_inbox()} new source(s) from inbox")
    elif a.cmd == "posted":
        r.posted(a.variant, a.url)
    elif a.cmd == "views":
        r.views(a.variant, a.views, a.qualified, a.approved, a.settled)
    elif a.cmd == "report":
        print(r.ledger.report())
    elif a.cmd == "hooks":
        for h in hooks.library():
            print(f"{h['name']:<40} {h['text']}")
    elif a.cmd == "nudge":
        print("sent" if r.ready_nudge() else "nothing new to nudge")
    elif a.cmd == "loop":
        r.loop()


if __name__ == "__main__":
    sys.exit(main())
