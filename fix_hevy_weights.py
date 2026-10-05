#!/usr/bin/env python3
"""Plan, apply, and verify a single backed-up Hevy weight migration (Python 3.10+)."""
from __future__ import annotations

import argparse
import copy
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BASE_URL = "https://api.hevyapp.com/v1"
SET_FIELDS = {"type", "weight_kg", "reps", "distance_meters", "duration_seconds", "custom_metric", "rpe"}
EXERCISE_FIELDS = {"exercise_template_id", "superset_id", "notes", "sets"}
WORKOUT_FIELDS = {"title", "description", "start_time", "end_time", "is_private", "exercises"}
SPECIAL_BAR = re.compile(r"\b(?:e[ -]?z|smith|trap|hex|safety|swiss|landmine|fixed)\b|\bt[ -]bar\b", re.I)


class HevyError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    def bad_constant(value):
        raise HevyError(f"Invalid JSON number: {value}")
    return json.loads(Path(path).read_text(), parse_constant=bad_constant)


def save_json(path, data, *, exclusive=False):
    path = Path(path)
    encoded = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
    else:
        fd, name = tempfile.mkstemp(prefix=".hevy-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HevyError(f"{label} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise HevyError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return Decimal(str(value))


def corrected_weight(old, equipment, bar_weight=20):
    weight = number(old, "Recorded weight")
    if equipment == "dumbbell":
        result = weight * 2
    elif equipment == "barbell":
        result = weight * 2 + number(bar_weight, "Bar weight", positive=True)
    else:
        raise HevyError(f"Unsupported equipment: {equipment}")
    value = float(result)
    number(value, "Corrected weight")
    return value


def classify_exercise(exercise, templates_by_id):
    """Equipment metadata is authoritative; never override non-target equipment."""
    template = templates_by_id.get(str(exercise["exercise_template_id"]), {})
    equipment = template.get("equipment")
    titles = f"{exercise.get('title', '')} {template.get('title', '')}"
    if equipment == "barbell" and SPECIAL_BAR.search(titles):
        return None
    return equipment if equipment in {"dumbbell", "barbell"} else None


def bar_weight_for(exercise, config):
    overrides = config.get("bar_weight_overrides", {})
    return overrides.get(str(exercise["exercise_template_id"]), overrides.get(exercise.get("title"), config["default_barbell_weight_kg"]))


def validate_config(config):
    allowed = {"default_barbell_weight_kg", "bar_weight_overrides", "default_is_private", "workout_privacy"}
    if set(config) - allowed:
        raise HevyError("Unknown configuration fields")
    number(config["default_barbell_weight_kg"], "Default bar weight", positive=True)
    for value in config.get("bar_weight_overrides", {}).values():
        number(value, "Bar weight override", positive=True)
    if config.get("default_is_private") is not None and type(config["default_is_private"]) is not bool:
        raise HevyError("default_is_private must be true, false, or null")
    if any(type(v) is not bool for v in config.get("workout_privacy", {}).values()):
        raise HevyError("workout_privacy values must be true or false")


def privacy_for(workout, config):
    value = workout.get("is_private")
    if type(value) is not bool:
        value = config.get("workout_privacy", {}).get(workout["id"], config.get("default_is_private"))
    if type(value) is not bool:
        raise HevyError(f"Workout {workout['id']}: current visibility is unknown; configure it before applying")
    return value


def reject_unknown(obj, allowed, label):
    extra = set(obj) - allowed
    if extra:
        raise HevyError(f"Cannot preserve undocumented {label} fields: {', '.join(sorted(extra))}")


def workout_to_update_payload(workout, config):
    """Only documented PUT fields; abort rather than discard unknown data."""
    reject_unknown(workout, WORKOUT_FIELDS | {"id", "routine_id", "created_at", "updated_at"}, "workout")
    for key in ("id", "title", "start_time", "end_time", "exercises"):
        if key not in workout:
            raise HevyError(f"Workout missing required field: {key}")
    body = {k: copy.deepcopy(v) for k, v in workout.items() if k in WORKOUT_FIELDS}
    body["is_private"] = privacy_for(workout, config)
    exercises = []
    for ei, exercise in enumerate(workout["exercises"]):
        reject_unknown(exercise, EXERCISE_FIELDS | {"index", "title"}, "exercise")
        if exercise.get("index", ei) != ei:
            raise HevyError("Exercise indexes do not match array order")
        if not exercise.get("exercise_template_id") or not isinstance(exercise.get("sets"), list):
            raise HevyError("Incomplete exercise structure")
        target = {k: copy.deepcopy(v) for k, v in exercise.items() if k in EXERCISE_FIELDS}
        target["sets"] = []
        for si, logged_set in enumerate(exercise["sets"]):
            reject_unknown(logged_set, SET_FIELDS | {"index"}, "set")
            if logged_set.get("index", si) != si:
                raise HevyError("Set indexes do not match array order")
            if logged_set.get("type") not in {"normal", "warmup", "dropset", "failure"}:
                raise HevyError("Unsupported set type")
            target["sets"].append({k: copy.deepcopy(v) for k, v in logged_set.items() if k in SET_FIELDS})
        exercises.append(target)
    body["exercises"] = exercises
    return {"workout": body}


def comparable(workout, config):
    result = copy.deepcopy(workout)
    result.pop("updated_at", None)  # Server-maintained timestamp necessarily changes.
    try:
        result["is_private"] = privacy_for(workout, config)
    except HevyError:
        # Untouched workouts need no PUT visibility value during full-history verification.
        # validate_plan still requires known visibility for every workout being changed.
        pass
    return result


def service_title_updates(actual, expected, templates_by_id):
    """Report Hevy refreshing a response-only title to its known template name."""
    updates = []
    for index, (observed, planned) in enumerate(zip(actual["exercises"], expected["exercises"])):
        tid = str(planned["exercise_template_id"])
        canonical = templates_by_id.get(tid, {}).get("title")
        if (observed.get("exercise_template_id") == planned["exercise_template_id"]
                and classify_exercise(planned, templates_by_id) in {"dumbbell", "barbell"}
                and observed.get("title") != planned.get("title")
                and isinstance(canonical, str) and observed.get("title") == canonical):
            updates.append({"exercise_index": index, "exercise_template_id": tid,
                            "previous_title": planned.get("title"), "current_title": canonical})
    return updates


def matches_corrected_workout(actual, expected, config, templates_by_id):
    observed = comparable(actual, config)
    for update in service_title_updates(actual, expected, templates_by_id):
        observed["exercises"][update["exercise_index"]]["title"] = update["previous_title"]
    return observed == comparable(expected, config)


def build_plan(backup, config):
    validate_config(config)
    templates = {str(t["id"]): t for t in backup["exercise_templates"]}
    corrections, excluded, blockers = [], {}, []
    seen = set()
    for workout in backup["workouts"]:
        wid = workout["id"]
        if wid in seen:
            raise HevyError("Duplicate workout in backup")
        seen.add(wid)
        before = len(corrections)
        for ei, exercise in enumerate(workout["exercises"]):
            equipment = classify_exercise(exercise, templates)
            if equipment is None:
                tid = str(exercise["exercise_template_id"])
                raw_equipment = templates.get(tid, {}).get("equipment")
                reason = "specialty bar" if raw_equipment == "barbell" else ("missing equipment metadata" if raw_equipment is None else "other equipment")
                key = (exercise.get("title", tid), reason)
                excluded[key] = excluded.get(key, 0) + len(exercise["sets"])
                if raw_equipment is None:
                    blockers.append(f"Missing equipment metadata for {tid}: review classification")
                continue
            for si, logged_set in enumerate(exercise["sets"]):
                old = logged_set.get("weight_kg")
                if old is None:
                    continue
                bar = bar_weight_for(exercise, config) if equipment == "barbell" else None
                new = corrected_weight(old, equipment, bar)
                if new == old:
                    continue
                corrections.append({
                    "workout_id": wid, "workout_title": workout["title"], "date": workout["start_time"],
                    "exercise_index": ei, "exercise_template_id": exercise["exercise_template_id"],
                    "exercise_title": exercise.get("title", ""), "equipment": equipment,
                    "set_index": si, "set_type": logged_set["type"], "reps": logged_set.get("reps"),
                    "old_weight_kg": old, "new_weight_kg": new, "bar_weight_kg": bar,
                })
        if len(corrections) > before:
            try:
                workout_to_update_payload(workout, config)
            except HevyError as exc:
                blockers.append(str(exc))
    return {
        "schema_version": 1, "backup_sha256": digest(backup), "account_id": backup["account_id"],
        "config": copy.deepcopy(config), "corrections": corrections,
        "excluded": [{"exercise": title, "reason": reason, "sets": count} for (title, reason), count in sorted(excluded.items())],
        "blockers": sorted(set(blockers)),
    }


def validate_plan(plan, backup):
    if plan != build_plan(backup, plan["config"]):
        raise HevyError("Plan differs from the backed-up data and correction rules; refusing to apply")
    if plan["blockers"]:
        raise HevyError("Plan has unresolved blockers:\n" + "\n".join(plan["blockers"]))


def expected_workouts(plan, backup):
    originals = {w["id"]: w for w in backup["workouts"]}
    desired = {}
    for change in plan["corrections"]:
        wid = change["workout_id"]
        if wid not in desired:
            desired[wid] = copy.deepcopy(originals[wid])
        desired[wid]["exercises"][change["exercise_index"]]["sets"][change["set_index"]]["weight_kg"] = change["new_weight_kg"]
    return originals, desired


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HevyError("API redirect refused to protect the API key")


class HevyAPI:
    def __init__(self, key_file):
        self.key = os.environ.get("HEVY_API_KEY", "").strip()
        if not self.key and Path(key_file).is_file():
            self.key = Path(key_file).read_text().strip()
        if not self.key or "\n" in self.key or "\r" in self.key:
            raise HevyError("Set HEVY_API_KEY or save the key only in .hevy-api-key")
        # Python.org's macOS build may lack a CA bundle; use system trust without disabling TLS.
        ca = "/etc/ssl/cert.pem" if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").exists() else None
        context = ssl.create_default_context(cafile=ca)
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, method, path, body=None):
        if not path.startswith("/") or method not in {"GET", "PUT"}:
            raise HevyError("Unsupported API request")
        request = urllib.request.Request(BASE_URL + path, method=method,
            headers={"api-key": self.key, "Accept": "application/json", "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body, allow_nan=False).encode())
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(min(2 ** (attempt + 1), 10))
                    continue
                raise HevyError(f"{method} {path}: HTTP {exc.code}. No automatic write retry; resume the SAME plan.") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                raise HevyError(f"{method} {path}: request failed. Write status may be unknown; resume the SAME plan.") from None

    def account_id(self):
        return self.request("GET", "/user/info")["data"]["id"]

    def get_workout(self, workout_id):
        data = self.request("GET", "/workouts/" + urllib.parse.quote(workout_id, safe=""))
        if isinstance(data, dict) and "workout" in data:
            data = data["workout"]
        if not isinstance(data, dict) or data.get("id") != workout_id:
            raise HevyError("Unexpected single-workout response")
        return data

    def update_workout(self, workout_id, payload):
        return self.request("PUT", "/workouts/" + urllib.parse.quote(workout_id, safe=""), payload)

    def all_pages(self, path, field, size):
        result, seen = [], set()
        total_pages = None
        for page in range(1, 100001):
            data = self.request("GET", f"{path}?page={page}&pageSize={size}")
            count = data.get("page_count")
            if type(count) is not int or count < 0 or data.get("page") != page or not isinstance(data.get(field), list):
                raise HevyError("Invalid pagination response")
            if total_pages is not None and count != total_pages:
                raise HevyError("History changed during pagination; try again")
            total_pages = count
            for item in data[field]:
                identity = str(item["id"])
                if identity in seen:
                    raise HevyError("Duplicate item during pagination")
                seen.add(identity)
                result.append(item)
            print(f"Downloaded {field} page {page}/{count}", flush=True)
            if page >= count:
                return result
            if not data[field]:
                raise HevyError("Unexpected empty page")
        raise HevyError("Pagination limit exceeded")

    def download(self):
        account = self.account_id()
        count = self.request("GET", "/workouts/count")["workout_count"]
        workouts = self.all_pages("/workouts", "workouts", 10)
        templates = self.all_pages("/exercise_templates", "exercise_templates", 100)
        if len(workouts) != count or self.request("GET", "/workouts/count")["workout_count"] != count:
            raise HevyError("Workout count changed during backup")
        return {"created_at": now(), "account_id": account, "workouts": workouts, "exercise_templates": templates}


def write_preview(run_dir, plan, backup):
    path = Path(run_dir)
    corrections = plan["corrections"]
    with (path / "preview.csv").open("w", newline="") as f:
        columns = ["date", "workout_title", "exercise_title", "set_index", "equipment", "old_weight_kg", "new_weight_kg", "bar_weight_kg", "workout_id"]
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for c in corrections:
            row = dict(c, set_index=c["set_index"] + 1)
            # Prevent spreadsheet formula execution when a remote title is opened in Excel.
            for key, value in row.items():
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    row[key] = "'" + value
            writer.writerow(row)
    counts = Counter(c["equipment"] for c in corrections)
    lines = ["# Hevy weight correction preview", "", "No changes have been sent by the plan command.", "",
        f"Workouts backed up: {len(backup['workouts'])}",
        f"Workouts to update: {len({c['workout_id'] for c in corrections})}",
        f"Dumbbell sets: {counts['dumbbell']}; barbell sets: {counts['barbell']}; total: {len(corrections)}.", "",
        f"Dumbbells × 2. Standard barbells × 2 + {plan['config']['default_barbell_weight_kg']} kg.",
        "The configured bar weight must match the equipment used; specialty bars are excluded.",
        "All calculations use kilograms, including if Hevy displays pounds.", "",
        "Only planned weight_kg fields are changed. Exercise order, sets, reps, types, notes, supersets, distances, durations and RPE are checked.",
        "The API does not expose workout visibility; configured visibility is supplied explicitly.",
        "Routine linkage is read-only in the documented API; it is checked after every write.", "",
        "## Changes by exercise", "", "| Exercise | Sets |", "| --- | ---: |"]
    for title, count in sorted(Counter(c["exercise_title"] for c in corrections).items()):
        lines.append(f"| {title.replace('|', '/')} | {count} |")
    lines += ["", "## Exercises left unchanged", "", "| Exercise | Reason | Sets |", "| --- | --- | ---: |"]
    for item in plan["excluded"]:
        lines.append(f"| {item['exercise'].replace('|', '/')} | {item['reason']} | {item['sets']} |")
    lines += ["", "## Blockers", ""]
    if plan["blockers"]:
        visibility = [b for b in plan["blockers"] if "current visibility is unknown" in b]
        if visibility:
            lines.append(f"Existing visibility is unknown for {len(visibility)} affected workouts. No updates may be sent until this is resolved.")
        lines += [b for b in plan["blockers"] if b not in visibility]
    else:
        lines.append("None.")
    (path / "summary.md").write_text("\n".join(lines) + "\n")


def apply_plan(plan, backup, api, run_dir, *, limit=None):
    validate_plan(plan, backup)
    if api.account_id() != plan["account_id"]:
        raise HevyError("API key belongs to a different account")
    originals, desired = expected_workouts(plan, backup)
    templates = {str(t["id"]): t for t in backup["exercise_templates"]}
    config = plan["config"]
    run_dir = Path(run_dir)
    journal_path = run_dir / "journal.json"
    journal = read_json(journal_path) if journal_path.exists() else {"plan_sha256": digest(plan), "workouts": {}}
    if journal["plan_sha256"] != digest(plan):
        raise HevyError("Journal belongs to a different plan")

    def state(wid, current):
        if matches_corrected_workout(current, desired[wid], config, templates):
            return "corrected"
        if comparable(current, config) == comparable(originals[wid], config):
            return "original"
        raise HevyError(f"Workout {wid} changed since backup; stopping before overwriting it")

    # Preflight the entire plan before the first write, including unrelated fields.
    for wid in desired:
        state(wid, api.get_workout(wid))
    updated, already = 0, 0
    for wid in desired:
        current = api.get_workout(wid)  # Recheck immediately before each write.
        if state(wid, current) == "corrected":
            already += 1
            journal["workouts"][wid] = {"status": "verified", "at": now(),
                "service_title_updates": service_title_updates(current, desired[wid], templates)}
            save_json(journal_path, journal)
            continue
        if limit is not None and updated >= limit:
            continue
        payload = workout_to_update_payload(desired[wid], config)
        journal["workouts"][wid] = {"status": "pending", "at": now()}
        save_json(journal_path, journal)
        api.update_workout(wid, payload)
        after = api.get_workout(wid)
        save_json(run_dir / f"verified-{wid}.json", after)
        if not matches_corrected_workout(after, desired[wid], config, templates):
            journal["workouts"][wid] = {"status": "verification_failed", "at": now()}
            save_json(journal_path, journal)
            raise HevyError(f"Verification failed for workout {wid}. Stopping; inspect backup and observed result before any further write")
        journal["workouts"][wid] = {"status": "verified", "at": now(),
            "service_title_updates": service_title_updates(after, desired[wid], templates)}
        save_json(journal_path, journal)
        updated += 1
        print(f"Verified {updated} workout update(s): {originals[wid]['title']}", flush=True)
        time.sleep(0.25)
    return {"updated_workouts": updated, "already_corrected_workouts": already, "planned_workouts": len(desired)}


def verify_plan(plan, backup, api):
    validate_plan(plan, backup)
    if api.account_id() != plan["account_id"]:
        raise HevyError("API key belongs to a different account")
    originals, desired = expected_workouts(plan, backup)
    templates = {str(t["id"]): t for t in backup["exercise_templates"]}
    title_updates = []
    for wid, original in originals.items():
        expected = desired.get(wid, original)
        current = api.get_workout(wid)
        # Privacy is independently supplied, not observable when omitted by GET.
        matches = (matches_corrected_workout(current, expected, plan["config"], templates) if wid in desired
                   else comparable(current, plan["config"]) == comparable(expected, plan["config"]))
        if not matches:
            raise HevyError(f"Verification mismatch in workout {wid}")
        title_updates.extend(dict(update, workout_id=wid) for update in service_title_updates(current, expected, templates))
    return {"verified_at": now(), "workouts_checked": len(originals), "corrected_sets": len(plan["corrections"]), "api_fields_match": True,
            "service_title_updates": title_updates,
            "visibility_note": "Visibility supplied from configuration; GET does not expose it."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "refresh-plan", "apply", "verify"])
    parser.add_argument("--run-dir", type=Path, default=Path("hevy-correction"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--api-key-file", type=Path, default=Path(".hevy-api-key"))
    parser.add_argument("--from-backup", type=Path, help="Build the initial plan from an existing downloaded backup")
    parser.add_argument("--yes", action="store_true", help="Apply the reviewed plan without an interactive prompt")
    parser.add_argument("--limit", type=int, help="Limit writes for a first-workout verification")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    try:
        if args.command == "plan":
            if args.run_dir.exists():
                raise HevyError("Run directory already exists; reuse its plan to avoid double correction")
            config = read_json(args.config)
            validate_config(config)
            backup = read_json(args.from_backup) if args.from_backup else HevyAPI(args.api_key_file).download()
            plan = build_plan(backup, config)
            args.run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            save_json(args.run_dir / "backup.json", backup, exclusive=True)
            save_json(args.run_dir / "plan.json", plan, exclusive=True)
            write_preview(args.run_dir, plan, backup)
            print(f"Planned {len(plan['corrections'])} set corrections. No Hevy changes sent.")
            print(f"Preview: {(args.run_dir / 'summary.md').resolve()}")
            print(f"Blockers: {len(plan['blockers'])}")
            return
        with (args.run_dir / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HevyError("Another process is using this plan") from None
            plan, backup = read_json(args.run_dir / "plan.json"), read_json(args.run_dir / "backup.json")
            if args.command == "refresh-plan":
                if (args.run_dir / "journal.json").exists():
                    raise HevyError("Cannot revise a plan after applying has started")
                if plan["backup_sha256"] != digest(backup):
                    raise HevyError("Backup differs from original plan")
                refreshed = build_plan(backup, read_json(args.config))
                save_json(args.run_dir / "plan.json", refreshed)
                write_preview(args.run_dir, refreshed, backup)
                print(f"Refreshed the plan from the SAME backup. Blockers: {len(refreshed['blockers'])}. No Hevy changes sent.")
                return
            validate_plan(plan, backup)
            api = HevyAPI(args.api_key_file)
            if args.command == "apply":
                if not args.yes and input(f"Apply {len(plan['corrections'])} reviewed corrections? Type APPLY: ") != "APPLY":
                    print("Cancelled; no updates sent.")
                    return
                print(json.dumps(apply_plan(plan, backup, api, args.run_dir, limit=args.limit), indent=2))
            else:
                report = verify_plan(plan, backup, api)
                save_json(args.run_dir / "verification.json", report)
                print(json.dumps(report, indent=2))
    except (HevyError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Interrupted. Resume the SAME plan; do not generate another plan.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
