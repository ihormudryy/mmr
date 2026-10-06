"""Post an AI code review on a pull request as one reviewer's GitHub App.

Run by .github/workflows/ai-review.yml when a `review:<key>` label is added.
The model backend is config: REVIEW_PROVIDER = openrouter | bedrock | azure.
An unknown or unconfigured backend fails loudly; there is no fallback.
"""
import json
import os
import re
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
MAX_DIFF_CHARS = 400_000
REVIEWERS = {"openai", "grok"}
SEVERITIES = ("blocker", "major", "minor")


class ReviewError(Exception):
    pass


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ReviewError(f"missing environment variable {name}")
    return value


def http_json(url, *, method="GET", headers=None, body=None, accept="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Accept": accept, **(headers or {})})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        raise ReviewError(f"{method} {url} failed: {error.code} {error.read().decode()[:500]}") from error
    return json.loads(raw) if accept.endswith("json") and raw else raw


class GitHub:
    def __init__(self, repo, token):
        self.repo = repo
        self.headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}

    def call(self, path, **kwargs):
        kwargs.setdefault("accept", "application/vnd.github+json")
        return http_json(f"{GITHUB_API}/repos/{self.repo}/{path}", headers=self.headers, **kwargs)


def complete(provider, model, system, prompt):
    if provider == "openrouter":
        response = http_json(
            "https://openrouter.ai/api/v1/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {required_env('OPENROUTER_API_KEY')}"},
            body={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
        )
        return response["choices"][0]["message"]["content"]
    if provider == "azure":
        endpoint = required_env("AZURE_OPENAI_ENDPOINT").rstrip("/")
        version = required_env("AZURE_OPENAI_API_VERSION")
        response = http_json(
            f"{endpoint}/openai/deployments/{model}/chat/completions?api-version={version}",
            method="POST",
            headers={"api-key": required_env("AZURE_OPENAI_API_KEY")},
            body={"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
        )
        return response["choices"][0]["message"]["content"]
    if provider == "bedrock":
        import boto3  # installed by the workflow only for this backend

        client = boto3.client("bedrock-runtime", region_name=required_env("AWS_REGION"))
        response = client.converse(
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        return "".join(part.get("text", "") for part in response["output"]["message"]["content"])
    raise ReviewError(f"unknown REVIEW_PROVIDER {provider!r}; use openrouter, bedrock or azure")


def review_rules():
    text = open("AGENTS.md", encoding="utf-8").read()
    match = re.search(r"## GitHub identity for AI agents\n(.*?)\n## ", text, re.S)
    if not match:
        raise ReviewError("AGENTS.md has no 'GitHub identity for AI agents' section")
    return match.group(1)


def build_prompt(pr, diff, previous_reviews):
    truncated = len(diff) > MAX_DIFF_CHARS
    note = f"\n\nNOTE: the diff was cut to the first {MAX_DIFF_CHARS} characters." if truncated else ""
    prompt = (
        f"Pull request #{pr['number']}: {pr['title']}\nHead commit: {pr['head']['sha']}\n\n"
        f"Description:\n{pr['body'] or ''}\n\n"
        f"Your earlier reviews on this PR (do not repeat fixed or known items):\n{previous_reviews or '(none)'}\n\n"
        f"Diff:\n{diff[:MAX_DIFF_CHARS]}{note}"
    )
    return prompt, truncated


SYSTEM = """You review code for MMR, a trading system where a wrong order can lose money.
Find real defects only: correctness, races, crash/restart safety, broker evidence, security.
Follow these repository rules:
{rules}
Answer with JSON only, no prose around it:
{{"verdict": "ready" | "not yet",
  "summary": "<short plain-English summary>",
  "findings": [{{"severity": "blocker|major|minor", "ticket": "#NN or empty",
                 "path": "<file in the diff>", "line": <line number on the new side>,
                 "body": "<the defect, a concrete event order or failing input, and the smallest fix>"}}]}}
A blocker needs a concrete failing input or exact event order. Plain English, short sentences."""


def parse_review(text):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ReviewError("model answer has no JSON object")
    review = json.loads(match.group(0))
    if review.get("verdict") not in ("ready", "not yet"):
        raise ReviewError(f"bad verdict {review.get('verdict')!r}")
    findings = [f for f in review.get("findings", []) if f.get("severity") in SEVERITIES and f.get("body")]
    return review["verdict"], review.get("summary", ""), findings


def review_body(verdict, summary, findings, signature, truncated):
    lines = [f"**{verdict}**", "", summary]
    if truncated:
        lines += ["", f"The diff was longer than {MAX_DIFF_CHARS} characters; only the first part was reviewed."]
    for finding in findings:
        where = f"`{finding.get('path')}:{finding.get('line')}`" if finding.get("path") else ""
        lines.append(f"- **{finding['severity']}** {finding.get('ticket', '')} {where} {finding['body']}")
    lines += ["", f"Reviewed by {signature} (automated, diff only; tests were not run)."]
    return "\n".join(lines)


def main():
    reviewer = required_env("REVIEWER_KEY")
    if reviewer not in REVIEWERS:
        raise ReviewError(f"unknown reviewer {reviewer!r}")
    github = GitHub(required_env("GITHUB_REPOSITORY"), required_env("GH_TOKEN"))
    number = int(required_env("PR_NUMBER"))
    provider, model = required_env("REVIEW_PROVIDER"), required_env("REVIEW_MODEL")
    bot_login = f"mmr-{reviewer}[bot]"

    pr = github.call(f"pulls/{number}")
    diff = github.call(f"pulls/{number}", accept="application/vnd.github.diff")
    previous = "\n\n".join(
        r["body"] for r in github.call(f"pulls/{number}/reviews?per_page=100") if r["user"]["login"] == bot_login
    )
    prompt, truncated = build_prompt(pr, diff, previous)
    verdict, summary, findings = parse_review(complete(provider, model, SYSTEM.format(rules=review_rules()), prompt))

    body = review_body(verdict, summary, findings, f"{provider}:{model}", truncated)
    event = "REQUEST_CHANGES" if any(f["severity"] == "blocker" for f in findings) else "COMMENT"
    github.call(f"pulls/{number}/reviews", method="POST", body={"commit_id": pr["head"]["sha"], "body": body, "event": event})
    github.call(f"issues/{number}/labels/review:{reviewer}", method="DELETE")
    print(f"posted {verdict} review with {len(findings)} findings")


if __name__ == "__main__":
    main()
