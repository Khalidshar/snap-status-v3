#!/usr/bin/env python3
"""Read public fashion sources; retain evidence without rendering, sending or consuming history."""

import argparse
import base64
from pathlib import Path
from types import SimpleNamespace

from studio_core.network.client import NetworkClient, NetworkResponse
from studio_core.network.production import ProductionPolicy, fetch
from studio_core.production.fashion_diagnostic import STAGES, diagnose
from studio_core.production.fashion_policy import TITLES
from studio_core.production.model import canonical, sha
from studio_core.production.prepare import Factory
from studio_core.sources.news import NewsUnavailable, collect


class Cache:
    def __init__(self, client, output):
        self.client, self.output, self.values = client, output, {}

    def get(self, url):
        if url not in self.values:
            response = self.client.get(url)
            self.values[url] = response
            name = sha(url.encode()) + ".bin"
            (self.output / name).write_bytes(response.body)
            with (self.output / "manifest.jsonl").open("ab") as stream:
                row = {"url": url, "file": name, "sha256": response.sha256}
                stream.write(canonical(row) + b"\n")
        return self.values[url]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=("snap", "status"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Use the existing pinned public-source policy, outside workers",
    )
    args = parser.parse_args()
    # A new explicit directory only: no operational files, overwrites or deletion of evidence.
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = args.output / "evidence"
    evidence.mkdir()
    policy = ProductionPolicy(Path(f"/run/madar/{args.app}-production.sock"), network_timeout=8)

    def direct(url):
        value = fetch(url, policy)
        return NetworkResponse(
            value["status"],
            value["content_type"],
            base64.b64decode(value["body"]),
            value["sha256"],
        )

    client = Cache(SimpleNamespace(get=direct) if args.direct else NetworkClient(policy), evidence)
    probe = Factory(
        args.app + "-bot-v2",
        "style",
        "d" * 32,
        args.output / "probe",
        client=client,
        state=args.output / "probe-state",
    )
    report = diagnose(client, website_reader=probe.website_candidates)
    collectors = []
    for key in TITLES:
        trace = []
        row = {"command": key, "sources": trace}
        try:
            content = collect(key, client, report["checked_at"], diagnostics=trace)["content"]
            row.update(
                result="CURRENT_REVIEW",
                source=content["source_id"],
                language=content["language"],
                desk=content.get("fashion_desk", key),
                requires_owner_review=content.get("requires_owner_review", False),
            )
        except NewsUnavailable:
            row["result"] = "NO_CURRENT_REVIEW"
        collectors.append(row)
    report["command_news_collectors"] = collectors
    (args.output / "report.json").write_bytes(canonical(report))
    rows = [
        "| الأمر | خام | العمر | اللغة/الموضوع | البراند | القسم | الصورة | بلا تكرار | البديل |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    any_source = any("raw" in source for source in report["sources"])
    for row in report["rows"]:
        values = [str(row[stage]) if any_source else "—" for stage in (*STAGES, "fallback_pool")]
        rows.append("| " + row["title"] + " | " + " | ".join(values) + " |")
    completeness = (
        "نتائج جزئية للمصادر المتاحة" if not report["complete"] else "اكتمل جلب المصادر المعتمدة"
    )
    rows.extend(
        [
            "",
            completeness,
            "السجل التشغيلي لم يُقرأ؛ منع التكرار هنا بين مواضيع المرشحين فقط.",
            "الصورة تعني توفر صورة عامة؛ اعتماد خالد والمدة والرسم ليست مثبتة بهذا الفحص.",
            "المواقع التي ليست RSS تمر بمحول الإنتاج؛ عدد خام لها بعد مرشحات المحول.",
            "",
            "| المصدر | النتيجة | العناصر |",
            "|---|---|---:|",
        ]
    )
    for row in report["sources"]:
        rows.append(f"| {row['source']} | {row['result']} | {row.get('raw', '—')} |")
    rows += [
        "",
        "## مسار command_news.collect الفعلي (لا إرسال)",
        "| الأمر | المصدر | اللغة | المكتب الفعلي | النتيجة |",
        "|---|---|---|---|---|",
    ]
    for row in collectors:
        rows.append(
            "| "
            + " | ".join(
                str(row.get(k, "—")) for k in ("command", "source", "language", "desk", "result")
            )
            + " |"
        )
    for row in collectors:
        rows += [
            "",
            "### " + row["command"],
            (
                "| المصدر | خام | محلل | عنوان | لغة | براند | قسم | عمر | تكرار |"
                " شرط إضافي | بديل | السبب |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for source in row["sources"]:
            values = [
                str(source["counts"][stage])
                for stage in (
                    "raw",
                    "parsed",
                    "title",
                    "language",
                    "brand",
                    "section",
                    "age",
                    "seen",
                    "predicate",
                )
            ]
            rows.append(
                "| "
                + source["source_id"]
                + " | "
                + " | ".join(values)
                + " | "
                + str(source["fallback_accepted"])
                + " | "
                + source.get("reason", "—")
                + ":"
                + source.get("error_type", "")
                + " |"
            )
    markdown = "\n".join(rows) + "\n"
    (args.output / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
