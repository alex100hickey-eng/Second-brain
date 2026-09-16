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
import json
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


def parse_urls_file(path: str) -> list:
    """`urls.txt` lines: `<url> <minutes>` (minutes required: credits are charged per source minute)."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("http"):
                    try:
                        out.append((parts[0], float(parts[1])))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


HOOK_SCRIPT = """RECORD THESE (Voice Memos → share → Save to Files → this folder). One line per file, 2-4 seconds,
energy up, no music. Name the file like the line: wait_for_this_part.m4a. Delete this file when done.

wait for this part
nobody talks about this
this is the moment
watch what happens next
you need to hear this
this changed my mind
the ending is crazy
listen to this
this is actually insane
hold on for the end
this is the part
you will not believe this
I had to clip this
this one is different
pay attention here
this is why
here is the thing
wait until the end
this is wild
he actually said this
this is the best part
watch his reaction
this is too good
I keep coming back to this
this is the one
you have to see this
this got me
this is important
do not skip this
this is the answer
"""


def write_hook_script(hooks_dir: str = config.HOOKS_DIR) -> str | None:
    """Drop a RECORD_THESE.txt into the hooks folder if it is empty, so Alex knows what to record."""
    try:
        os.makedirs(hooks_dir, exist_ok=True)
        has_audio = any(n.lower().endswith(config.AUDIO_EXT) for n in os.listdir(hooks_dir))
        path = os.path.join(hooks_dir, "RECORD_THESE.txt")
        if not has_audio and not os.path.exists(path):
            with open(path, "w") as f:
                f.write(HOOK_SCRIPT)
            return path
    except OSError:
        pass
    return None


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



def _beat(name: str, stale_after_s: int, note: str = "") -> None:
    """Report liveness into the SHARED store so the always-on server can see this Mac loop.

    Silent death is this system's signature failure — polybot slept through a DNS blackout,
    clipbot sat on 187 finished clips for two days, and nothing anywhere noticed. A loop that
    cannot be seen from the server is a loop nobody is watching."""
    try:
        import os, sys
        sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake, monitor
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        monitor.supabase = intake.supabase
        monitor.beat(name, stale_after_s, note)
    except Exception:
        pass        # a heartbeat must never break the work it reports on



def _publish(lane: str, facts: dict) -> None:
    """Push this lane's money-relevant numbers into the SHARED store.

    Liveness is not the same as health: polybot can beat happily while every module loses, and
    clipbot beat for two days while 187 finished clips went nowhere. The server cannot read this
    Mac's sqlite, so the scoreboard has to travel."""
    try:
        import os, sys
        sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        st = intake._load_state(f"business:{lane}")
        st.update(facts)
        st["key"] = f"business:{lane}"
        st["at"] = __import__("datetime").datetime.now().isoformat()
        intake._save_state(st)
    except Exception:
        pass


class Runner:
    def __init__(self, cfg: config.Config | None = None, ledger: Ledger | None = None, client=None, log=print):
        self.cfg = cfg or config.load()
        self.ledger = ledger or Ledger()
        self.client = client or OpusClient()
        self.log = log
        config.ensure_dirs()
        if write_hook_script():
            self.log(f"  wrote RECORD_THESE.txt into {config.HOOKS_DIR}")

    # ---- campaigns -----------------------------------------------------------------------
    def add_campaign(self, name, marketplace="", rate=0.0, cap=0.0, hashtags="", prompt="", platforms="", notes="",
                     rules=None) -> int:
        cid = self.ledger.add_campaign(name, marketplace, rate, cap, hashtags, prompt, platforms, notes, rules)
        self.log(f"campaign #{cid} {name} ({marketplace}) ${rate}/1k cap ${cap}"
                 + (f" rules {json.dumps(rules)}" if rules else ""))
        return cid

    def _campaign_for_clip(self, clip) -> dict | None:
        src = next((s for s in self.ledger.sources() if s["id"] == clip["source_id"]), None)
        return self.ledger.campaign(src["campaign_id"]) if src else None

    # ---- ingest ----------------------------------------------------------------------------
    def ingest(self, campaign_ref, url: str = "", path: str = "", minutes: float = 0.0, title: str = "",
               direct: bool = False) -> int | None:
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
        if direct:
            if not path:
                self.log("direct ingest needs a local --file (a pre-cut clip, used as-is; no OpusClip, 0 credits)")
                return None
            return self._ingest_direct(camp, path, title, minutes)
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
        # Claim it before the (minutes-long) upload, or the loop's queue sweep submits it a second time.
        self.ledger.update_source(sid, status="submitting", error="")
        return self._submit(sid, camp, locator, path, title, credits)

    def _ingest_direct(self, camp, path: str, title: str, minutes: float) -> int:
        """A pre-cut clip (Vyro clip banks ship these): register source+clip as already downloaded, 0 credits."""
        sid = self.ledger.add_source(camp["id"], path, title, minutes, 0)
        self.ledger.update_source(sid, status="clipped", error="")
        dur = transform.probe_duration(path)
        cid = self.ledger.add_clip(sid, {"clip_id": f"direct-{sid}", "title": title, "score": 0, "duration_s": dur})
        self.ledger.update_clip(cid, local_path=path, status="downloaded", duration_s=dur)
        self.log(f"direct source #{sid} → clip #{cid} ({dur:.0f}s, 0 credits) [{camp['name']}]")
        return sid

    def _submit(self, sid, camp, locator, path, title, credits) -> int:
        try:
            video_url = self.client.upload_local(path, self.log) if path else locator
            platforms = camp["platforms"] or ""
            rules = self.ledger.rules(camp)
            durations = rules["durations"] or self.cfg.clip_durations
            if not rules["durations"] and (rules["min_seconds"] or rules["max_seconds"]):
                lo = float(rules["min_seconds"] or 0)
                hi = float(rules["max_seconds"] or 0) or max(lo + 30, 90)
                durations = [[lo, hi]]
            resp = self.client.create_project(video_url, title=title, prompt=camp["prompt"] or "",
                                              durations=durations, model=self.cfg.model, aspect=self.cfg.aspect,
                                              brand_template_id=rules["brand_template_id"] or self.cfg.brand_template_id)
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
        for s in self.ledger.sources("submitting"):          # an upload that died mid-way (crash, reboot)
            if time.time() - float(s.get("updated") or 0) > 3 * 3600:
                self.ledger.update_source(s["id"], status="queued", error="upload did not finish; retrying")
                self.log(f"  source #{s['id']} stuck in submitting for 3h+ — back to queued")
        for s in self.ledger.sources("queued"):
            if not self.client.available:
                break
            camp = self.ledger.campaign(s["campaign_id"])
            self.ledger.update_source(s["id"], status="submitting", error="")
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
                if fname.startswith("."):
                    continue
                if fname.lower() == "urls.txt":
                    for url, minutes in parse_urls_file(fpath):
                        if not self.ledger.source_by_locator(url) and self.ingest(camp["id"], url=url, minutes=minutes) is not None:
                            n += 1
                    continue
                if not fname.lower().endswith(config.VIDEO_EXT):
                    continue
                if self.ledger.source_by_locator(fpath):
                    continue
                if time.time() - os.path.getmtime(fpath) < 120:
                    continue  # still syncing / being written
                if self.ingest(camp["id"], path=fpath, direct=bool(self.ledger.rules(camp)["direct"])) is not None:
                    n += 1
        return n

    def prune(self, keep_days: int = 14) -> int:
        """Delete HD sources and variants older than keep_days whose variants are posted or skipped."""
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for c in self.ledger.clips():
            if c["created"] > cutoff or not c["local_path"]:
                continue
            vs = self.ledger.variants(clip_id=c["id"])
            if vs and all(v["status"] in ("posted", "skipped") for v in vs):
                for p in [c["local_path"]] + [v["path"] for v in vs]:
                    if p and os.path.exists(p):
                        os.remove(p)
                        removed += 1
        return removed

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

    def refresh_urlless(self, max_tries: int = 6) -> int:
        """OpusClip lists some clips before their export exists (no HD or preview url, 0 s). Re-poll the
        project and the collection export a bounded number of times, then mark them skipped so they stop
        sitting in 'new' forever."""
        pending = [c for c in self.ledger.clips("new") if not c["hd_url"] and not c["preview_url"]]
        if not pending or not self.client.available:
            return 0
        fixed = 0
        by_src = {}
        for c in pending:
            by_src.setdefault(c["source_id"], []).append(c)
        for sid, cs in by_src.items():
            src = next((x for x in self.ledger.sources() if x["id"] == sid), None)
            pid = (src or {}).get("opus_project_id")
            if not pid:
                continue
            tries = int(self.ledger.get_kv(f"urlfix:{sid}", 0) or 0) + 1
            self.ledger.set_kv(f"urlfix:{sid}", tries)
            found = {}
            try:
                found = {c["clip_id"]: c for c in self.client.clips(pid)}
            except Exception as exc:
                self.log(f"  re-poll {pid}: {exc}")
            missing = []
            for c in cs:
                fresh = found.get(c["opus_clip_id"]) or {}
                if fresh.get("hd_url") or fresh.get("preview_url"):
                    self.ledger.update_clip(c["id"], hd_url=fresh.get("hd_url") or "", preview_url=fresh.get("preview_url") or "",
                                            duration_s=fresh.get("duration_s") or c["duration_s"],
                                            score=fresh.get("score") or c["score"])
                    fixed += 1
                else:
                    missing.append(c)
            if missing and tries <= max_tries:
                try:
                    hd = self.client.hd_urls_via_collection(pid, [c["opus_clip_id"] for c in missing],
                                                            name=f"clipbot {pid} retry {tries}")
                except Exception as exc:
                    hd = {}
                    self.log(f"  HD export retry via collection failed for {pid}: {exc}")
                for c in missing:
                    if hd.get(c["opus_clip_id"]):
                        self.ledger.update_clip(c["id"], hd_url=hd[c["opus_clip_id"]])
                        fixed += 1
            elif missing:
                for c in missing:
                    self.ledger.update_clip(c["id"], status="skipped")
                self.log(f"  gave up on {len(missing)} url-less clip(s) from source #{sid} after {tries} re-polls")
        if fixed:
            self.log(f"  re-poll filled urls for {fixed} clip(s)")
        return fixed

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
            camp = self._campaign_for_clip(c)
            rules = self.ledger.rules(camp)
            dur = float(c.get("duration_s") or 0)
            if dur and ((rules["min_seconds"] and dur < float(rules["min_seconds"]))
                        or (rules["max_seconds"] and dur > float(rules["max_seconds"]))):
                self.ledger.update_clip(c["id"], status="skipped")
                self.log(f"  skipped clip #{c['id']}: {dur:.0f}s outside the brief's "
                         f"{rules['min_seconds'] or 0:.0f}–{rules['max_seconds'] or '∞'}s window")
                continue
            camp_platforms = self._platforms_for_clip(c)
            used_here = set()
            made = 0
            for platform in camp_platforms:
                recipe = config.VARIANTS.get(platform, config.VARIANTS["tiktok"])
                hook = hooks.pick(lib, uses, exclude=used_here) if (lib and rules["voice"]) else None
                hook_len = hooks.hook_length(hook, self.cfg.hook_seconds_max) if hook else 0.0
                text_hook = (c["title"] or (hook["text"] if hook else "Watch this")) if rules["text_hook"] else ""
                if rules["text_hook"] and rules["hook_lines"]:     # brief-approved lines beat OpusClip's clickbait titles
                    text_hook = rules["hook_lines"][(c["id"] + made) % len(rules["hook_lines"])]
                    self.ledger.update_clip(c["id"], title=text_hook)
                dst = os.path.join(config.HOME, "variants", f"{c['id']:05d}_{platform}.mp4")
                try:
                    transform.make_variant(src, dst, transform.plain_text(text_hook), recipe,
                                           hook["file"] if hook else None, hook_len, self.cfg.text_hook_seconds)
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
        camp = self._campaign_for_clip(clip)
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
                  "refreshed": self.refresh_urlless(),
                  "downloaded": self.download_new(), "variants": self.transform_downloaded(),
                  "staged": self.stage_made()}
        if counts["staged"]:
            posting.write_post_order(self.ledger)
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
    def transformation_risk(self, campaign) -> str:
        """Does this campaign's brief force us to post content the platforms suppress?

        2026-09-15 diagnosis: @wildest_moments went to zero reach — 247 views across 13 posts,
        unchanged for 24 h, and YouTube deleted all five Shorts outright. TikTok's rule is explicit
        that "reposted, duplicate or unoriginal content" is not shown broadly in the For You feed,
        and the zero-view threshold (3+ consecutive posts 24 h apart) was met.

        The Vyro TV/film briefs REQUIRE that: The Shards said no added text or subtitles, FX Adults
        said no audio changes. Obeying the brief and keeping reach are mutually exclusive there.
        Battlbox, which allows recuts, hooks and captions, is the compatible shape. So a campaign
        that forbids every form of transformation is an account-risk, not just a low payer."""
        r = self.ledger.rules(campaign)
        blocked = []
        if r.get("voice") is False:
            blocked.append("no voiceover")
        if r.get("text_hook") is False:
            blocked.append("no text card")
        if r.get("direct"):
            blocked.append("pre-cut bank, no re-cut")
        if len(blocked) >= 2:
            return ("posts would be near-untransformed (" + ", ".join(blocked)
                    + ") — that is what platforms suppress as unoriginal")
        return ""


    def ready_nudge(self) -> bool:
        staged = self.ledger.variants("staged")
        last_seen = self.ledger.get_kv("last_nudged_variant", 0)
        fresh = [v for v in staged if v["id"] > last_seen]
        posting.write_post_order(self.ledger)
        if not fresh:
            # A staged backlog that nobody is posting is the failure mode this nudge exists to catch:
            # between 2026-09-12 and 09-14, 187 clips sat ready, nothing posted, and because no NEW
            # variant had been staged this returned False every day — silence that read as "all fine".
            return self._stalled_nudge(staged)
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

    def _stalled_nudge(self, staged) -> bool:
        """Nothing new staged, but is the pipeline actually moving? If a backlog is sitting there and
        nothing has gone out in 24 h, say so and name what is blocking it — a nudge that doesn't carry
        the next action is the same as no nudge."""
        if not staged:
            return False
        posts = self.ledger.posts()
        last_post = max((p.get("posted_at") or 0) for p in posts) if posts else 0
        idle_h = (time.time() - last_post) / 3600 if last_post else 999
        if idle_h < 24:
            return False
        day = datetime.now(ET).strftime("%Y-%m-%d")
        if self.ledger.get_kv("last_stalled_nudge_day", "") == day:
            return False
        blockers = json.loads(self.ledger.get_kv("blockers", "[]") or "[]")
        body = f"{len(staged)} clips staged and nothing posted in {int(idle_h)} h."
        if blockers:
            body += " Blocked on: " + "; ".join(blockers)
        ok = notify.nudge("clipbot is stalled", body, key="clipbot-stalled", log=self.log)
        if ok:
            self.ledger.set_kv("last_stalled_nudge_day", day)
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
            _beat("clipbot", 3 * 3600, "loop alive")
            try:
                st = self.ledger.stats()
                risky = [c["name"] for c in self.ledger.campaigns()
                         if self.transformation_risk(c)]
                _publish("clipbot", {"stats": st, "risky_campaigns": risky,
                                     "credits_week": self.ledger.credits_this_week()})
            except Exception:
                pass
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
    sub.add_parser("plan")
    pr = sub.add_parser("prune")
    pr.add_argument("--keep-days", type=int, default=14)
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
    c.add_argument("--no-voice", action="store_true", help="brief forbids changing the audio: no voice hook")
    c.add_argument("--no-text-hook", action="store_true", help="brief forbids added on-screen text")
    c.add_argument("--no-extra-tags", action="store_true", help="only the campaign's hashtags, none from the clip")
    c.add_argument("--caption", default="", help="mandatory caption line, verbatim from the brief")
    c.add_argument("--tag", default="", help="account to tag in the caption, e.g. @adultsfx")
    c.add_argument("--min-seconds", type=float, default=0.0)
    c.add_argument("--max-seconds", type=float, default=0.0)
    c.add_argument("--brand-template", default="", help="OpusClip brand template id for this campaign")
    c.add_argument("--direct", action="store_true", help="inbox files are pre-cut clips: no OpusClip, 0 credits")
    c.add_argument("--hook-lines", default="", help="brief-approved lines, '|'-separated; rotate as card + caption opener")
    c.add_argument("--ends", default="", help="campaign end date YYYY-MM-DD (post order puts the soonest first)")
    c.add_argument("--per-day", type=int, default=0, help="clips per day for this campaign in the post order (default 3)")
    i = sub.add_parser("ingest")
    i.add_argument("--campaign", required=True)
    i.add_argument("--url", default="")
    i.add_argument("--file", default="")
    i.add_argument("--minutes", type=float, default=0.0)
    i.add_argument("--title", default="")
    i.add_argument("--direct", action="store_true", help="pre-cut clip: skip OpusClip, transform + stage as-is")
    p = sub.add_parser("posted")
    p.add_argument("--variant", type=int, required=True)
    p.add_argument("--url", required=True)
    b = sub.add_parser("blockers", help="what is stopping posting; carried in the stalled nudge")
    b.add_argument("--set", nargs="*", default=None, help="replace the list (no args clears it)")
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
        rules = {}
        if a.no_voice:
            rules["voice"] = False
        if a.no_text_hook:
            rules["text_hook"] = False
        if a.no_extra_tags:
            rules["extra_tags"] = False
        if a.caption:
            rules["caption"] = a.caption
        if a.tag:
            rules["tag"] = a.tag
        if a.min_seconds:
            rules["min_seconds"] = a.min_seconds
        if a.max_seconds:
            rules["max_seconds"] = a.max_seconds
        if a.brand_template:
            rules["brand_template_id"] = a.brand_template
        if a.direct:
            rules["direct"] = True
        if a.hook_lines:
            rules["hook_lines"] = [x.strip() for x in a.hook_lines.split("|") if x.strip()]
        if a.ends:
            rules["ends"] = a.ends
        if a.per_day:
            rules["per_day"] = a.per_day
        r.add_campaign(a.name, a.marketplace, a.rate, a.cap, a.hashtags, a.prompt, a.platforms, a.notes, rules)
    elif a.cmd == "campaign":
        for k in r.ledger.campaigns():
            rl = {kk: v for kk, v in config.campaign_rules(k).items() if v != config.DEFAULT_RULES[kk]}
            print(f"#{k['id']} {k['name']} ({k['marketplace']}) ${k['rate_per_1k']}/1k cap ${k['cap_per_clip']} "
                  f"tags '{k['hashtags']}' platforms '{k['platforms'] or 'default'}'" + (f" rules {rl}" if rl else ""))
    elif a.cmd == "ingest":
        r.ingest(a.campaign, url=a.url, path=os.path.expanduser(a.file) if a.file else "", minutes=a.minutes,
                 title=a.title, direct=a.direct)
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
    elif a.cmd == "plan":
        print(posting.format_post_order(posting.post_order(r.ledger)), end="")
        print(f"→ {posting.write_post_order(r.ledger)}")
    elif a.cmd == "blockers":
        if a.set is not None:
            r.ledger.set_kv("blockers", json.dumps(a.set))
        print(json.loads(r.ledger.get_kv("blockers", "[]") or "[]"))
    elif a.cmd == "nudge":
        print("sent" if r.ready_nudge() else "nothing new to nudge")
    elif a.cmd == "prune":
        print(f"removed {r.prune(a.keep_days)} file(s)")
    elif a.cmd == "loop":
        r.loop()


if __name__ == "__main__":
    sys.exit(main())
