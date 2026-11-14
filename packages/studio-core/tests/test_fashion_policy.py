import hashlib
import tempfile
import unittest
from pathlib import Path

from studio_core.production.fashion_policy import (
    approved_brand,
    image_review,
    record_review,
    review_matches,
    section,
)


class FashionPolicyTests(unittest.TestCase):
    def test_all_six_desks_are_distinct_and_title_has_priority(self):
        fixtures = {
            "style": "ديور تطلق مجموعة جديدة",
            "style_women": "حقائب شانيل الجديدة للمرأة",
            "style_men": "أرماني تطلق أزياء رجالية",
            "style_perfume": "عطر برادا الجديد",
            "style_couples": "هدايا غوتشي للأزواج",
            "style_kids": "ملابس بربري للأطفال",
        }
        for key, title in fixtures.items():
            with self.subTest(key=key):
                self.assertEqual(section(title), key)
        self.assertEqual(section("عطر ديور الجديد", "ذُكر في معرض أزياء الأطفال"), "style_perfume")

    def test_unapproved_local_and_mixed_local_brand_stories_are_rejected(self):
        for title in (
            "عطر من علامة مجهولة",
            "مجموعة هدى بيوتي الجديدة",
            "عطر العربية للعود",
            "عطر أجمل مع ديور",
            "شانيل تتعاون مع علامة محلية",
            "عطر ديورا الجديد",
        ):
            with self.subTest(title=title):
                self.assertIsNone(section(title))
        self.assertTrue(approved_brand("Chanel launches a new perfume"))
        self.assertTrue(approved_brand("حقيبة جديدة لديور"))

    def test_unseen_changed_or_revoked_image_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.jpg"
            image.write_bytes(b"synthetic-image-for-policy-only")
            ledger = root / "reviews.jsonl"
            image_url = "https://example.test/photo.jpg"
            article_url = "https://example.test/story"
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            self.assertIsNone(image_review(ledger, digest, image_url, article_url))
            record = record_review(ledger, image, image_url, article_url, approve=True)
            self.assertFalse(ledger.exists())
            self.assertTrue(review_matches(record, digest, image_url, article_url))
            record_review(ledger, image, image_url, article_url, approve=True, apply=True)
            self.assertIsNotNone(image_review(ledger, digest, image_url, article_url))
            self.assertIsNone(image_review(ledger, "0" * 64, image_url, article_url))
            self.assertIsNone(image_review(ledger, digest, image_url, article_url + "2"))
            before = ledger.read_bytes()
            record_review(ledger, image, image_url, article_url, approve=False, apply=True)
            self.assertTrue(ledger.read_bytes().startswith(before))
            self.assertIsNone(image_review(ledger, digest, image_url, article_url))


if __name__ == "__main__":
    unittest.main()
