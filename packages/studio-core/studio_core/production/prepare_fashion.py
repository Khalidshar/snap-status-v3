"""Six owner-requested fashion desks, with source-bound reviewed images."""

import subprocess
from typing import Any

from studio_core.production.fashion_policy import (
    FashionUnavailable,
    approved_brand,
    auto_review,
    image_review,
    matches_desk,
    owner_rejected,
)

from . import parsing
from .model import canonical, scene
from .prepare_news import NewsPreparation

SENTENCE_END = ".!?؟"


def summary(text: str, limit: int) -> str:
    """The feed summary cut at a sentence end; empty when no whole sentence of 8+ words fits."""
    clean = parsing.clean(text)
    if len(clean) > limit:
        cut = clean[:limit]
        end = max(cut.rfind(mark) for mark in SENTENCE_END)
        clean = cut[: end + 1] if end >= 0 else ""
    clean = clean.strip()
    if len(clean.split()) < 8:
        return ""
    return clean


class FashionPreparation(NewsPreparation):
    def fashion(self) -> list[dict[str, Any]]:
        candidates = self.news_candidates("style")
        candidates.sort(
            key=lambda i: (i.get("language", "ar") == "ar", i["published"]), reverse=True
        )
        failures: list[dict[str, Any]] = []
        self.fashion_desk = self.key
        selected = self._fashion_select(candidates, self.key, failures)
        if self.key != "style" and (
            not selected or (len(selected) == 1 and len(selected[0]["points"]) < 2)
        ):
            self.fashion_desk = "style"
            selected = self._fashion_select(candidates, "style", failures)
        # Retain rejected-image evidence from both passes, without consuming any story.
        (self.root / "fashion-image-candidates.json").write_bytes(canonical(failures))
        if not selected or (len(selected) == 1 and len(selected[0]["points"]) < 2):
            raise FashionUnavailable()
        scenes: list[dict[str, Any]] = []
        for n, row in enumerate(selected, 1):
            item = row["item"]
            common = {
                "image": row["image"],
                "image_credit": item["source"],
                "source": item["source"],
                "source_id": item["source_id"],
                "source_url": item["url"],
                "published": item["published"],
                "article": item["article"],
                "feed": item["feed"],
                "published_label": item["published_label"],
                "fashion_review": row["review"],
                "fashion_original_title": item["fashion_original_title"],
            }
            if len(selected) > 1:
                detail, translations = row["story"]
                scenes.append(
                    scene(
                        detail,
                        max(12.0, (len(item["title"]) + len(detail)) / 17 + 0.25),
                        role="story",
                        head=item["title"],
                        number=f"{n}/{len(selected)}",
                        translations=translations,
                        **common,
                    )
                )
            else:
                scenes.append(
                    scene(
                        item["title"],
                        max(5.0, len(item["title"]) / 17 + 0.25),
                        role="headline",
                        translations=item.get("translations", {}),
                        **common,
                    )
                )
                seen_bodies: set[str] = set()
                chosen = [
                    p
                    for p in row["points"]
                    if p.get("body")
                    and p["body"] not in seen_bodies
                    and not seen_bodies.add(p["body"])
                ][:4]
                while (
                    len(chosen) > 2
                    and sum(max(9.0, (len(p["body"]) + len(p["head"])) / 17 + 0.25) for p in chosen)
                    > 44.7
                ):
                    chosen.pop()
                if len(chosen) < 2:
                    raise ValueError("FASHION_ARTICLE_TOO_SHORT")
                # 16 Nov: points show the article's own photos (the one under a heading,
                # else the next unused one), under the same automatic check; otherwise the
                # story photo is reused.
                spare = [u for u in row.get("pics", []) if u not in {item.get("image")}]
                for point in chosen:
                    url = point.get("pic") or (spare.pop(0) if spare else "")
                    if url in spare:
                        spare.remove(url)
                    picture = self._point_picture(item, url, row)
                    scenes.append(
                        scene(
                            point["body"],
                            max(9.0, (len(point["body"]) + len(point["head"])) / 17 + 0.25),
                            role="point",
                            head=point["head"],
                            translations=point.get("translations", {}),
                            **{**common, **picture},
                        )
                    )
            self.used(item["url"])
            if self.fashion_desk == "style" and self.key != "style":
                self.used(item["url"], kind="style")
        return scenes

    def _point_picture(self, item: dict[str, Any], url: str, row: dict[str, Any]) -> dict[str, Any]:
        if not url:
            return {}
        try:
            image = self.picture(url, item["url"], item["source"])
            if image == row["image"]:
                return {}
            asset = next(a for a in self.assets if a["file"] == image)
            ledger = self.db.parent / "fashion-image-reviews.jsonl"
            if owner_rejected(ledger, asset["sha256"], asset["source_url"], item["url"]):
                return {}
            review = image_review(ledger, asset["sha256"], asset["source_url"], item["url"])
            review = review or auto_review(
                asset["sha256"],
                asset["source_url"],
                item["url"],
                item.get("fashion_original_title", item["title"]) + " " + item["detail"],
            )
            if review is None:
                return {}
            proof = self.save(canonical(review), ".json", item["url"], "مراجعة احتشام الصورة")
            return {"image": image, "fashion_review": proof}
        except (
            OSError,
            ValueError,
            KeyError,
            StopIteration,
            RuntimeError,
            subprocess.SubprocessError,
        ):
            return {}

    def _fashion_select(
        self, candidates: list[dict[str, Any]], desk: str, failures: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        from studio_core.production.news_selection import diverse, publisher

        selected: list[dict[str, Any]] = []
        topics: list[str] = []
        publishers: dict[str, int] = {}
        attempts = 0
        for original in diverse(candidates, publishers):
            item = dict(original)
            if len(selected) >= (4 if self.app == "snap-bot-v2" else 3):
                break
            if not matches_desk(desk, item["title"], item["detail"]):
                continue
            # A story scene shows the feed summary; it must be a complete sentence (16 Nov).
            if not summary(item["detail"], 230):
                continue
            if (
                desk == "style"
                and self.key != "style"
                and self.used(item["url"], reserve=False, kind="style")
            ):
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
                ledger = self.db.parent / "fashion-image-reviews.jsonl"
                review = image_review(ledger, asset["sha256"], asset["source_url"], item["url"])
                if review is None and not owner_rejected(
                    ledger, asset["sha256"], asset["source_url"], item["url"]
                ):
                    review = auto_review(
                        asset["sha256"],
                        asset["source_url"],
                        item["url"],
                        item["title"] + " " + item["detail"],
                    )
                if review is None:
                    failures.append(
                        {
                            "code": "FASHION_IMAGE_REVIEW_REQUIRED",
                            "source": item["source"],
                            "title": item["title"],
                            "desk": desk,
                            "requested_desk": self.key,
                            "article_url": item["url"],
                            "image_url": asset["source_url"],
                            "image_file": image,
                            "image_sha256": asset["sha256"],
                        }
                    )
                    continue
                proof = self.save(canonical(review), ".json", item["url"], "مراجعة احتشام الصورة")
                item["fashion_original_title"] = item["title"]
                if item.get("language") == "en":
                    original = item["title"]
                    item["title"] = self.translate(original)
                    item["translations"] = {"body": self.translations[original]}
                    points = [
                        {
                            **p,
                            "head": self.translate(p["head"]) if p["head"] else "",
                            "body": self.translate(p["body"]),
                            "translations": {
                                field: self.translations[p[field]]
                                for field in ("head", "body")
                                if p[field]
                            },
                        }
                        for p in points
                    ]
                # Prepare the story-scene summary here, so a summary whose translation fails its
                # fact check skips this story instead of failing the whole clip.
                detail = summary(item["detail"], max(100, 230 - len(item["title"])))
                if not detail:
                    continue
                story_translations = item.get("translations", {})
                if item.get("language") == "en":
                    source_detail = detail
                    detail = self.translate(source_detail)
                    # The shown Arabic must also read as a whole sentence, not a fragment.
                    if len(detail.split()) < 8:
                        continue
                    story_translations = {
                        "head": story_translations["body"],
                        "body": self.translations[source_detail],
                    }
                selected.append(
                    {
                        "item": item,
                        "image": image,
                        "review": proof,
                        "points": points,
                        "story": (detail, story_translations),
                        "pics": [
                            line[8:].strip()
                            for line in text.split("\n")
                            if line.startswith("[[IMG]]")
                        ][:8],
                    }
                )
                topics.append(topic)
                pub = publisher(item["url"])
                publishers[pub] = publishers.get(pub, 0) + 1
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                continue
        return selected
