"""Manual CLI for reviewing an existing PR or checking GitHub connectivity."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any, Optional, Sequence

from backend.config import Settings, get_settings
from backend.github.client import GitHubClient, is_real_github_token
from backend.models import QueuedPRJob
from backend.worker import build_review_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Review a GitHub pull request or verify GitHub credentials.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Verify the GitHub token and fetch PR metadata.")
    check.add_argument("--repo", required=True, help="owner/repository")
    check.add_argument("--pr", required=True, type=int, help="Pull request number")

    review = sub.add_parser("review", help="Analyze a PR and post eligible review comments.")
    review.add_argument("--repo", required=True, help="owner/repository")
    review.add_argument("--pr", required=True, type=int, help="Pull request number")
    review.add_argument(
        "--no-post",
        action="store_true",
        help="Run analysis and routing without posting comments to GitHub.",
    )
    review.add_argument(
        "--delivery-id",
        default=None,
        help="Override the idempotency key (defaults to a random CLI delivery).",
    )
    return parser


async def _check(settings: Settings, repo: str, pr_number: int) -> int:
    client = GitHubClient(settings=settings)
    try:
        user = await client.get_authenticated_user()
        bundle = await client.fetch_pull_request_bundle(repo, pr_number)
    finally:
        await client.aclose()

    print(
        json.dumps(
            {
                "authenticated_as": user.get("login"),
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": bundle.head_sha,
                "changed_files": len(bundle.changed_files),
                "diff_bytes": len(bundle.diff.encode("utf-8")),
                "is_public": bundle.is_public,
            },
            indent=2,
        )
    )
    return 0


async def _review(
    settings: Settings,
    repo: str,
    pr_number: int,
    *,
    post_comments: bool,
    delivery_id: Optional[str],
) -> int:
    pipeline = build_review_pipeline(settings)
    job = QueuedPRJob(
        delivery_id=delivery_id or f"cli-{uuid.uuid4().hex[:16]}",
        trace_id=f"tr-cli-{uuid.uuid4().hex[:12]}",
        repo=repo,
        pr_number=pr_number,
        commit_sha="pending-fetch",
        action="manual",
        is_public=True,
        diff="",
        changed_files=[],
        metadata={"source": "cli", "post_comments": post_comments},
    )
    try:
        summary = await pipeline.process(job, post_comments=post_comments)
    finally:
        await pipeline.aclose()

    print(summary.model_dump_json(indent=2))
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not is_real_github_token(settings.GITHUB_TOKEN):
        print(
            "GITHUB_TOKEN is missing or still a placeholder. Set a real token in .env first.",
            file=sys.stderr,
        )
        return 2
    if args.command == "check":
        return await _check(settings, args.repo, args.pr)
    return await _review(
        settings,
        args.repo,
        args.pr,
        post_comments=not args.no_post,
        delivery_id=args.delivery_id,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise failure
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
