"""Six owner-requested fashion desks, with source-bound reviewed images."""

import subprocess
from typing import Any

from studio_core.production.fashion_policy import approved_brand, image_review, section

from . import parsing
from .model import canonical, scene
from .prepare_news import NewsPreparation


class FashionPreparation(NewsPreparation):
    def fashion(self) -> list[dict[str, Any]]:
        from studio_core.production.news_selection import diverse, publisher

        candidates = self.news_candidates("style")
        candidates.sort(key=lambda i: i["published"], reverse=True)
        selected: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        topics: list[str] = []
        publishers: dict[str, int] = {}
        attempts = 0
        for item in diverse(candidates, publishers):
            if len(selected) >= (4 if self.app == "snap-bot-v2" else 3):
                break
            if section(item["title"], item["detail"]) != self.key:
                continue
            topic = item["title"] + " " + item["detail"][:120]
            if parsing.same_topic(topic, topics):
                continue
            if attempts >= 25:
                break
            attempts += 1
            try:
                text, points = self.article(item)
                if len(parsing.words_only(text)) < 300:
                    continue
                if not approved_brand(item["title"] + " " + parsing.words_only(text)):
                    continue
                image = self.news_picture(item)
                asset = next(a for a in self.assets if a["file"] == image)
                review = image_review(
                    self.db.parent / "fashion-image-reviews.jsonl",
                    asset["sha256"], asset["source_url"], item["url"],
                )
                if review is None:
                    failures.append({
                        "code": "FASHION_IMAGE_REVIEW_REQUIRED", "source": item["source"],
                        "title": item["title"], "desk": self.key,
                        "article_url": item["url"], "image_url": asset["source_url"],
                        "image_file": image, "image_sha256": asset["sha256"],
                    })
                    continue
                proof = self.save(canonical(review), ".json", item["url"], "مراجعة احتشام الصورة")
                item["fashion_original_title"] = item["title"]
                if item.get("language") == "en":
                    original = item["title"]
                    item["title"] = self.translate(original)
                    item["translations"] = {"body": self.translations[original]}
                    points = [
                        {**p, "head": self.translate(p["head"]) if p["head"] else "",
                         "body": self.translate(p["body"]),
                         "translations": {
                             field: self.translations[p[field]] for field in ("head", "body") if p[field]
                         }}
                        for p in points
                    ]
                selected.append({"item": item, "image": image, "review": proof, "points": points})
                topics.append(topic)
                pub = publisher(item["url"])
                publishers[pub] = publishers.get(pub, 0) + 1
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                continue
        # Preserve the source/image evidence for review, without making an unsafe clip.
        (self.root / "fashion-image-candidates.json").write_bytes(canonical(failures))
        if not selected:
            raise ValueError("FASHION_NO_APPROVED_STORY_IMAGE")
        scenes: list[dict[str, Any]] = []
        for n, row in enumerate(selected, 1):
            item = row["item"]
            common = {
                "image": row["image"], "image_credit": item["source"],
                "source": item["source"], "source_id": item["source_id"],
                "source_url": item["url"], "published": item["published"],
                "article": item["article"], "feed": item["feed"],
                "published_label": item["published_label"], "fashion_review": row["review"],
                "fashion_original_title": item["fashion_original_title"],
            }
            if len(selected) > 1:
                detail = parsing.clean(item["detail"], max(100, 230 - len(item["title"])))
                translations = item.get("translations", {})
                if item.get("language") == "en":
                    # Translate the exact detail separately and retain its proof.
                    original = detail
                    detail = self.translate(original)
                    translations = {
                        "head": translations["body"], "body": self.translations[original],
                    }
                scenes.append(scene(
                    detail, max(12.0, (len(item["title"]) + len(detail)) / 17 + 0.25),
                    role="story", head=item["title"], number=f"{n}/{len(selected)}",
                    translations=translations, **common,
                ))
            else:
                scenes.append(scene(
                    item["title"], max(5.0, len(item["title"]) / 17 + 0.25),
                    role="headline", translations=item.get("translations", {}), **common,
                ))
                chosen = [p for p in row["points"] if p.get("body")][:4]
                while len(chosen) > 2 and sum(
                    max(9.0, (len(p["body"]) + len(p["head"])) / 17 + 0.25) for p in chosen
                ) > 44.7:
                    chosen.pop()
                if len(chosen) < 2:
                    raise ValueError("FASHION_ARTICLE_TOO_SHORT")
                for point in chosen:
                    # Reuse only this story's reviewed photo; no unreviewed inline images.
                    scenes.append(scene(
                        point["body"], max(9.0, (len(point["body"]) + len(point["head"])) / 17 + 0.25),
                        role="point", head=point["head"],
                        translations=point.get("translations", {}), **common,
                    ))
            self.used(item["url"])
        return scenes
