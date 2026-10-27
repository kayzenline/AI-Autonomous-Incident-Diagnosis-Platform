"""Submit one incident, follow its event stream, and print the report.

The server must already be running. The default diagnosis uses the offline
M4 investigator against a frozen scenario.
"""

import argparse
import json
import os
import sys
from typing import Any

import httpx

from app.eval.scenario import frozen_dir, load_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentops")
    commands = parser.add_subparsers(dest="command", required=True)
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("--scenario", help="Frozen scenario id to diagnose.")
    diagnose.add_argument("--title")
    diagnose.add_argument("--description")
    diagnose.add_argument("--service")
    diagnose.add_argument("--api", default=os.environ.get("AGENTOPS_API", "http://127.0.0.1:8000"))
    diagnose.add_argument("--api-key", default=os.environ.get("API_KEY"))
    args = parser.parse_args(argv)
    if args.command == "diagnose":
        return _diagnose(args)
    parser.error(f"unknown command {args.command}")
    return 2


def _diagnose(args: argparse.Namespace) -> int:
    if not args.api_key:
        print("Set API_KEY or pass --api-key.", file=sys.stderr)
        return 2
    payload = _incident_payload(args)
    if isinstance(payload, int):
        return payload
    headers = {"X-API-Key": args.api_key}
    base = str(args.api).rstrip("/")
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=70.0) as client:
            created = client.post("/v1/incidents", json=payload)
            if created.status_code >= 400:
                print(created.text, file=sys.stderr)
                return 1
            incident_id = created.json()["id"]
            body: dict[str, Any] = {}
            if args.scenario:
                body["scenario_id"] = args.scenario
            started = client.post(f"/v1/incidents/{incident_id}/runs", json=body)
            if started.status_code >= 400:
                print(started.text, file=sys.stderr)
                return 1
            run_id = started.json()["id"]
            _follow(client, run_id)
            finished = client.get(f"/v1/runs/{run_id}")
            finished.raise_for_status()
            report = finished.json().get("final_report")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if finished.json().get("status") == "COMPLETED" else 1
    except httpx.HTTPError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _incident_payload(args: argparse.Namespace) -> dict[str, Any] | int:
    if args.scenario:
        path = frozen_dir() / f"{args.scenario}.yaml"
        if not path.is_file():
            print(f"Unknown scenario {args.scenario}.", file=sys.stderr)
            return 2
        payload = load_scenario(path).incident.model_dump(mode="json")
        if args.title:
            payload["title"] = args.title
        if args.description:
            payload["description"] = args.description
        if args.service:
            payload["service"] = args.service
        return payload
    if not args.title or not args.description:
        print("Pass --scenario, or both --title and --description.", file=sys.stderr)
        return 2
    return {
        "title": args.title,
        "description": args.description,
        "service": args.service,
    }


def _follow(client: httpx.Client, run_id: str) -> None:
    with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
        response.raise_for_status()
        event_name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                body = json.loads(line.split(":", 1)[1].strip())
                print(f"{body.get('seq')} {event_name} {body.get('summary', '')}")


if __name__ == "__main__":
    raise SystemExit(main())
