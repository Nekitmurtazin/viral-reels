#!/usr/bin/env python3
"""
Еженедельный сборщик виральных Reels на бизнес-темы (RU).

Логика: виральность = не абсолютные просмотры, а отклонение от собственной нормы
аккаунта. outlier = views / медиана просмотров последних N роликов этого аккаунта.

Команды:
    python viral.py collect     — забрать свежие reels по watchlist в историю
    python viral.py discover    — найти новые аккаунты по хэштегам
    python viral.py report      — посчитать топ и собрать HTML-дашборд
    python viral.py run         — collect + report (то, что дёргает планировщик)
"""

import base64
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from statistics import median

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
HISTORY = os.path.join(DATA, "history.jsonl")
ACCOUNTS_STATE = os.path.join(DATA, "accounts.json")
CONFIG = os.path.join(ROOT, "config.json")

APIFY_BASE = "https://api.apify.com/v2"


# ─────────────────────────── утилиты ───────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def token():
    t = os.environ.get("APIFY_TOKEN", "").strip()
    if not t:
        sys.exit("APIFY_TOKEN не задан. export APIFY_TOKEN=apify_api_...")
    return t


def flat_watchlist(cfg):
    """Плоский список (username, ниша) без дублей."""
    seen, out = set(), []
    for niche, users in cfg["watchlist"].items():
        for u in users:
            u = u.strip().lstrip("@").lower()
            if u and u not in seen:
                seen.add(u)
                out.append((u, niche))
    # аккаунты, добытые автопоиском
    for u, meta in load_accounts_state().get("discovered", {}).items():
        if u not in seen:
            seen.add(u)
            out.append((u, meta.get("niche", "найдено автопоиском")))
    return out


def load_accounts_state():
    if os.path.exists(ACCOUNTS_STATE):
        with open(ACCOUNTS_STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"dead": [], "discovered": {}}


def save_accounts_state(state):
    os.makedirs(DATA, exist_ok=True)
    with open(ACCOUNTS_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─────────────────────────── Apify ───────────────────────────

def apify_run(actor, payload, timeout_secs=900):
    """Синхронный запуск актора, возвращает элементы датасета."""
    url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
    params = {"token": token(), "timeout": timeout_secs}
    r = requests.post(url, params=params, json=payload, timeout=timeout_secs + 60)
    if r.status_code >= 400:
        log(f"  Apify {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    return r.json()


# ─────────────────────── нормализация постов ───────────────────────

def to_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def norm_post(item, niche):
    """Приводим сырой ответ Apify к нашей схеме. Поля у актора плавают — берём с запасом."""
    username = (item.get("ownerUsername") or item.get("username")
                or (item.get("owner") or {}).get("username") or "").lower()
    views = to_int(item.get("videoPlayCount")) or to_int(item.get("videoViewCount")) \
        or to_int(item.get("playCount")) or to_int(item.get("viewCount"))
    ts = item.get("timestamp") or item.get("takenAtTimestamp") or item.get("takenAt")
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    shortcode = item.get("shortCode") or item.get("shortcode") or item.get("code") or ""
    if not shortcode and item.get("url"):
        m = re.search(r"/(?:reel|p)/([^/?]+)", item["url"])
        shortcode = m.group(1) if m else ""
    if not shortcode or not username:
        return None
    return {
        "id": shortcode,
        "username": username,
        "niche": niche,
        "url": item.get("url") or f"https://www.instagram.com/reel/{shortcode}/",
        "caption": (item.get("caption") or "")[:600],
        "views": views,
        "likes": to_int(item.get("likesCount")) or to_int(item.get("likeCount")),
        "comments": to_int(item.get("commentsCount")) or to_int(item.get("commentCount")),
        "duration": float(item.get("videoDuration") or 0),
        "timestamp": ts,
        "thumbnail": item.get("displayUrl") or item.get("thumbnailUrl") or item.get("images", [None])[0],
        "owner_followers": to_int(item.get("ownerFollowersCount")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def append_history(posts):
    os.makedirs(DATA, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def prune_history(keep_days=120):
    """Схлопываем дубли и режем хвост, чтобы история не росла бесконечно."""
    posts = load_history()
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept = [p for p in posts if (parse_ts(p.get("timestamp")) or cutoff) >= cutoff]
    with open(HISTORY, "w", encoding="utf-8") as f:
        for p in sorted(kept, key=lambda x: x.get("timestamp") or ""):
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    log(f"История сжата: {len(posts)} → {len(kept)} роликов")


def load_history():
    """Читаем историю, для каждого поста оставляем самую свежую версию метрик."""
    if not os.path.exists(HISTORY):
        return []
    best = {}
    with open(HISTORY, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            prev = best.get(p["id"])
            if prev is None or p.get("views", 0) >= prev.get("views", 0):
                best[p["id"]] = p
    return list(best.values())


# ─────────────────────────── команды ───────────────────────────

def cmd_collect(cfg):
    wl = flat_watchlist(cfg)
    state = load_accounts_state()
    dead = set(state.get("dead", []))
    targets = [(u, n) for u, n in wl if u not in dead]
    log(f"Аккаунтов в работе: {len(targets)} (отбраковано ранее: {len(dead)})")

    limit = cfg["apify"]["reels_per_account"]
    batch_size = 25
    collected, seen_users = [], set()

    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        niche_of = {u: n for u, n in batch}
        log(f"Батч {i // batch_size + 1}/{(len(targets) - 1) // batch_size + 1}: {len(batch)} аккаунтов")
        try:
            items = apify_run(
                cfg["apify"]["reel_actor"],
                {"username": [u for u, _ in batch], "resultsLimit": limit},
                cfg["apify"]["timeout_secs"],
            )
        except Exception as e:
            log(f"  батч упал: {e}")
            continue
        for it in items:
            p = norm_post(it, "")
            if not p:
                continue
            p["niche"] = niche_of.get(p["username"], "прочее")
            seen_users.add(p["username"])
            collected.append(p)
        log(f"  получено роликов: {len(items)}")
        time.sleep(1)

    # аккаунты, не отдавшие ни одного ролика — кандидаты в мёртвые
    silent = [u for u, _ in targets if u not in seen_users]
    misses = state.setdefault("misses", {})
    for u in silent:
        misses[u] = misses.get(u, 0) + 1
    for u in seen_users:
        misses.pop(u, None)
    newly_dead = [u for u, c in misses.items() if c >= 2 and u not in dead]
    if newly_dead:
        state["dead"] = sorted(dead | set(newly_dead))
        log(f"Отбраковано (2 пустых прогона подряд): {', '.join(newly_dead)}")
    save_accounts_state(state)

    append_history(collected)
    log(f"Записано в историю: {len(collected)} роликов от {len(seen_users)} аккаунтов")
    return collected


def cmd_discover(cfg):
    d = cfg["discovery"]
    if not d.get("enabled"):
        return
    state = load_accounts_state()
    known = {u for u, _ in flat_watchlist(cfg)} | set(state.get("dead", []))
    found = defaultdict(int)
    followers = {}

    for tag in d["hashtags"]:
        log(f"Хэштег #{tag}")
        try:
            items = apify_run(
                cfg["apify"]["hashtag_actor"],
                {"hashtags": [tag], "resultsLimit": d["posts_per_hashtag"]},
                cfg["apify"]["timeout_secs"],
            )
        except Exception as e:
            log(f"  упал: {e}")
            continue
        for it in items:
            u = (it.get("ownerUsername") or "").lower()
            if not u or u in known:
                continue
            found[u] += 1
            followers[u] = max(followers.get(u, 0), to_int(it.get("ownerFollowersCount")))

    # приоритет — те, кто засветился в нескольких хэштегах
    ranked = sorted(found.items(), key=lambda kv: (-kv[1], -followers.get(kv[0], 0)))
    added = 0
    for u, hits in ranked:
        if added >= d["max_new_per_run"]:
            break
        if followers.get(u, 0) and followers[u] < d["min_followers_to_add"]:
            continue
        state.setdefault("discovered", {})[u] = {
            "niche": "найдено автопоиском",
            "hashtag_hits": hits,
            "followers": followers.get(u, 0),
            "added": datetime.now(timezone.utc).date().isoformat(),
        }
        added += 1
    save_accounts_state(state)
    log(f"Добавлено новых аккаунтов: {added}")


def cmd_expand(cfg, seeds=None, depth=2):
    """Граф похожих аккаунтов: Instagram сам подсказывает, кто в той же нише."""
    e = cfg.get("expand", {})
    state = load_accounts_state()
    known = {u for u, _ in flat_watchlist(cfg)} | set(state.get("dead", []))
    frontier = seeds or [u for u, _ in flat_watchlist(cfg)][:e.get("seed_limit", 20)]
    accepted, rejected = {}, 0

    for level in range(depth):
        if not frontier:
            break
        log(f"Уровень {level + 1}: разворачиваю {len(frontier)} аккаунтов")
        try:
            profiles = apify_run(
                cfg["apify"]["profile_actor"],
                {"usernames": frontier},
                cfg["apify"]["timeout_secs"],
            )
        except Exception as ex:
            log(f"  упал: {ex}")
            break

        nxt = []
        for prof in profiles:
            for rel in (prof.get("relatedProfiles") or []):
                u = (rel.get("username") or "").lower()
                if not u or u in known or u in accepted:
                    continue
                bio = f"{rel.get('full_name') or ''} {rel.get('biography') or ''}"
                cat = (rel.get('business_category_name') or '')
                fol = to_int(rel.get("followersCount") or rel.get("edge_followed_by", {}).get("count"))
                if not is_relevant(bio, cat, e):
                    rejected += 1
                    continue
                if fol and fol < e.get("min_followers", 15000):
                    rejected += 1
                    continue
                accepted[u] = {
                    "niche": "найдено графом похожих",
                    "followers": fol,
                    "via": prof.get("username"),
                    "added": datetime.now(timezone.utc).date().isoformat(),
                }
                nxt.append(u)
        log(f"  принято {len(accepted)}, отсеяно {rejected}")
        frontier = nxt[:e.get("expand_per_level", 60)]

    state.setdefault("discovered", {}).update(accepted)
    save_accounts_state(state)
    log(f"Итого добавлено в watchlist: {len(accepted)}")
    return list(accepted)


def is_relevant(bio, category, e):
    """Русскоязычный + тематический фильтр. Без кириллицы — мимо, без темы — мимо."""
    text = f"{bio} {category}".lower()
    if not re.search(r"[а-яё]", text):
        return False
    kw = e.get("keywords", [])
    stop = e.get("stopwords", [])
    if any(s in text for s in stop):
        return False
    return any(k in text for k in kw) if kw else True


# ─────────────────────────── скоринг ───────────────────────────

def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def norm_caption(c):
    c = re.sub(r"[#@]\S+", " ", (c or "").lower())
    c = re.sub(r"[^\wа-яё ]+", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def dedup(posts, threshold):
    """Один и тот же ролик, разошедшийся по десятку аккаунтов, показываем один раз."""
    kept = []
    for p in sorted(posts, key=lambda x: -x["score"]):
        cap = norm_caption(p["caption"])
        dupe = False
        for k in kept:
            if p["username"] == k["username"]:
                continue
            # основной сигнал — совпадение текста; длительность лишь ужесточает порог
            if not cap or len(cap) < 30:
                continue
            sim = SequenceMatcher(None, cap, norm_caption(k["caption"])).ratio()
            same_len = p["duration"] > 0 and abs(p["duration"] - k["duration"]) < 1.0
            if sim > threshold or (same_len and sim > threshold - 0.15):
                dupe = True
                k.setdefault("also_posted_by", []).append(p["username"])
                break
        if not dupe:
            kept.append(p)
    return kept


def score_posts(cfg, history):
    s = cfg["scoring"]
    now = datetime.now(timezone.utc)

    by_user = defaultdict(list)
    for p in history:
        if p.get("views"):
            by_user[p["username"]].append(p)

    baselines = {}
    for u, posts in by_user.items():
        posts.sort(key=lambda x: parse_ts(x["timestamp"]) or now, reverse=True)
        window = [p["views"] for p in posts[:s["baseline_window"]] if p["views"] > 0]
        if len(window) >= s["baseline_min_posts"]:
            baselines[u] = median(window)

    candidates = []
    for p in history:
        ts = parse_ts(p["timestamp"])
        if not ts:
            continue
        age_h = (now - ts).total_seconds() / 3600
        if age_h < s["min_post_age_hours"] or age_h > s["max_post_age_days"] * 24:
            continue
        if p["views"] < s["min_views"]:
            continue
        base = baselines.get(p["username"])
        if not base:
            continue
        p = dict(p)
        p["baseline"] = base
        p["outlier"] = round(p["views"] / base, 2)
        p["age_hours"] = round(age_h)
        p["er"] = round((p["likes"] + p["comments"] * 3) / max(p["views"], 1) * 100, 2)
        # ранжируем по отклонению, абсолютный охват — вторичный вес
        p["score"] = (p["outlier"] ** 0.75) * (max(p["views"], 1) ** 0.18)
        candidates.append(p)

    candidates = dedup(candidates, s["dedup_caption_similarity"])
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:s["top_n"]], baselines


# ─────────────────────── превью картинок ───────────────────────

def fetch_thumb(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        })
        if r.status_code != 200 or len(r.content) < 500:
            return None
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            im.thumbnail((360, 640))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72)
            raw = buf.getvalue()
        except Exception:
            raw = r.content
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None


# ─────────────────────────── отчёт ───────────────────────────

def cmd_report(cfg, embed_thumbs=True):
    history = load_history()
    if not history:
        sys.exit("История пуста — сначала запусти collect.")
    top, baselines = score_posts(cfg, history)
    log(f"История: {len(history)} роликов, аккаунтов с базлайном: {len(baselines)}, в топе: {len(top)}")

    if embed_thumbs:
        for i, p in enumerate(top, 1):
            p["thumb_data"] = fetch_thumb(p.get("thumbnail"))
            log(f"  превью {i}/{len(top)} {'ok' if p['thumb_data'] else '—'}")

    os.makedirs(OUT, exist_ok=True)
    stats = {
        "accounts_tracked": len(baselines),
        "posts_in_history": len(history),
        "median_outlier": round(median([p["outlier"] for p in top]), 2) if top else 0,
        "total_views": sum(p["views"] for p in top),
    }
    html = render_html(top, stats)
    path = os.path.join(OUT, "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUT, "top.json"), "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in p.items() if k != "thumb_data"} for p in top],
                  f, ensure_ascii=False, indent=2)
    log(f"Готово: {path}")
    return path


def fmt_num(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def render_html(top, stats):
    from jinja2 import Template
    with open(os.path.join(ROOT, "template.html"), encoding="utf-8") as f:
        tpl = Template(f.read())
    niches = sorted({p["niche"] for p in top})
    return tpl.render(
        posts=top, stats=stats, niches=niches, fmt=fmt_num,
        generated=datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y"),
        week_range=f"{(datetime.now() - timedelta(days=7)).strftime('%d.%m')} — {datetime.now().strftime('%d.%m.%Y')}",
    )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    conf = load_config()
    if cmd == "collect":
        cmd_collect(conf)
    elif cmd == "discover":
        cmd_discover(conf)
    elif cmd == "expand":
        seeds = [a.lstrip("@").lower() for a in sys.argv[2:]] or None
        cmd_expand(conf, seeds)
    elif cmd == "report":
        cmd_report(conf)
    elif cmd == "prune":
        prune_history()
    elif cmd == "run":
        cmd_collect(conf)
        cmd_report(conf)
        prune_history()
    else:
        sys.exit(__doc__)
