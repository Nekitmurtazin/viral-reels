#!/usr/bin/env python3
"""Генератор синтетической истории — проверка пайплайна без Apify."""
import json, os, random, math
from datetime import datetime, timedelta, timezone
from viral import DATA, HISTORY, load_config, flat_watchlist

random.seed(7)
cfg = load_config()
wl = flat_watchlist(cfg)[:70]
now = datetime.now(timezone.utc)

HOOKS = [
    "Почему 9 из 10 фаундеров теряют деньги на первом найме",
    "Разбор: как маркетплейс вырос с 0 до 40 млн выручки за 11 месяцев",
    "Три цифры, которые надо считать до запуска продукта",
    "Что на самом деле означает LTV/CAC 3:1 и почему это ловушка",
    "Не поднимайте раунд, пока не закрыли эти 4 вопроса",
    "Как я потерял 12 млн на неправильной юнит-экономике",
    "Схема, по которой работают все успешные подписочные модели",
    "Найм первых 10 человек: где ошибаются почти все",
    "Ошибка в договоре, из-за которой бизнес уходит партнёру",
    "Считаем маржу правильно — большинство считает неверно",
    "Один вопрос на собеседовании, который заменяет весь скрининг",
    "Почему ваш продукт не растёт, хотя реклама работает",
    "Как перестать быть узким горлышком в собственной компании",
    "Разбираю финмодель реального стартапа на цифрах",
    "Что делать, когда выручка растёт, а денег на счету нет",
    "Первые 90 дней после запуска: чек-лист",
    "Инвестор смотрит на это в первые 30 секунд питча",
    "Как удержание в 5% меняет оценку компании вдвое",
]

os.makedirs(DATA, exist_ok=True)
rows = []
for username, niche in wl:
    base = math.exp(random.uniform(8.5, 12.5))          # своя норма аккаунта
    n_posts = random.randint(14, 26)
    viral_idx = random.randint(0, 3) if random.random() < 0.35 else -1
    for i in range(n_posts):
        age_h = i * random.uniform(14, 40) + random.uniform(2, 20)
        views = base * math.exp(random.gauss(0, 0.45))
        if i == viral_idx:
            views = base * random.uniform(2.8, 14)
        views = int(views)
        rows.append({
            "id": f"{username}_{i}",
            "username": username,
            "niche": niche,
            "url": f"https://www.instagram.com/reel/{username[:6]}{i}/",
            "caption": random.choice(HOOKS),
            "views": views,
            "likes": int(views * random.uniform(0.02, 0.075)),
            "comments": int(views * random.uniform(0.0008, 0.005)),
            "duration": round(random.uniform(12, 75), 1),
            "timestamp": (now - timedelta(hours=age_h)).isoformat(),
            "thumbnail": None,
            "owner_followers": int(base * random.uniform(3, 14)),
            "fetched_at": now.isoformat(),
        })

with open(HISTORY, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"mock: {len(rows)} постов, {len(wl)} аккаунтов")
