"""Collect Windows 10/11 quality-update metadata from Microsoft Graph beta."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

INGESTION_ENDPOINT = "__INGESTION_ENDPOINT__"
DCR_IMMUTABLE_ID = "__DCR_IMMUTABLE_ID__"
STREAM = "Custom-WindowsMonthlyPatchCatalog_CL"
MAX_UPLOAD_BYTES = 900_000
GRAPH_RESOURCE = "https://graph.microsoft.com/"
GRAPH_ROOT = GRAPH_RESOURCE + "beta/admin/windows/updates/"
PRODUCTS_URL = GRAPH_ROOT + "products?" + urlencode({
    "$expand": "revisions($expand=catalogEntry,knowledgeBaseArticle)",
})
FAMILIES = {"Windows 10": "Windows10", "Windows 11": "Windows11"}
QUALITY_TYPE = "#microsoft.graph.windowsUpdates.qualityUpdateCatalogEntry"
FEATURE_TYPE = "#microsoft.graph.windowsUpdates.featureUpdateCatalogEntry"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        raise HTTPError(request.full_url, code, "Authenticated redirects are not allowed", headers, response)


def request_bytes(request: Request, limit: int = 10_000_000) -> tuple[bytes, str, str]:
    for attempt in range(3):
        delay = 2 ** (attempt + 1)
        try:
            with build_opener(NoRedirect()).open(request, timeout=90) as response:
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise ValueError(f"Response exceeds {limit} bytes: {request.host}")
                return content, response.url, response.headers.get_content_charset() or "utf-8"
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After")
            if retry_after:
                if retry_after.isdigit():
                    delay = int(retry_after)
                else:
                    delay = max(0, (
                        parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                    ).total_seconds())
            print(f"Retrying {request.host}: HTTP {error.code} after {delay}s", flush=True)
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            print(f"Retrying network request to {request.host}", flush=True)
        time.sleep(delay)
    raise RuntimeError("HTTP retry loop ended unexpectedly")


def identity_token(resource: str = "https://monitor.azure.com") -> str:
    if resource not in ("https://monitor.azure.com", GRAPH_RESOURCE):
        raise ValueError("Unexpected managed identity token resource")
    endpoint = os.environ.get("IDENTITY_ENDPOINT")
    header = os.environ.get("IDENTITY_HEADER")
    if not endpoint or not header:
        raise RuntimeError("Azure Automation managed identity endpoint is unavailable")
    separator = "&" if "?" in endpoint else "?"
    payload, _, _ = request_bytes(Request(
        endpoint + separator + urlencode({"resource": resource}),
        headers={"X-IDENTITY-HEADER": header, "Metadata": "True"},
    ))
    token = json.loads(payload)["access_token"]
    if not isinstance(token, str) or not token:
        raise RuntimeError("Managed identity returned an invalid access token")
    return token


def graph_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com"
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or parsed.fragment or not parsed.path.startswith("/beta/admin/windows/updates/")
    ):
        raise ValueError("Unexpected Graph catalog pagination URL")
    return value


class GraphClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("A Graph access token is required")
        self.headers = {"Authorization": "Bearer " + token, "Prefer": "odata.maxpagesize=100"}

    def get_json(self, url: str) -> dict:
        content, _, _ = request_bytes(Request(graph_url(url), headers=self.headers))
        value = json.loads(content)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("Graph returned an invalid catalog response")
        return value

    def collection(self, url: str):
        visited = set()
        while url:
            if url in visited:
                raise ValueError("Graph pagination contains a cycle")
            visited.add(url)
            page = self.get_json(url)
            if not isinstance(page.get("value"), list):
                raise ValueError("Graph collection does not contain a value array")
            for item in page["value"]:
                if not isinstance(item, dict):
                    raise ValueError("Graph collection contains a non-object item")
                yield item
            next_url = page.get("@odata.nextLink")
            if next_url is not None and not isinstance(next_url, str):
                raise ValueError("Graph nextLink is not a string")
            url = graph_url(next_url) if next_url else None

    def revisions(self, product: dict):
        revisions = product.get("revisions")
        if not isinstance(revisions, list):
            raise ValueError("Expanded Graph revisions array is missing")
        for revision in revisions:
            if not isinstance(revision, dict):
                raise ValueError("Graph revision is not an object")
            yield revision
        next_url = product.get("revisions@odata.nextLink")
        if next_url is not None and not isinstance(next_url, str):
            raise ValueError("Graph revision nextLink is not a string")
        if next_url:
            yield from self.collection(graph_url(next_url))


def product_family(product: dict) -> str | None:
    family = FAMILIES.get(product.get("groupName"))
    if family:
        return family
    name = product.get("name")
    if isinstance(name, str):
        match = re.match(r"^Windows (10|11)(?:\b|,)", name)
        if match:
            return "Windows" + match[1]
    return None


def graph_time(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Graph {field} is not a timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError(f"Graph {field} does not include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_revision(family: str, revision: dict, now: datetime) -> dict | None:
    entry = revision.get("catalogEntry")
    kb = revision.get("knowledgeBaseArticle")
    # Graph also exposes RTM/feature placeholders with no quality-update KB.
    if entry is None and kb is None:
        return None
    if isinstance(entry, dict) and entry.get("@odata.type") == FEATURE_TYPE:
        return None
    if not isinstance(entry, dict) or entry.get("@odata.type") != QUALITY_TYPE:
        raise ValueError("Graph quality revision has an unknown catalog-entry type")
    if not isinstance(kb, dict):
        raise ValueError("Graph quality revision is missing its KB article")
    if revision.get("product") not in FAMILIES or FAMILIES[revision["product"]] != family:
        raise ValueError("Graph product and revision families disagree")
    if type(revision.get("isHotpatchUpdate")) is not bool:
        raise ValueError("Graph revision does not specify its Hotpatch status")
    if revision["isHotpatchUpdate"]:
        return None
    cadence = entry.get("qualityUpdateCadence")
    classification = entry.get("qualityUpdateClassification")
    if cadence not in ("monthly", "outOfBand"):
        raise ValueError(f"Unsupported Graph update cadence: {cadence}")
    if classification not in ("security", "nonSecurity", "all"):
        raise ValueError(f"Unsupported Graph update classification: {classification}")
    released = graph_time(revision.get("releaseDateTime"), "revision.releaseDateTime")
    catalog_release = graph_time(entry.get("releaseDateTime"), "catalogEntry.releaseDateTime")
    if released > now or catalog_release > now:
        return None
    kind = "OOB" if cadence == "outOfBand" else (
        "B" if classification == "security" else (
            "Preview" if classification == "nonSecurity" else "Other"
        )
    )
    if kind == "B" and (released.year, released.month) != (
        catalog_release.year, catalog_release.month
    ):
        raise ValueError("Graph monthly catalog and revision release months disagree")
    build = revision.get("osBuild")
    keys = ("majorVersion", "minorVersion", "buildNumber", "updateBuildRevision")
    if not isinstance(build, dict) or any(
        type(build.get(key)) is not int or build[key] < 0 for key in keys
    ):
        raise ValueError("Graph osBuild must contain four nonnegative integers")
    if build["majorVersion"] == 0 or build["buildNumber"] == 0:
        raise ValueError("Graph osBuild is not a Windows release build")
    parts = [str(build[key]) for key in keys]
    kb_id = kb.get("id")
    match = re.fullmatch(r"(?:KB)?(\d{6,8})", kb_id, re.IGNORECASE) if isinstance(kb_id, str) else None
    if match is None:
        raise ValueError("Graph KB article identifier is not a KB number")
    source = kb.get("url")
    parsed_url = urlsplit(source) if isinstance(source, str) else None
    if parsed_url is None or parsed_url.scheme != "https" or parsed_url.hostname != "support.microsoft.com":
        raise ValueError("Graph KB article URL is not an official HTTPS support URL")
    return {
        "RecordType": "Build", "Family": family,
        "BaseVersion": ".".join(parts[:3]), "FullVersion": ".".join(parts),
        "Revision": build["updateBuildRevision"],
        "ReleaseMonth": catalog_release.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "ReleaseDate": released.isoformat(),
        "PatchId": "KB" + match[1], "ReleaseKind": kind, "SourceUrl": source,
    }


def collect(now: datetime, client: GraphClient) -> list[dict]:
    records = {}
    counts = Counter()
    for product in client.collection(PRODUCTS_URL):
        family = product_family(product)
        if family is None:
            counts["OutOfScopeProducts"] += 1
            continue
        counts["ClientProducts"] += 1
        accepted = 0
        for revision in client.revisions(product):
            row = normalize_revision(family, revision, now)
            if row is None:
                counts["NonQualityHotpatchOrFuture"] += 1
                continue
            key = tuple(row[name] for name in (
                "Family", "FullVersion", "ReleaseDate", "PatchId", "ReleaseKind",
            ))
            records[key] = row
            accepted += 1
        if not accepted:
            print(f"No usable quality-update revisions: {product.get('name')}", flush=True)
    for family in FAMILIES.values():
        months = {row["ReleaseMonth"] for row in records.values()
                  if row["Family"] == family and row["ReleaseKind"] == "B"}
        if len(months) < 6:
            raise ValueError(f"Graph provides fewer than six monthly security releases for {family}")
    print(json.dumps({"GraphCollection": dict(counts), "UniqueBuilds": len(records)}), flush=True)
    return sorted(records.values(), key=lambda row: (
        row["ReleaseDate"], row["Family"], row["BaseVersion"], row["Revision"],
    ))


def upload_batches(records, maximum_bytes=MAX_UPLOAD_BYTES):
    batch = []
    size = 2
    for record in records:
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) + 2 > maximum_bytes:
            raise ValueError("A catalog record exceeds the ingestion request limit")
        if batch and (size + len(encoded) + 1 > maximum_bytes or len(batch) == 1000):
            yield b"[" + b",".join(batch) + b"]"
            batch, size = [], 2
        size += len(encoded) + (1 if batch else 0)
        batch.append(encoded)
    if batch:
        yield b"[" + b",".join(batch) + b"]"


def publish(records, endpoint: str, dcr_id: str):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(
        ".ingest.monitor.azure.com"
    ):
        raise ValueError("A valid Azure Monitor ingestion endpoint is required")
    if not re.fullmatch(r"dcr-[a-fA-F0-9]{32}", dcr_id):
        raise ValueError("A valid DCR immutable ID is required")
    target = endpoint.rstrip("/") + f"/dataCollectionRules/{dcr_id}/streams/{STREAM}?api-version=2023-01-01"
    token = identity_token()
    for batch in upload_batches(records):
        request_bytes(Request(target, data=batch, method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
        }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint", default=INGESTION_ENDPOINT)
    parser.add_argument("--dcr-id", default=DCR_IMMUTABLE_ID)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    client = GraphClient(os.environ.get("YMC_GRAPH_ACCESS_TOKEN") or identity_token(GRAPH_RESOURCE))
    records = collect(now, client)
    snapshot = uuid.uuid4().hex
    snapshot_time = now.isoformat()
    for record in records:
        record.update(SnapshotId=snapshot, SnapshotTime=snapshot_time)
    marker = {
        "RecordType": "Snapshot", "SnapshotId": snapshot, "SnapshotTime": snapshot_time,
        "Family": "", "BaseVersion": "", "FullVersion": "", "Revision": 0,
        "ReleaseMonth": None, "ReleaseDate": None, "PatchId": "",
        "ReleaseKind": "", "SourceUrl": "",
    }
    if args.output:
        args.output.write_text(json.dumps(records + [marker]), encoding="utf-8")
    if not args.dry_run:
        publish(records, args.endpoint, args.dcr_id)
        publish([marker], args.endpoint, args.dcr_id)
    print(json.dumps({
        "Source": "MicrosoftGraphBeta", "BuildRecords": len(records),
        "LatestMonthlyRelease": max(
            row["ReleaseMonth"] for row in records if row["ReleaseKind"] == "B"
        ),
        "MonthlyReleases": sorted({
            row["ReleaseMonth"][:7] for row in records if row["ReleaseKind"] == "B"
        })[-6:],
        "RecordsByFamily": dict(Counter(row["Family"] for row in records)),
        "RecordsByKind": dict(Counter(row["ReleaseKind"] for row in records)),
        "Families": sorted({row["Family"] for row in records}),
        "SnapshotId": snapshot, "Uploaded": not args.dry_run,
    }), flush=True)


if __name__ == "__main__":
    main()
